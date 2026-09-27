import asyncio
from types import SimpleNamespace

import pytest

from app.ai.advisor import (
    AdvisorError, AdvisorProposal, AdvisorResponse, ClaudeParameterAdvisor, OptimizationContext,
    build_output_model,
)
from app.ai.optimizer import (
    CandidateEvaluation, OptimizationResult, PeriodEvaluation, StrategyOptimizer, check_search_space,
)
from app.ai.service import OptimizerService
from app.engine.params import ParameterSet
from app.market.data_provider import generate_synthetic_candles
from app.risk.risk_manager import RiskLimits
from app.strategy import EmaRsiParams

DEFAULT_EXITS = {"stop_loss_atr_multiplier": 2.0, "take_profit_risk_reward": 2.0, "trailing_stop_enabled": False,
                 "trailing_stop_atr_multiplier": 2.0, "trailing_activation_r": 1.0}
DEFAULT_STRATEGY = {"ema_fast": 20, "ema_slow": 50, "rsi_length": 14, "rsi_long_threshold": 50.0,
                    "rsi_short_threshold": 50.0, "atr_length": 14}


class FakeAdvisor:
    model_name = "fake-model"

    def __init__(self, proposals):
        self.proposals = proposals
        self.context = None

    def propose(self, context):
        self.context = context
        return AdvisorResponse("analyse de test", self.proposals)


def evaluation(score, trades=20, dd=0.05):
    return PeriodEvaluation(score, trades, score / 100, dd, 0.5, 1.2, {})


def test_optimizer_validates_and_backtests_candidates():
    datasets = {"SYN": generate_synthetic_candles("SYN", "1h", 900, seed=3)}
    advisor = FakeAdvisor([
        AdvisorProposal(DEFAULT_STRATEGY | {"ema_fast": 1_000}, DEFAULT_EXITS, "hors bornes"),
        AdvisorProposal(DEFAULT_STRATEGY, DEFAULT_EXITS, "identique"),
        AdvisorProposal(DEFAULT_STRATEGY | {"ema_fast": 12, "ema_slow": 40}, DEFAULT_EXITS | {"take_profit_risk_reward": 2.5}, "valide"),
    ])
    optimizer = StrategyOptimizer(advisor, RiskLimits(), initial_capital=10_000, timeframe="1h", candidates=3)
    result = optimizer.run(datasets, 300, ParameterSet().validated(), {"trades": 0}, [])
    invalid, same, valid = result.candidates
    assert not invalid.accepted and "invalides" in invalid.reason
    assert not same.accepted and "Identique" in same.reason
    assert valid.train is not None and valid.validation is not None and valid.reason
    # Claude ne voit que la période d'entraînement.
    assert advisor.context.market_stats["SYN"]["bars"] == 600
    assert advisor.context.risk_limits["max_drawdown"] == 0.10


def test_acceptance_rules():
    optimizer = StrategyOptimizer(FakeAdvisor([]), RiskLimits(max_drawdown=0.1), initial_capital=1, timeframe="1h",
                                  min_trades=10, min_improvement=1.0)
    base = OptimizationResult("", evaluation(5.0), evaluation(5.0))

    def decide(train, val):
        return optimizer._decide(CandidateEvaluation({}, "", train=train, validation=val), base)

    assert decide(evaluation(6), evaluation(7))[0] is True
    assert decide(evaluation(6), evaluation(5.5))[0] is False  # amélioration insuffisante
    assert decide(evaluation(4), evaluation(9))[0] is False  # dégrade l'entraînement
    assert decide(evaluation(6), evaluation(9, trades=3))[0] is False
    assert decide(evaluation(6), evaluation(9, dd=0.2))[0] is False


def test_search_space_is_enforced():
    check_search_space(ParameterSet().validated())
    with pytest.raises(ValueError):
        check_search_space(ParameterSet(strategy={"ema_fast": 4, "ema_slow": 50}).validated())


def test_claude_advisor_uses_opus_5_5_with_structured_output():
    output_model = build_output_model(EmaRsiParams)
    parsed = output_model.model_validate({"analysis": "ok", "proposals": [
        {"strategy": DEFAULT_STRATEGY, "exits": DEFAULT_EXITS, "rationale": "r"}]})
    calls = []

    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, _request_id="req_test")

    client = SimpleNamespace(messages=SimpleNamespace(parse=parse))
    advisor = ClaudeParameterAdvisor("key", client=client)
    context = OptimizationContext("ema_rsi", "1h", {}, {}, {}, {}, {}, {})
    response = advisor.propose(context)
    assert response.proposals[0].strategy == DEFAULT_STRATEGY
    kwargs = calls[0]
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["output_config"] == {"effort": "high"}
    assert "thinking" not in kwargs  # Opus 5.5 : réflexion adaptative par défaut
    assert set(kwargs["output_format"].model_fields) == {"analysis", "proposals"}


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_claude_advisor_handles_incomplete_answers(stop_reason):
    response = SimpleNamespace(stop_reason=stop_reason, parsed_output=None)
    client = SimpleNamespace(messages=SimpleNamespace(parse=lambda **_: response))
    with pytest.raises(AdvisorError):
        ClaudeParameterAdvisor("key", client=client).propose(
            OptimizationContext("ema_rsi", "1h", {}, {}, {}, {}, {}, {}))


def test_service_applies_accepted_candidate(services):
    new_params = ParameterSet(strategy=DEFAULT_STRATEGY | {"ema_fast": 12, "ema_slow": 40}).validated()

    class StubOptimizer:
        advisor = FakeAdvisor([])

        def run(self, *args):
            best = CandidateEvaluation(new_params.model_dump(mode="json"), "meilleur", True, "ok",
                                       evaluation(6), evaluation(8))
            return OptimizationResult("analyse", evaluation(5), evaluation(5), [best], best)

    services.optimizer.optimizer = StubOptimizer()
    services.optimizer.symbols = []
    outcome = asyncio.run(services.optimizer.run_once("test"))
    assert outcome["status"] == "applied"
    assert services.stack.engine.current_params().strategy["ema_fast"] == 12
    assert services.repo.active_version()[1].strategy["ema_fast"] == 12
    run = services.repo.list_runs(1)[0]
    assert run["status"] == "applied" and run["analysis"] == "analyse"


def test_service_reports_errors_without_crashing(services):
    class FailingOptimizer:
        advisor = FakeAdvisor([])

        def run(self, *args):
            raise AdvisorError("API indisponible")

    services.optimizer.optimizer = FailingOptimizer()
    services.optimizer.symbols = []
    outcome = asyncio.run(services.optimizer.run_once("test"))
    assert outcome["status"] == "error"
    assert services.repo.list_runs(1)[0]["status"] == "error"


def test_service_without_advisor_is_unavailable(services):
    assert isinstance(services.optimizer, OptimizerService)
    assert services.optimizer.available is False and services.optimizer.scheduled is False
