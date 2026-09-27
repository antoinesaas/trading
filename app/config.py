"""Configuration centralisée, chargée depuis ``.env`` (jamais de secret dans le code)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.market.timeframes import TIMEFRAME_SECONDS
from app.safety import assert_paper_only

INSECURE_SECRETS = frozenset({"", "change_me", "changeme", "secret", "password", "test"})
MIN_SECRET_LENGTH = 16


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- Sécurité -------------------------------------------------------------
    paper_only: bool = True
    trading_mode: str = "paper"
    webhook_secret: SecretStr = SecretStr("")
    dashboard_token: SecretStr = SecretStr("")

    # --- Capital et risque (limites NON modifiables par l'IA) ------------------
    initial_capital: float = Field(10_000.0, gt=0)
    account_currency: str = "USDT"
    risk_per_trade: float = Field(0.01, gt=0, le=0.05)
    hard_max_risk_per_trade: float = Field(0.02, gt=0, le=0.05)
    min_risk_per_trade: float = Field(0.0025, gt=0, le=0.05)
    max_open_positions: int = Field(3, ge=1, le=50)
    max_portfolio_risk: float = Field(0.05, gt=0, le=0.3)
    max_correlated_risk: float = Field(0.03, gt=0, le=0.3)
    max_daily_loss: float = Field(0.03, gt=0, le=0.5)
    max_weekly_loss: float = Field(0.06, gt=0, le=0.5)
    max_drawdown: float = Field(0.10, gt=0, le=0.9)
    max_consecutive_losses: int = Field(4, ge=1, le=50)
    cooldown_hours: float = Field(6.0, ge=0, le=168)
    min_reward_risk: float = Field(1.5, ge=0.5, le=10)
    min_stop_atr: float = Field(0.5, gt=0, le=10)
    max_stop_atr: float = Field(6.0, gt=0, le=50)
    kelly_multiplier: float = Field(0.25, ge=0, le=1)
    kelly_min_trades: int = Field(20, ge=5, le=1_000)
    max_spread_pct: float = Field(0.002, gt=0, le=0.05)
    trade_weekends: bool = True
    no_trade_hours_utc: str = ""
    max_position_pct: float = Field(1.0, gt=0, le=1.0)
    close_positions_on_max_drawdown: bool = True
    slippage: float = Field(0.0005, ge=0, le=0.05)
    trading_fees: float = Field(0.001, ge=0, le=0.01)
    min_qty: float = Field(0.0001, gt=0)
    qty_step: float = Field(0.0001, gt=0)
    min_notional: float = Field(10.0, ge=0)

    # --- Sorties (valeurs initiales, ajustables par l'optimiseur) -------------
    stop_loss_atr_multiplier: float = Field(2.0, gt=0, le=20)
    take_profit_risk_reward: float = Field(2.0, gt=0, le=20)
    trailing_stop_enabled: bool = False
    trailing_stop_atr_multiplier: float = Field(2.0, gt=0, le=20)
    trailing_activation_r: float = Field(1.0, ge=0, le=20)
    partial_take_profit_r: float = Field(0.0, ge=0, le=10)
    partial_take_profit_fraction: float = Field(0.5, gt=0, lt=1)
    breakeven_at_r: float = Field(0.0, ge=0, le=10)
    time_stop_bars: int = Field(0, ge=0, le=1_000)

    # --- Scanner / stratégie de règles ------------------------------------------
    strategy: str = "ema_rsi"

    # --- Stratégie EMA/RSI (valeurs initiales) --------------------------------
    ema_fast: int = Field(20, ge=2, le=500)
    ema_slow: int = Field(50, ge=3, le=500)
    rsi_length: int = Field(14, ge=2, le=100)
    rsi_long_threshold: float = Field(50.0, gt=0, lt=100)
    rsi_short_threshold: float = Field(50.0, gt=0, lt=100)
    atr_length: int = Field(14, ge=2, le=100)

    # --- Marché --------------------------------------------------------------
    symbols: str = "BTCUSDT,ETHUSDT,SOLUSDT"
    timeframe: str = "4h"
    context_timeframes: str = "1h,4h,1d"
    binance_futures_url: str = "https://fapi.binance.com"
    fear_greed_url: str = "https://api.alternative.me"
    market_data_provider: Literal["binance", "csv"] = "binance"
    binance_base_url: str = "https://data-api.binance.vision"
    csv_data_dir: Path = Path("data")
    poll_interval_seconds: float = Field(5.0, ge=1, le=3_600)
    lookback_bars: int = Field(300, ge=100, le=1_000)
    signal_source: Literal["internal", "tradingview", "both"] = "internal"
    bot_auto_start: bool = False

    # --- Webhook TradingView --------------------------------------------------
    webhook_max_signal_age_seconds: int = Field(120, ge=5, le=3_600)
    webhook_max_future_skew_seconds: int = Field(30, ge=0, le=300)
    webhook_max_price_deviation: float = Field(0.02, gt=0, le=0.5)
    webhook_rate_limit_per_minute: int = Field(30, ge=1, le=1_000)
    tradingview_ip_allowlist: str = ""

    # --- Base de données, serveur, logs ---------------------------------------
    database_url: str = "sqlite:///data/trading_bot.db"
    host: str = "127.0.0.1"
    port: int = Field(8000, ge=1, le=65_535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: Path = Path("logs")
    reports_dir: Path = Path("reports")

    # --- Décisions par Claude -----------------------------------------------------
    anthropic_api_key: SecretStr = SecretStr("")
    decision_mode: Literal["ai", "rules"] = "ai"
    ai_decision_model: str = "claude-opus-5-5"
    ai_decision_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    ai_review_model: str = "claude-sonnet-5"
    ai_briefing_model: str = "claude-sonnet-5"
    ai_briefing_interval_minutes: int = Field(240, ge=30, le=1_440)
    ai_briefing_max_searches: int = Field(5, ge=1, le=20)
    ai_min_confidence: float = Field(0.65, ge=0.5, le=0.99)
    ai_min_scan_score: float = Field(50.0, ge=0, le=100)
    ai_review_interval_minutes: int = Field(120, ge=15, le=1_440)
    ai_daily_budget_usd: float = Field(5.0, ge=0, le=1_000)
    ai_max_calls_per_hour: int = Field(20, ge=1, le=500)
    news_blackout_minutes: int = Field(30, ge=0, le=240)

    # --- Optimiseur IA (Claude) ----------------------------------------------
    ai_optimizer_enabled: bool = False
    ai_model: str = "claude-opus-5-5"
    ai_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    ai_optimizer_interval_hours: float = Field(24.0, ge=1, le=24 * 30)
    ai_auto_apply: bool = True
    ai_candidates: int = Field(3, ge=1, le=8)
    ai_train_bars: int = Field(1_500, ge=300, le=20_000)
    ai_validation_bars: int = Field(500, ge=100, le=10_000)
    ai_min_trades: int = Field(8, ge=1, le=1_000)
    ai_min_improvement: float = Field(0.5, ge=0, le=100)

    @field_validator("timeframe")
    @classmethod
    def _check_timeframe(cls, value: str) -> str:
        if value not in TIMEFRAME_SECONDS:
            raise ValueError(f"TIMEFRAME doit être l'un de {', '.join(TIMEFRAME_SECONDS)}")
        return value

    @field_validator("symbols")
    @classmethod
    def _check_symbols(cls, value: str) -> str:
        symbols = [s.strip().upper() for s in value.split(",") if s.strip()]
        if not symbols:
            raise ValueError("SYMBOLS ne peut pas être vide")
        return ",".join(dict.fromkeys(symbols))

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        # Lève LiveTradingDisabledError (non capturée par pydantic) si live demandé.
        assert_paper_only(self)
        if self.ema_fast >= self.ema_slow:
            raise ValueError("EMA_FAST doit être strictement inférieure à EMA_SLOW")
        if self.min_qty < self.qty_step:
            raise ValueError("MIN_QTY doit être >= QTY_STEP")
        if not self.min_risk_per_trade <= self.risk_per_trade <= self.hard_max_risk_per_trade:
            raise ValueError("Il faut MIN_RISK_PER_TRADE <= RISK_PER_TRADE <= HARD_MAX_RISK_PER_TRADE")
        if self.min_stop_atr >= self.max_stop_atr:
            raise ValueError("MIN_STOP_ATR doit être < MAX_STOP_ATR")
        return self

    @property
    def symbol_list(self) -> list[str]:
        return self.symbols.split(",")

    @property
    def context_timeframe_list(self) -> list[str]:
        return [tf.strip() for tf in self.context_timeframes.split(",") if tf.strip() in TIMEFRAME_SECONDS]

    @property
    def no_trade_hours(self) -> frozenset[int]:
        hours: set[int] = set()
        for part in filter(None, (p.strip() for p in self.no_trade_hours_utc.split(","))):
            start, _, end = part.partition("-")
            hours.update(range(int(start), int(end or start) + 1))
        return frozenset(h for h in hours if 0 <= h <= 23)

    @property
    def ai_decisions_enabled(self) -> bool:
        return self.decision_mode == "ai" and self.ai_available

    @property
    def ip_allowlist(self) -> list[str]:
        return [ip.strip() for ip in self.tradingview_ip_allowlist.split(",") if ip.strip()]

    @property
    def webhook_secret_is_secure(self) -> bool:
        return _is_secure(self.webhook_secret)

    @property
    def dashboard_token_is_secure(self) -> bool:
        return _is_secure(self.dashboard_token)

    @property
    def ai_available(self) -> bool:
        return bool(self.anthropic_api_key.get_secret_value())


def _is_secure(secret: SecretStr) -> bool:
    value = secret.get_secret_value()
    return value.lower() not in INSECURE_SECRETS and len(value) >= MIN_SECRET_LENGTH


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
