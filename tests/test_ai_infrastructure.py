"""Client Claude, coûts, briefing, contexte et scanner — sans réseau."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.ai.briefing import SUBMIT_BRIEFING_TOOL, parse_briefing
from app.ai.claude import ClaudeClient, ClaudeError
from app.ai.context import MarketContextBuilder, summarize_timeframe
from app.ai.costs import BudgetExceededError, CostTracker, cost_of
from app.ai.decision import TradeDecision
from app.market.data_provider import MarketDataError, MarketDataProvider, generate_synthetic_candles
from app.strategy import CandleWindow, ConfluenceStrategy

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)


def usage(inp=1000, out=500, searches=0):
    return SimpleNamespace(input_tokens=inp, output_tokens=out, cache_read_input_tokens=0,
                           cache_creation_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=searches))


def test_cost_computation_and_budget():
    cost, counts = cost_of("claude-opus-5-5", usage(1_000_000, 100_000, 10))
    assert cost == pytest.approx(4.0 + 2.0 + 0.10) and counts["web_searches"] == 10
    records = []
    tracker = CostTracker(1.0, 2, sink=records.append)
    tracker.check(0.1, "test")
    tracker.record("test", "claude-sonnet-5", usage(100_000, 10_000))
    assert tracker.status()["spent_today_usd"] == pytest.approx(0.3) and len(records) == 1
    tracker.check(0.1, "test")
    with pytest.raises(BudgetExceededError):
        tracker.check(0.1, "test")  # 3e appel dans l'heure
    with pytest.raises(BudgetExceededError):
        CostTracker(0.2, 10, spent_today=0.15).check(0.1, "test")


def fake_anthropic(responses):
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)
    return SimpleNamespace(messages=SimpleNamespace(parse=respond, create=respond)), calls


def test_structured_call_uses_cache_and_effort():
    parsed = TradeDecision(action="HOLD", confidence=0.5, market_regime="r", thesis="t", key_factors=[],
                           risks=[], invalidation="i", entry=None, adjustment=None)
    client, calls = fake_anthropic([SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=usage())])
    claude = ClaudeClient("key", CostTracker(5, 10), client=client)
    result = claude.structured(purpose="décision", model="claude-opus-5-5", effort="medium", system="S",
                               user="U", output_model=TradeDecision, estimate_usd=0.1)
    assert result.action == "HOLD"
    assert calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert calls[0]["output_config"] == {"effort": "medium"} and "thinking" not in calls[0]


def test_research_handles_pause_turn_and_submit_tool():
    tool_use = SimpleNamespace(type="tool_use", name="submit_briefing", input={"summary": "ok"})
    responses = [SimpleNamespace(stop_reason="pause_turn", content=[SimpleNamespace(type="server_tool_use")],
                                 usage=usage(searches=2)),
                 SimpleNamespace(stop_reason="tool_use", content=[tool_use], usage=usage(searches=1))]
    client, calls = fake_anthropic(responses)
    claude = ClaudeClient("key", CostTracker(5, 10), client=client)
    data = claude.research(purpose="briefing", model="claude-sonnet-5", effort="low", system="S", user="U",
                           submit_tool=SUBMIT_BRIEFING_TOOL, max_searches=3, estimate_usd=0.1)
    assert data == {"summary": "ok"} and len(calls) == 2
    assert calls[1]["messages"][-1]["role"] == "assistant"  # reprise sans message « continue »
    assert calls[0]["tools"][0]["type"].startswith("web_search")


def test_refusal_raises():
    client, _ = fake_anthropic([SimpleNamespace(stop_reason="refusal", parsed_output=None, usage=usage())])
    with pytest.raises(ClaudeError):
        ClaudeClient("key", CostTracker(5, 10), client=client).structured(
            purpose="x", model="claude-opus-5-5", effort="low", system="S", user="U",
            output_model=TradeDecision, estimate_usd=0.1)


def test_briefing_parsing_adds_blackouts_for_high_impact_events():
    data = {"overall_sentiment": 3, "risk_level": "high", "summary": "s", "per_symbol": [], "sources": [],
            "key_events": [{"time_utc": "2026-09-27T18:00:00Z", "name": "FOMC", "impact": "high", "region": "US"},
                           {"time_utc": "", "name": "?", "impact": "high", "region": "US"},
                           {"time_utc": "2026-09-27T20:00:00Z", "name": "PMI", "impact": "low", "region": "US"}],
            "blackout_windows": [{"start_utc": "pas une date", "end_utc": "x", "reason": "bad"}]}
    briefing = parse_briefing(data, "m", NOW, blackout_minutes=30)
    assert briefing.overall_sentiment == 1.0 and len(briefing.blackout_windows) == 1
    window = briefing.blackout_windows[0]
    assert window.start_utc == NOW + timedelta(hours=5, minutes=30) and "FOMC" in window.reason
    assert briefing.active_blackout(NOW + timedelta(hours=6)) is not None
    assert briefing.active_blackout(NOW) is None


class OfflineProvider(MarketDataProvider):
    def fetch_candles(self, symbol, timeframe, limit=500):
        if timeframe == "1d":
            raise MarketDataError("indisponible")
        return generate_synthetic_candles(symbol, timeframe, 300, seed=1)


def test_context_builder_tolerates_missing_sources():
    context = MarketContextBuilder(OfflineProvider(), timeframes=["1h", "1d"]).build("BTCUSDT", "4h")
    frames = context["analyse_multi_timeframe"]
    assert frames["4h"]["tendance"] in ("haussière", "baissière", "neutre / range")
    assert frames["1d"]["disponible"] is False
    assert len(context["dernieres_bougies"]["valeurs"]) == 24
    assert context["derives"] is None and context["fear_greed"] is None
    assert summarize_timeframe(generate_synthetic_candles(bars=30))["disponible"] is False


def test_confluence_scanner_scores_and_signals():
    candles = generate_synthetic_candles(bars=900, seed=11)
    strategy = ConfluenceStrategy()
    signals = []
    for end in range(strategy.warmup_bars + 5, len(candles)):
        analysis = strategy.analyze(CandleWindow.from_candles(candles[end - 400 if end > 400 else 0:end]))
        assert "adx" in analysis.indicators
        if analysis.signal:
            signals.append(analysis.signal)
    assert signals and all("score" in s.reason for s in signals)
    assert {s.direction.value for s in signals} <= {"LONG", "SHORT"}
