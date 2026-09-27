"""Jeu de paramètres versionnable (stratégie + sorties), unité manipulée par l'optimiseur."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import Settings
from app.risk.risk_manager import ExitParams, RiskLimits
from app.strategy import STRATEGIES, Strategy


class ParameterSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_name: str = "ema_rsi"
    strategy: dict[str, Any] = Field(default_factory=dict)
    exits: ExitParams = Field(default_factory=ExitParams)

    @field_validator("strategy_name")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        if value not in STRATEGIES:
            raise ValueError(f"Stratégie inconnue : {value}")
        return value

    def build_strategy(self) -> Strategy:
        strategy_cls = STRATEGIES[self.strategy_name]
        return strategy_cls(strategy_cls.params_model(**self.strategy))

    def validated(self) -> ParameterSet:
        """Retourne une copie normalisée ; lève ``ValueError`` si les paramètres sont invalides."""
        strategy = self.build_strategy()
        return self.model_copy(update={"strategy": strategy.params.model_dump()})

    @classmethod
    def from_settings(cls, settings: Settings) -> ParameterSet:
        strategy = {
            "ema_fast": settings.ema_fast, "ema_slow": settings.ema_slow,
            "rsi_length": settings.rsi_length, "rsi_long_threshold": settings.rsi_long_threshold,
            "rsi_short_threshold": settings.rsi_short_threshold, "atr_length": settings.atr_length,
        } if settings.strategy == "ema_rsi" else {}
        return cls(
            strategy_name=settings.strategy,
            strategy=strategy,
            exits=ExitParams(
                stop_loss_atr_multiplier=settings.stop_loss_atr_multiplier,
                take_profit_risk_reward=settings.take_profit_risk_reward,
                trailing_stop_enabled=settings.trailing_stop_enabled,
                trailing_stop_atr_multiplier=settings.trailing_stop_atr_multiplier,
                trailing_activation_r=settings.trailing_activation_r,
                partial_take_profit_r=settings.partial_take_profit_r,
                partial_take_profit_fraction=settings.partial_take_profit_fraction,
                breakeven_at_r=settings.breakeven_at_r,
                time_stop_bars=settings.time_stop_bars,
            ),
        ).validated()


def risk_limits_from_settings(settings: Settings) -> RiskLimits:
    return RiskLimits(
        risk_per_trade=settings.risk_per_trade,
        hard_max_risk_per_trade=settings.hard_max_risk_per_trade,
        min_risk_per_trade=settings.min_risk_per_trade,
        max_open_positions=settings.max_open_positions,
        max_portfolio_risk=settings.max_portfolio_risk,
        max_correlated_risk=settings.max_correlated_risk,
        max_daily_loss=settings.max_daily_loss,
        max_weekly_loss=settings.max_weekly_loss,
        max_drawdown=settings.max_drawdown,
        max_consecutive_losses=settings.max_consecutive_losses,
        cooldown_hours=settings.cooldown_hours,
        min_reward_risk=settings.min_reward_risk,
        min_stop_atr=settings.min_stop_atr,
        max_stop_atr=settings.max_stop_atr,
        min_confidence=settings.ai_min_confidence,
        kelly_multiplier=settings.kelly_multiplier,
        kelly_min_trades=settings.kelly_min_trades,
        max_position_pct=settings.max_position_pct,
        fee_rate=settings.trading_fees,
        slippage_rate=settings.slippage,
        min_qty=settings.min_qty,
        qty_step=settings.qty_step,
        min_notional=settings.min_notional,
        close_positions_on_max_drawdown=settings.close_positions_on_max_drawdown,
    )
