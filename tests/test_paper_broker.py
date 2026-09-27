from datetime import timedelta

import pytest

from app.core.events import EventBus, EventCollector, EventType
from app.core.types import OrderIntent, OrderStatus, OrderType, Side
from app.risk.risk_manager import RiskLimits
from app.trading.orders import OrderRequest
from app.trading.paper_broker import OrderRejectedError, PaperBroker
from app.trading.portfolio import Portfolio
from tests.conftest import T0, candle

FEE, SLIP = 0.001, 0.0005


@pytest.fixture
def events():
    return EventCollector()


@pytest.fixture
def broker(events):
    bus = EventBus()
    bus.subscribe(events)
    return PaperBroker(Portfolio(10_000.0), bus, RiskLimits(fee_rate=FEE, slippage_rate=SLIP))


def buy(broker, qty=10.0, stop=2.0, tp=4.0, side=Side.BUY, **kwargs):
    return broker.submit_order(OrderRequest("TEST", side, qty, stop_distance=stop, take_profit_distance=tp,
                                            reference_price=100.0, strategy="t", reason="test", **kwargs), T0)


def test_market_order_filled_at_next_open_with_slippage_and_fees(broker):
    order = buy(broker)
    assert order.status is OrderStatus.PENDING
    broker.process_bar(candle(100, 100.5, 99.5, 100.2, index=1))
    position = broker.portfolio.position("TEST")
    assert order.status is OrderStatus.FILLED
    assert position.entry_price == pytest.approx(100 * (1 + SLIP))
    assert position.stop_loss == pytest.approx(position.entry_price - 2.0)
    assert position.take_profit == pytest.approx(position.entry_price + 4.0)
    assert order.fees == pytest.approx(10 * 100.05 * FEE)
    assert order.slippage == pytest.approx(10 * 0.05)
    assert broker.portfolio.balance == pytest.approx(10_000 - order.fees)


def test_order_never_filled_on_a_candle_opened_before_it(broker):
    order = broker.submit_order(OrderRequest("TEST", Side.BUY, 1.0, stop_distance=2.0),
                                T0 + timedelta(minutes=30))  # reçu en milieu de bougie
    broker.process_bar(candle(90, 101, 89, 100, index=0))  # bougie ouverte à T0 : trop tôt
    assert order.status is OrderStatus.PENDING
    broker.process_bar(candle(100, 100.5, 99.5, 100.2, index=1))
    assert order.status is OrderStatus.FILLED
    assert order.fill_price == pytest.approx(100 * (1 + SLIP))


def test_short_entry_receives_lower_price(broker):
    buy(broker, side=Side.SELL)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    position = broker.portfolio.position("TEST")
    assert position.entry_price == pytest.approx(100 * (1 - SLIP))
    assert position.stop_loss > position.entry_price > position.take_profit


def test_order_rejected_when_funds_insufficient(events):
    bus = EventBus()
    bus.subscribe(events)
    broker = PaperBroker(Portfolio(10_000.0), bus, RiskLimits(min_qty=1.0, qty_step=1.0))
    order = broker.submit_order(OrderRequest("TEST", Side.BUY, 1.0, stop_distance=100.0), T0)
    broker.process_bar(candle(20_000, 20_100, 19_900, 20_000, index=1))
    assert order.status is OrderStatus.REJECTED and "Fonds" in order.reject_reason
    assert broker.portfolio.position("TEST") is None


def test_invalid_orders_are_refused_at_submission(broker):
    with pytest.raises(OrderRejectedError):
        broker.submit_order(OrderRequest("TEST", Side.BUY, 0.0, stop_distance=1.0), T0)
    with pytest.raises(OrderRejectedError):
        broker.submit_order(OrderRequest("TEST", Side.BUY, 1.0, order_type=OrderType.LIMIT, stop_distance=1.0), T0)
    with pytest.raises(OrderRejectedError):
        broker.submit_order(OrderRequest("TEST", Side.BUY, 1.0, stop_distance=None), T0)


def test_stop_loss_triggered_with_slippage(broker):
    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    stop = broker.portfolio.position("TEST").stop_loss
    broker.process_bar(candle(99.5, 99.8, 97.0, 97.5, index=2))
    trade = broker.portfolio.trades[-1]
    assert trade.order_type is OrderType.STOP_LOSS
    assert trade.exit_price == pytest.approx(stop * (1 - SLIP))
    assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees)
    assert trade.net_pnl < 0
    assert broker.portfolio.position("TEST") is None


def test_take_profit_triggered_without_slippage(broker):
    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    tp = broker.portfolio.position("TEST").take_profit
    broker.process_bar(candle(101, 105, 100.8, 104.5, index=2))
    trade = broker.portfolio.trades[-1]
    assert trade.order_type is OrderType.TAKE_PROFIT
    assert trade.exit_price == pytest.approx(tp)
    assert trade.gross_pnl == pytest.approx(10 * 4.0)
    assert trade.r_multiple > 1.5


def test_stop_has_priority_when_both_levels_hit(broker):
    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    broker.process_bar(candle(100, 106, 96, 101, index=2))
    assert broker.portfolio.trades[-1].order_type is OrderType.STOP_LOSS


def test_gap_through_stop_fills_at_open(broker):
    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    broker.process_bar(candle(95, 96, 94, 95.5, index=2))
    assert broker.portfolio.trades[-1].exit_price == pytest.approx(95 * (1 - SLIP))


def test_limit_order_fills_only_when_price_reached(broker):
    order = buy(broker, order_type=OrderType.LIMIT, limit_price=95.0)
    broker.process_bar(candle(100, 101, 96, 97, index=1))
    assert order.status is OrderStatus.PENDING
    broker.process_bar(candle(97, 97.5, 94, 96, index=2))
    assert order.status is OrderStatus.FILLED
    assert order.fill_price == pytest.approx(95.0) and order.slippage == 0.0


def test_fees_accounting_balance_matches_realized_pnl(broker):
    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    broker.process_bar(candle(101, 105, 100.8, 104.5, index=2))
    trade = broker.portfolio.trades[-1]
    assert trade.fees == pytest.approx(10 * 100.05 * FEE + 10 * trade.exit_price * FEE)
    assert broker.portfolio.balance == pytest.approx(10_000 + trade.net_pnl)


def test_market_close_and_trailing_stop_exit(broker):
    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=1))
    broker.modify_stop("TEST", 101.0, T0, "test")
    assert broker.portfolio.position("TEST").trailing_active
    broker.process_bar(candle(101.5, 102, 100.5, 101, index=2))
    assert broker.portfolio.trades[-1].order_type is OrderType.TRAILING_STOP

    buy(broker)
    broker.process_bar(candle(100, 100.5, 99.5, 100, index=3))
    close_order = broker.close_position("TEST", "manuel", T0)
    assert close_order.intent is OrderIntent.CLOSE
    assert broker.close_position("TEST", "doublon", T0) is None
    broker.process_bar(candle(100.2, 100.5, 99.9, 100.1, index=4))
    assert broker.portfolio.trades[-1].exit_price == pytest.approx(100.2 * (1 - SLIP))


def test_order_ids_are_unique_and_events_emitted(broker, events):
    ids = {buy(broker, qty=1).id for _ in range(5)}
    assert len(ids) == 5 and all(i.startswith("PO-") for i in ids)
    assert len(events.of_type(EventType.ORDER)) == 5


def test_paper_broker_declares_itself_simulated():
    assert PaperBroker.is_live is False
