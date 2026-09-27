"""Accès aux données : persistance des événements, lecture pour le dashboard, restauration d'état."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update

from app.core.events import EngineEvent, EventType
from app.core.types import Direction, OrderStatus, OrderType, Side, ensure_utc, utcnow
from app.database.database import Database
from app.database.models import (
    AIDecisionRow, AIUsageRow, Base, BotEventRow, BotStateRow, EquitySnapshotRow, MarketBriefingRow,
    OptimizationRunRow, OrderRow, PositionRow, SignalRow, StrategyVersionRow, TradeRow,
)
from app.engine.params import ParameterSet
from app.trading.positions import Position, Trade

logger = logging.getLogger(__name__)


def _dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    return ensure_utc(datetime.fromisoformat(str(value)))


def _safe_dt(value: Any) -> datetime | None:
    """Date issue de données non fiables (ISO ou epoch s/ms) ; ``None`` si illisible."""
    try:
        if isinstance(value, int | float) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
        return _dt(value)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _safe_num(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _row_dict(row: Base) -> dict[str, Any]:
    data = {c.key: getattr(row, c.key) for c in row.__table__.columns}
    return {k: (ensure_utc(v).isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


@dataclass(slots=True)
class RestoredState:
    initial_capital: float
    balance: float
    peak_equity: float
    positions: list[Position]
    id_counters: dict[str, int]
    day: date | None = None
    day_start_equity: float = 0.0
    week_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str | None = None
    last_processed: dict[str, str] = field(default_factory=dict)


class TradingRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- Écritures ------------------------------------------------------------------
    def upsert_order(self, data: dict[str, Any]) -> None:
        with self.db.session() as s:
            s.merge(OrderRow(
                id=data["id"], seq=data["seq"], symbol=data["symbol"], side=data["side"],
                order_type=data["order_type"], intent=data["intent"], status=data["status"],
                quantity=data["quantity"], limit_price=data.get("limit_price"),
                reference_price=data.get("reference_price"), fill_price=data.get("fill_price"),
                stop_loss=data.get("stop_loss"), take_profit=data.get("take_profit"),
                fees=data.get("fees", 0.0), slippage=data.get("slippage", 0.0),
                position_id=data.get("position_id"), strategy=data.get("strategy", ""),
                reason=data.get("reason", ""), reject_reason=data.get("reject_reason"),
                created_at=_dt(data["created_at"]), filled_at=_dt(data.get("filled_at")),
            ))

    def upsert_position(self, data: dict[str, Any], status: str) -> None:
        with self.db.session() as s:
            s.merge(PositionRow(
                id=data["id"], seq=data["seq"], symbol=data["symbol"], direction=data["direction"],
                status=status, quantity=data["quantity"], entry_price=data["entry_price"],
                entry_time=_dt(data["entry_time"]), stop_loss=data["stop_loss"],
                take_profit=data["take_profit"], initial_stop_loss=data["initial_stop_loss"],
                entry_fee=data["entry_fee"], entry_slippage=data["entry_slippage"],
                trailing_active=data.get("trailing_active", False), strategy=data.get("strategy", ""),
                entry_order_id=data["entry_order_id"], entry_reason=data.get("entry_reason", ""),
                updated_at=utcnow(), initial_quantity=data.get("initial_quantity"),
                tp1_price=data.get("tp1_price"), tp1_fraction=data.get("tp1_fraction"),
                tp1_done=data.get("tp1_done"), breakeven_at_r=data.get("breakeven_at_r"),
                breakeven_done=data.get("breakeven_done"), time_stop_at=_dt(data.get("time_stop_at")),
                stop_reason=data.get("stop_reason"), realized_pnl=data.get("realized_pnl"),
                exit_fees=data.get("exit_fees"), exit_slippage=data.get("exit_slippage"),
                exit_notional=data.get("exit_notional"), best_price=data.get("best_price"),
                decision_id=data.get("decision_id"), confidence=data.get("confidence"),
            ))

    def insert_trade(self, data: dict[str, Any]) -> None:
        fields = {k: v for k, v in data.items() if k in TradeRow.__table__.columns}
        fields.update(timestamp=_dt(data["exit_time"]), entry_time=_dt(data["entry_time"]))
        fields.pop("exit_time", None)
        with self.db.session() as s:
            s.merge(TradeRow(**fields))

    def insert_equity(self, data: dict[str, Any]) -> None:
        with self.db.session() as s:
            s.add(EquitySnapshotRow(
                timestamp=_dt(data["timestamp"]), balance=data["balance"], equity=data["equity"],
                unrealized_pnl=data["unrealized_pnl"], drawdown=data["drawdown"],
                peak_equity=data["peak_equity"], open_positions=data["open_positions"],
            ))

    def insert_signal(self, data: dict[str, Any], *, status: str | None = None,
                      reason: str | None = None) -> None:
        payload = json.loads(json.dumps(data, default=str, allow_nan=False)) if status is None else {
            k: str(v)[:200] for k, v in data.items()}  # données externes : stockées comme texte
        with self.db.session() as s:
            s.add(SignalRow(
                timestamp=_safe_dt(data.get("timestamp")) or utcnow(), received_at=utcnow(),
                symbol=str(data.get("symbol", "?"))[:32], direction=str(data.get("direction", "?"))[:8],
                price=_safe_num(data.get("price")), atr=_safe_num(data.get("atr")),
                source=str(data.get("source", "internal"))[:16], strategy=str(data.get("strategy", ""))[:64],
                status=status or data.get("status", "unknown"),
                reason=reason or data.get("status_reason") or data.get("reason", ""),
                order_id=data.get("order_id"), payload=payload,
            ))

    def insert_event(self, level: str, event_type: str, message: str,
                     data: dict[str, Any] | None = None) -> None:
        with self.db.session() as s:
            s.add(BotEventRow(timestamp=utcnow(), level=level, event_type=event_type,
                              message=message, data=data))

    # -- État clé/valeur ----------------------------------------------------------------
    def get_state(self, key: str, default: Any = None) -> Any:
        with self.db.session() as s:
            row = s.get(BotStateRow, key)
            return default if row is None else row.value

    def set_state(self, key: str, value: Any) -> None:
        with self.db.session() as s:
            s.merge(BotStateRow(key=key, value=value, updated_at=utcnow()))

    # -- Lectures dashboard ------------------------------------------------------------------
    def _recent(self, model: type[Base], order_column: Any, limit: int, **filters: Any) -> list[dict[str, Any]]:
        stmt = select(model).order_by(order_column.desc()).limit(limit)
        for name, value in filters.items():
            if value is not None:
                stmt = stmt.where(getattr(model, name) == value)
        with self.db.session() as s:
            return [_row_dict(r) for r in s.scalars(stmt)]

    def recent_trades(self, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        return self._recent(TradeRow, TradeRow.seq, limit, symbol=symbol)

    def recent_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._recent(OrderRow, OrderRow.seq, limit)

    def recent_signals(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._recent(SignalRow, SignalRow.id, limit)

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._recent(BotEventRow, BotEventRow.id, limit)

    def equity_curve(self, limit: int = 2_000) -> list[dict[str, Any]]:
        rows = self._recent(EquitySnapshotRow, EquitySnapshotRow.id, limit)
        return list(reversed(rows))

    def load_trades(self) -> list[Trade]:
        with self.db.session() as s:
            rows = s.scalars(select(TradeRow).order_by(TradeRow.seq)).all()
        return [_trade_from_row(r) for r in rows]

    # -- Restauration -----------------------------------------------------------------------
    def load_account_state(self, configured_capital: float) -> RestoredState:
        initial = self.get_state("initial_capital")
        if initial is None:
            initial = configured_capital
            self.set_state("initial_capital", initial)
        elif float(initial) != configured_capital:
            logger.warning("INITIAL_CAPITAL=%s ignoré : le compte paper existant a démarré avec %s. "
                           "Utilisez `python -m app reset-paper` pour repartir de zéro.",
                           configured_capital, initial)
        initial = float(initial)
        with self.db.session() as s:
            realized = s.scalar(select(func.coalesce(func.sum(TradeRow.net_pnl), 0.0))) or 0.0
            open_rows = s.scalars(select(PositionRow).where(PositionRow.status == "OPEN")).all()
            peak = s.scalar(select(func.max(EquitySnapshotRow.equity)))
            counters = {
                "PO": s.scalar(select(func.max(OrderRow.seq))) or 0,
                "POS": s.scalar(select(func.max(PositionRow.seq))) or 0,
                "TR": s.scalar(select(func.max(TradeRow.seq))) or 0,
            }
            s.execute(update(OrderRow).where(OrderRow.status == OrderStatus.PENDING.value)
                      .values(status=OrderStatus.CANCELLED.value, reject_reason="Redémarrage du bot"))
            today = datetime.now(UTC).date()
            first_today = s.scalar(
                select(EquitySnapshotRow.equity)
                .where(EquitySnapshotRow.timestamp >= datetime(today.year, today.month, today.day, tzinfo=UTC))
                .order_by(EquitySnapshotRow.id).limit(1))
            monday = today - timedelta(days=today.weekday())
            first_week = s.scalar(
                select(EquitySnapshotRow.equity)
                .where(EquitySnapshotRow.timestamp >= datetime(monday.year, monday.month, monday.day, tzinfo=UTC))
                .order_by(EquitySnapshotRow.id).limit(1))
        positions = [_position_from_row(r) for r in open_rows]
        # Les sorties partielles des positions ouvertes sont déjà encaissées dans la balance.
        balance = initial + realized + sum(p.realized_pnl - p.exit_fees - p.entry_fee for p in positions)
        halt = self.get_state("risk_halt") or {}
        return RestoredState(
            initial_capital=initial, balance=balance, peak_equity=max(float(peak or initial), initial),
            positions=positions, id_counters=counters, day=today if first_today else None,
            day_start_equity=float(first_today or 0.0), week_start_equity=float(first_week or 0.0),
            halted=bool(halt.get("halted")),
            halt_reason=halt.get("reason"), last_processed=self.get_state("last_processed") or {},
        )

    # -- Versions de stratégie ----------------------------------------------------------------
    def active_version(self) -> tuple[int, ParameterSet] | None:
        with self.db.session() as s:
            row = s.scalars(select(StrategyVersionRow).where(StrategyVersionRow.status == "active")
                            .order_by(StrategyVersionRow.id.desc()).limit(1)).first()
        return None if row is None else (row.id, ParameterSet.model_validate(row.params))

    def save_version(self, params: ParameterSet, *, source: str, status: str,
                     metrics: dict[str, Any] | None = None, rationale: str = "",
                     run_id: int | None = None) -> int:
        with self.db.session() as s:
            if status == "active":
                s.execute(update(StrategyVersionRow).where(StrategyVersionRow.status == "active")
                          .values(status="retired"))
            row = StrategyVersionRow(created_at=utcnow(), source=source, status=status,
                                     params=params.model_dump(mode="json"), metrics=metrics,
                                     rationale=rationale, optimization_run_id=run_id)
            s.add(row)
            s.flush()
            return row.id

    def activate_version(self, version_id: int) -> ParameterSet:
        with self.db.session() as s:
            row = s.get(StrategyVersionRow, version_id)
            if row is None:
                raise KeyError(f"Version {version_id} introuvable")
            params = ParameterSet.model_validate(row.params).validated()
            s.execute(update(StrategyVersionRow).where(StrategyVersionRow.status == "active")
                      .values(status="retired"))
            row.status = "active"
        return params

    def list_versions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._recent(StrategyVersionRow, StrategyVersionRow.id, limit)

    # -- Optimisation -----------------------------------------------------------------------------
    def start_run(self, trigger: str, model: str) -> int:
        with self.db.session() as s:
            row = OptimizationRunRow(started_at=utcnow(), trigger=trigger, model=model, status="running")
            s.add(row)
            s.flush()
            return row.id

    def finish_run(self, run_id: int, **fields: Any) -> None:
        with self.db.session() as s:
            s.execute(update(OptimizationRunRow).where(OptimizationRunRow.id == run_id)
                      .values(finished_at=utcnow(), **fields))

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        return self._recent(OptimizationRunRow, OptimizationRunRow.id, limit)

    # -- Décisions IA, coûts et briefings -------------------------------------------------------------
    def save_decision(self, **fields: Any) -> int:
        with self.db.session() as s:
            row = AIDecisionRow(timestamp=utcnow(), **fields)
            s.add(row)
            s.flush()
            return row.id

    def update_decision(self, decision_id: int, **fields: Any) -> None:
        with self.db.session() as s:
            s.execute(update(AIDecisionRow).where(AIDecisionRow.id == decision_id).values(**fields))

    def record_decision_outcome(self, trade: dict[str, Any]) -> None:
        if trade.get("decision_id") is None:
            return
        self.update_decision(int(trade["decision_id"]), outcome_net_pnl=trade["net_pnl"],
                             outcome_r=trade["r_multiple"], outcome_reason=trade["reason"],
                             closed_at=_dt(trade["exit_time"]))

    def recent_decisions(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._recent(AIDecisionRow, AIDecisionRow.id, limit)
        for row in rows:
            row.pop("context", None)
        return rows

    def ai_track_record(self, journal_size: int = 8) -> dict[str, Any]:
        """Calibration par niveau de confiance et derniers trades décidés par l'IA (mémoire)."""
        with self.db.session() as s:
            rows = s.scalars(select(AIDecisionRow).where(AIDecisionRow.outcome_r.is_not(None))
                             .order_by(AIDecisionRow.id)).all()
        buckets: dict[str, list[float]] = {}
        for row in rows:
            conf = row.confidence or 0.0
            key = "0.80+" if conf >= 0.8 else "0.70-0.80" if conf >= 0.7 else "<0.70"
            buckets.setdefault(key, []).append(row.outcome_r or 0.0)
        calibration = {key: {"trades": len(rs), "taux_reussite": round(sum(r > 0 for r in rs) / len(rs), 3),
                             "r_moyen": round(sum(rs) / len(rs), 3)} for key, rs in buckets.items()}
        journal = [{"date": ensure_utc(r.timestamp).strftime("%Y-%m-%d %H:%M"), "symbole": r.symbol,
                    "action": r.action, "confiance": r.confidence, "these": (r.thesis or "")[:240],
                    "resultat_r": round(r.outcome_r or 0.0, 2), "sortie": r.outcome_reason}
                   for r in rows[-journal_size:]]
        return {"trades_ia_clotures": len(rows), "calibration_par_confiance": calibration,
                "derniers_trades_ia": journal}

    def insert_usage(self, record: Any) -> None:
        with self.db.session() as s:
            s.add(AIUsageRow(timestamp=record.timestamp, purpose=record.purpose, model=record.model,
                             input_tokens=record.input_tokens, output_tokens=record.output_tokens,
                             cache_read_tokens=record.cache_read_tokens,
                             cache_write_tokens=record.cache_write_tokens, web_searches=record.web_searches,
                             cost_usd=record.cost_usd))

    def ai_spent_today(self) -> float:
        today = datetime.now(UTC).date()
        with self.db.session() as s:
            return float(s.scalar(select(func.coalesce(func.sum(AIUsageRow.cost_usd), 0.0)).where(
                AIUsageRow.timestamp >= datetime(today.year, today.month, today.day, tzinfo=UTC))) or 0.0)

    def save_briefing(self, briefing: Any) -> None:
        with self.db.session() as s:
            s.add(MarketBriefingRow(generated_at=briefing.generated_at, model=briefing.model,
                                    risk_level=briefing.risk_level, sentiment=briefing.overall_sentiment,
                                    summary=briefing.summary, data=briefing.model_dump(mode="json")))

    def latest_briefing(self) -> dict[str, Any] | None:
        with self.db.session() as s:
            row = s.scalars(select(MarketBriefingRow).order_by(MarketBriefingRow.id.desc()).limit(1)).first()
        return None if row is None else row.data

    # -- Remise à zéro ----------------------------------------------------------------------------
    def reset_paper_account(self) -> None:
        with self.db.session() as s:
            for model in (OrderRow, PositionRow, TradeRow, EquitySnapshotRow, SignalRow, BotEventRow,
                          BotStateRow, AIDecisionRow):
                s.execute(delete(model))
        logger.warning("Compte paper réinitialisé (ordres, positions, trades, equity, signaux, événements).")


def _position_from_row(row: PositionRow) -> Position:
    return Position(
        id=row.id, seq=row.seq, symbol=row.symbol, direction=Direction(row.direction),
        quantity=row.quantity, entry_price=row.entry_price, entry_time=ensure_utc(row.entry_time),
        stop_loss=row.stop_loss, take_profit=row.take_profit, initial_stop_loss=row.initial_stop_loss,
        entry_fee=row.entry_fee, entry_slippage=row.entry_slippage, strategy=row.strategy,
        entry_order_id=row.entry_order_id, entry_reason=row.entry_reason,
        trailing_active=row.trailing_active, initial_quantity=row.initial_quantity or 0.0,
        tp1_price=row.tp1_price, tp1_fraction=row.tp1_fraction or 0.0, tp1_done=bool(row.tp1_done),
        breakeven_at_r=row.breakeven_at_r or 0.0, breakeven_done=bool(row.breakeven_done),
        time_stop_at=ensure_utc(row.time_stop_at) if row.time_stop_at else None,
        stop_reason=row.stop_reason or "Stop-loss touché", realized_pnl=row.realized_pnl or 0.0,
        exit_fees=row.exit_fees or 0.0, exit_slippage=row.exit_slippage or 0.0,
        exit_notional=row.exit_notional or 0.0, best_price=row.best_price or 0.0,
        decision_id=row.decision_id, confidence=row.confidence,
    )


def _trade_from_row(row: TradeRow) -> Trade:
    return Trade(
        id=row.id, seq=row.seq, position_id=row.position_id, symbol=row.symbol,
        direction=Direction(row.direction), side=Side(row.side), order_type=OrderType(row.order_type),
        entry_time=ensure_utc(row.entry_time), exit_time=ensure_utc(row.timestamp),
        entry_price=row.entry_price, exit_price=row.exit_price, quantity=row.quantity,
        stop_loss=row.stop_loss, take_profit=row.take_profit, fees=row.fees, slippage=row.slippage,
        gross_pnl=row.gross_pnl, net_pnl=row.net_pnl, return_pct=row.return_pct,
        r_multiple=row.r_multiple, strategy=row.strategy, reason=row.reason,
        entry_reason=row.entry_reason, decision_id=row.decision_id, confidence=row.confidence,
        max_favorable_r=row.max_favorable_r or 0.0,
    )


class DatabaseRecorder:
    """Abonné du bus : enregistre TOUS les ordres, positions, trades, equity, signaux et événements."""

    def __init__(self, repository: TradingRepository) -> None:
        self.repo = repository

    def __call__(self, event: EngineEvent) -> None:
        data = event.payload
        match event.type:
            case EventType.ORDER:
                self.repo.upsert_order(data)
            case EventType.POSITION_OPENED | EventType.POSITION_UPDATED:
                self.repo.upsert_position(data, "OPEN")
            case EventType.POSITION_CLOSED:
                self.repo.upsert_position(data, "CLOSED")
            case EventType.TRADE:
                self.repo.insert_trade(data)
                self.repo.record_decision_outcome(data)
            case EventType.EQUITY:
                self.repo.insert_equity(data)
            case EventType.SIGNAL:
                self.repo.insert_signal(data)
            case EventType.RISK:
                level = "CRITICAL" if data["kind"] == "max_drawdown" else "WARNING"
                self.repo.insert_event(level, "risk", data["message"], data)
                if data["kind"] == "max_drawdown":
                    self.repo.set_state("risk_halt", {"halted": True, "reason": data["message"]})
            case EventType.BOT | EventType.OPTIMIZATION:
                self.repo.insert_event(data.get("level", "INFO"), event.type.value,
                                       data.get("message", ""), data)
            case _:
                pass
