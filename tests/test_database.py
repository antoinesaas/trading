import pytest

from app.core.events import EventBus
from app.core.types import Direction, Signal
from app.database.database import Database, normalize_url
from app.database.repository import DatabaseRecorder, TradingRepository
from app.engine.params import ParameterSet
from app.engine.stack import build_trading_stack
from app.risk.risk_manager import RiskLimits
from tests.conftest import T0, candle


@pytest.fixture
def repo(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    db.create_schema()
    return TradingRepository(db)


def test_supabase_urls_use_psycopg_driver():
    assert normalize_url("postgres://u:p@h:5432/db") == "postgresql+psycopg://u:p@h:5432/db"
    assert normalize_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_url("sqlite:///x.db") == "sqlite:///x.db"


def test_every_event_is_recorded_and_state_restored(repo):
    bus = EventBus()
    bus.subscribe(DatabaseRecorder(repo))
    stack = build_trading_stack(ParameterSet(), RiskLimits(), 10_000.0, bus=bus)
    engine = stack.engine
    engine.handle_signal(Signal("AAA", Direction.LONG, 100.0, 1.0, T0, "t", "entrée A"))
    engine.handle_signal(Signal("BBB", Direction.SHORT, 50.0, 1.0, T0, "t", "entrée B"))
    engine.on_bar_update(candle(100, 100.5, 99.5, 100, index=1, symbol="AAA"))
    engine.on_bar_update(candle(50, 50.2, 49.9, 50, index=1, symbol="BBB"))
    engine.on_bar_update(candle(101, 105, 100.8, 104.5, index=2, symbol="AAA"))  # take-profit A
    engine.on_bar_close(candle(101, 105, 100.8, 104.5, index=2, symbol="AAA"))

    assert len(repo.recent_trades()) == 1
    assert len(repo.recent_orders()) == 3  # 2 entrées + 1 sortie TP
    assert len(repo.recent_signals()) == 2
    assert len(repo.equity_curve()) == 1
    trade = repo.recent_trades()[0]
    for key in ("timestamp", "symbol", "side", "order_type", "entry_price", "exit_price", "quantity", "stop_loss",
                "take_profit", "fees", "slippage", "gross_pnl", "net_pnl", "strategy", "reason"):
        assert key in trade

    state = repo.load_account_state(10_000.0)
    assert state.balance == pytest.approx(stack.portfolio.balance)
    assert [p.symbol for p in state.positions] == ["BBB"]
    assert state.positions[0].stop_loss == pytest.approx(stack.portfolio.position("BBB").stop_loss)
    assert state.id_counters == {"PO": 3, "POS": 2, "TR": 1}


def test_initial_capital_is_locked_after_first_run(repo):
    assert repo.load_account_state(10_000.0).initial_capital == 10_000.0
    assert repo.load_account_state(50_000.0).initial_capital == 10_000.0
    repo.reset_paper_account()
    assert repo.load_account_state(50_000.0).initial_capital == 50_000.0


def test_strategy_versions(repo):
    first = repo.save_version(ParameterSet(), source="config", status="active")
    params = ParameterSet(strategy={"ema_fast": 10, "ema_slow": 40})
    second = repo.save_version(params, source="ai", status="proposed", rationale="test")
    assert repo.active_version()[0] == first
    repo.activate_version(second)
    active_id, active = repo.active_version()
    assert active_id == second and active.strategy["ema_fast"] == 10
    assert {v["id"]: v["status"] for v in repo.list_versions()} == {first: "retired", second: "active"}
