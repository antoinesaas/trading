"""Orchestrateur IA testé avec un faux Claude (aucun appel réseau, aucun coût)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.briefing import BlackoutWindow, MarketBriefing
from app.ai.claude import ClaudeError
from app.ai.costs import BudgetExceededError
from app.ai.decision import EntryPlan, PositionAdjustment, TradeDecision
from app.ai.trader import AITrader, AITraderConfig
from app.core.events import EventBus
from app.core.types import Candle
from app.database.database import Database
from app.database.repository import DatabaseRecorder, TradingRepository
from app.engine.params import ParameterSet
from app.engine.stack import build_trading_stack
from tests.conftest import zero_cost_limits

SYMBOL = "BTCUSDT"


def decision(action="OPEN_LONG", confidence=0.8, **entry_overrides):
    entry = None
    if action.startswith("OPEN"):
        entry = EntryPlan(**({"order_type": "MARKET", "limit_price": None, "stop_loss": 98.0, "take_profit": 104.0,
                              "partial_take_profit_price": 102.0, "partial_take_profit_fraction": 0.5,
                              "breakeven_at_r": 1.0, "time_stop_hours": 24.0, "risk_percent": 1.0}
                             | entry_overrides))
    return TradeDecision(action=action, confidence=confidence, market_regime="test", thesis="thèse de test",
                         key_factors=["a"], risks=["b"], invalidation="c", entry=entry, adjustment=None)


class FakeTrader:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def decide(self, context, *, review):
        self.calls.append((review, context))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result, "fake-model"


class FakeContext:
    def build(self, symbol, timeframe):
        return {"analyse_multi_timeframe": {timeframe: {"atr14": 1.0}},
                "carnet_ordres": {"spread_pct": 0.0001}}


class FakeBriefing:
    def __init__(self, latest=None):
        self.latest = latest

    def blackout(self, now):
        return self.latest.active_blackout(now) if self.latest else None

    def due(self, now):
        return False

    async def ensure_ready(self):
        return None


@pytest.fixture
def setup(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'ai.db').as_posix()}")
    db.create_schema()
    repo = TradingRepository(db)
    bus = EventBus()
    bus.subscribe(DatabaseRecorder(repo))
    stack = build_trading_stack(ParameterSet(), zero_cost_limits(), 10_000, bus=bus)
    stack.engine.auto_execute = False
    now = datetime.now(UTC).replace(microsecond=0)
    stack.engine.on_bar_update(Candle(SYMBOL, "4h", now - timedelta(hours=1), now + timedelta(hours=3),
                                      100, 100.5, 99.5, 100, 10, closed=False))

    def make(result, briefing=None, **config):
        trader = FakeTrader(result)
        ai = AITrader(stack.engine, trader, FakeContext(), repo, bus,
                      AITraderConfig(symbols=[SYMBOL], timeframe="4h", **config), briefing or FakeBriefing())
        return ai, trader
    return stack, repo, make


def test_open_decision_is_executed_with_claude_plan(setup):
    stack, repo, make = setup
    ai, trader = make(decision())
    result = asyncio.run(ai.evaluate(SYMBOL, "manual"))
    assert result["status"] == "executed", result
    order = stack.broker.pending_orders()[0]
    assert order.quantity == pytest.approx(50.0)  # 1 % de 10 000 / distance 2
    assert order.tp1_distance == pytest.approx(2.0) and order.breakeven_at_r == 1.0
    assert order.time_stop == timedelta(hours=24) and order.decision_id == result["id"]
    context = trader.calls[0][1]
    for key in ("sessions_de_marche", "marche", "portefeuille", "actualites", "ton_historique"):
        assert key in context
    saved = repo.recent_decisions(1)[0]
    assert saved["status"] == "executed" and saved["order_id"] == order.id


def test_hold_and_low_confidence_do_not_trade(setup):
    stack, repo, make = setup
    ai, _ = make(decision("HOLD", 0.4))
    assert asyncio.run(ai.evaluate(SYMBOL, "manual"))["status"] == "hold"
    ai, _ = make(decision(confidence=0.6), min_confidence=0.65)
    result = asyncio.run(ai.evaluate(SYMBOL, "manual"))
    assert result["status"] == "rejected" and "Confiance" in result["reason"]
    assert stack.broker.pending_orders() == []


def test_risk_manager_still_has_the_last_word(setup):
    stack, _, make = setup
    ai, _ = make(decision(stop_loss=99.9))  # stop à 0,1 ATR : refusé
    result = asyncio.run(ai.evaluate(SYMBOL, "manual"))
    assert result["status"] == "rejected" and "ATR" in result["reason"]
    ai, _ = make(decision(risk_percent=10.0))  # demande 10 % : plafonné
    assert asyncio.run(ai.evaluate(SYMBOL, "manual"))["status"] == "executed"
    assert stack.broker.pending_orders()[0].quantity <= 100.0  # <= 2 % de risque


def test_blocks_are_checked_before_any_api_call(setup):
    stack, _, make = setup
    now = datetime.now(UTC)
    briefing = MarketBriefing(blackout_windows=[BlackoutWindow(start_utc=now - timedelta(minutes=5),
                                                              end_utc=now + timedelta(minutes=5), reason="FOMC")])
    ai, trader = make(decision(), briefing=FakeBriefing(briefing))
    result = asyncio.run(ai.evaluate(SYMBOL, "manual"))
    assert result["status"] == "skipped" and "FOMC" in result["reason"] and trader.calls == []
    stack.engine.entries_enabled = False
    ai, trader = make(decision())
    assert asyncio.run(ai.evaluate(SYMBOL, "manual"))["status"] == "skipped" and trader.calls == []


def test_budget_and_api_errors_are_recorded(setup):
    _, repo, make = setup
    ai, _ = make(BudgetExceededError("budget atteint"))
    assert asyncio.run(ai.evaluate(SYMBOL, "manual"))["status"] == "skipped"
    ai, _ = make(ClaudeError("API indisponible"))
    assert asyncio.run(ai.evaluate(SYMBOL, "manual"))["status"] == "error"
    assert {d["status"] for d in repo.recent_decisions(5)} == {"skipped", "error"}


def test_review_can_tighten_stop_or_close(setup):
    stack, _, make = setup
    ai, _ = make(decision())
    asyncio.run(ai.evaluate(SYMBOL, "manual"))
    now = datetime.now(UTC) + timedelta(hours=4)
    stack.engine.on_bar_update(Candle(SYMBOL, "4h", now, now + timedelta(hours=4), 100, 100.8, 99.8, 100.6, 10))
    position = stack.portfolio.position(SYMBOL)
    assert position is not None
    adjust = decision("ADJUST", 0.7)
    adjust = adjust.model_copy(update={"adjustment": PositionAdjustment(new_stop_loss=99.0, new_take_profit=None)})
    ai, _ = make(adjust)
    assert asyncio.run(ai.review(SYMBOL))["status"] == "executed" and position.stop_loss == 99.0
    ai, _ = make(decision("CLOSE", 0.7))
    assert asyncio.run(ai.review(SYMBOL))["status"] == "executed"
    assert stack.broker.pending_orders()[0].reason.startswith("Clôture IA")


def test_trade_outcome_is_linked_back_to_the_decision(setup):
    stack, repo, make = setup
    ai, _ = make(decision())
    decision_id = asyncio.run(ai.evaluate(SYMBOL, "manual"))["id"]
    now = datetime.now(UTC) + timedelta(hours=4)
    stack.engine.on_bar_update(Candle(SYMBOL, "4h", now, now + timedelta(hours=4), 100, 105, 99.9, 104.5, 10))
    record = repo.ai_track_record()
    assert record["trades_ia_clotures"] == 1
    saved = next(d for d in repo.recent_decisions(5) if d["id"] == decision_id)
    assert saved["outcome_r"] > 1
