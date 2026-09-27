"""Modèles SQLAlchemy — portables SQLite / PostgreSQL (Supabase).

Le schéma PostgreSQL équivalent est versionné dans ``supabase/migrations/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")
UtcDateTime = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


class OrderRow(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    intent: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)
    quantity: Mapped[float] = mapped_column(Float)
    limit_price: Mapped[float | None] = mapped_column(Float)
    reference_price: Mapped[float | None] = mapped_column(Float)
    fill_price: Mapped[float | None] = mapped_column(Float)
    stop_loss: Mapped[float | None] = mapped_column(Float)
    take_profit: Mapped[float | None] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    position_id: Mapped[str | None] = mapped_column(String(32))
    strategy: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    filled_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class PositionRow(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(8), index=True)  # OPEN | CLOSED
    quantity: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_time: Mapped[datetime] = mapped_column(UtcDateTime)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    initial_stop_loss: Mapped[float] = mapped_column(Float)
    entry_fee: Mapped[float] = mapped_column(Float)
    entry_slippage: Mapped[float] = mapped_column(Float)
    trailing_active: Mapped[bool] = mapped_column(Boolean, default=False)
    strategy: Mapped[str] = mapped_column(String(64), default="")
    entry_order_id: Mapped[str] = mapped_column(String(32))
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # Colonnes ajoutées en v2 (nullables pour la migration automatique des bases existantes)
    initial_quantity: Mapped[float | None] = mapped_column(Float)
    tp1_price: Mapped[float | None] = mapped_column(Float)
    tp1_fraction: Mapped[float | None] = mapped_column(Float)
    tp1_done: Mapped[bool | None] = mapped_column(Boolean)
    breakeven_at_r: Mapped[float | None] = mapped_column(Float)
    breakeven_done: Mapped[bool | None] = mapped_column(Boolean)
    time_stop_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    stop_reason: Mapped[str | None] = mapped_column(Text)
    realized_pnl: Mapped[float | None] = mapped_column(Float)
    exit_fees: Mapped[float | None] = mapped_column(Float)
    exit_slippage: Mapped[float | None] = mapped_column(Float)
    exit_notional: Mapped[float | None] = mapped_column(Float)
    best_price: Mapped[float | None] = mapped_column(Float)
    decision_id: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    fx_rate: Mapped[float | None] = mapped_column(Float)


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    position_id: Mapped[str] = mapped_column(String(32))
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)  # heure de sortie
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    entry_time: Mapped[datetime] = mapped_column(UtcDateTime)
    entry_price: Mapped[float] = mapped_column(Float)
    exit_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    fees: Mapped[float] = mapped_column(Float)
    slippage: Mapped[float] = mapped_column(Float)
    gross_pnl: Mapped[float] = mapped_column(Float)
    net_pnl: Mapped[float] = mapped_column(Float)
    return_pct: Mapped[float] = mapped_column(Float)
    r_multiple: Mapped[float] = mapped_column(Float)
    strategy: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    entry_reason: Mapped[str] = mapped_column(Text, default="")
    decision_id: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float | None] = mapped_column(Float)
    max_favorable_r: Mapped[float | None] = mapped_column(Float)


class EquitySnapshotRow(Base):
    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    balance: Mapped[float] = mapped_column(Float)
    equity: Mapped[float] = mapped_column(Float)
    unrealized_pnl: Mapped[float] = mapped_column(Float)
    drawdown: Mapped[float] = mapped_column(Float)
    peak_equity: Mapped[float] = mapped_column(Float)
    open_positions: Mapped[int] = mapped_column(Integer)


class SignalRow(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    price: Mapped[float | None] = mapped_column(Float)
    atr: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(16))
    strategy: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16))  # accepted | rejected | ignored | invalid
    reason: Mapped[str] = mapped_column(Text, default="")
    order_id: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any] | None] = mapped_column(JsonType)


class BotEventRow(Base):
    __tablename__ = "bot_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    level: Mapped[str] = mapped_column(String(10))
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JsonType)


class StrategyVersionRow(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime)
    source: Mapped[str] = mapped_column(String(16))  # config | ai | manual
    status: Mapped[str] = mapped_column(String(16), index=True)  # active | proposed | retired
    params: Mapped[dict[str, Any]] = mapped_column(JsonType)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    rationale: Mapped[str] = mapped_column(Text, default="")
    optimization_run_id: Mapped[int | None] = mapped_column(Integer)


class OptimizationRunRow(Base):
    __tablename__ = "optimization_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    trigger: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # applied | proposed | no_improvement | error
    summary: Mapped[str] = mapped_column(Text, default="")
    analysis: Mapped[str] = mapped_column(Text, default="")
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    candidates: Mapped[list[Any] | None] = mapped_column(JsonType)
    error: Mapped[str | None] = mapped_column(Text)


class AIDecisionRow(Base):
    """Journal de chaque décision de Claude, de son exécution et de son résultat."""

    __tablename__ = "ai_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), index=True)
    status_reason: Mapped[str] = mapped_column(Text, default="")
    market_regime: Mapped[str] = mapped_column(Text, default="")
    thesis: Mapped[str] = mapped_column(Text, default="")
    invalidation: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    context: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    order_id: Mapped[str | None] = mapped_column(String(32))
    outcome_net_pnl: Mapped[float | None] = mapped_column(Float)
    outcome_r: Mapped[float | None] = mapped_column(Float)
    outcome_reason: Mapped[str | None] = mapped_column(Text)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    lesson: Mapped[dict[str, Any] | None] = mapped_column(JsonType)


class AIUsageRow(Base):
    __tablename__ = "ai_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    purpose: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer)
    cache_write_tokens: Mapped[int] = mapped_column(Integer)
    web_searches: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[float] = mapped_column(Float)


class MarketBriefingRow(Base):
    __tablename__ = "market_briefings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(UtcDateTime, index=True)
    model: Mapped[str] = mapped_column(String(64))
    risk_level: Mapped[str] = mapped_column(String(16))
    sentiment: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JsonType)


class BotStateRow(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JsonType)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime)
