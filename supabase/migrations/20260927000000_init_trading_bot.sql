-- Schéma du bot de paper trading pour Supabase (PostgreSQL).
-- Généré depuis app/database/models.py (SQLAlchemy) : garder les deux synchronisés.
-- À exécuter une fois dans Supabase > SQL Editor (ou `supabase db push`).
--
-- Sécurité : RLS est activé SANS politique sur chaque table. Les clés publiques
-- (anon / publishable) n'ont donc aucun accès via l'API REST ; seul le bot, connecté
-- en direct à PostgreSQL avec DATABASE_URL, lit et écrit ces tables.


CREATE TABLE IF NOT EXISTS bot_events (
    id SERIAL NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    level VARCHAR(10) NOT NULL, 
    event_type VARCHAR(32) NOT NULL, 
    message TEXT NOT NULL, 
    data JSONB, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_bot_events_event_type ON bot_events (event_type);

CREATE INDEX IF NOT EXISTS ix_bot_events_timestamp ON bot_events (timestamp);

CREATE TABLE IF NOT EXISTS bot_state (
    key VARCHAR(64) NOT NULL, 
    value JSONB NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (key)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id SERIAL NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    balance FLOAT NOT NULL, 
    equity FLOAT NOT NULL, 
    unrealized_pnl FLOAT NOT NULL, 
    drawdown FLOAT NOT NULL, 
    peak_equity FLOAT NOT NULL, 
    open_positions INTEGER NOT NULL, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_equity_snapshots_timestamp ON equity_snapshots (timestamp);

CREATE TABLE IF NOT EXISTS optimization_runs (
    id SERIAL NOT NULL, 
    started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    finished_at TIMESTAMP WITH TIME ZONE, 
    trigger VARCHAR(16) NOT NULL, 
    model VARCHAR(64) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    summary TEXT NOT NULL, 
    analysis TEXT NOT NULL, 
    baseline JSONB, 
    candidates JSONB, 
    error TEXT, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_optimization_runs_started_at ON optimization_runs (started_at);

CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(32) NOT NULL, 
    seq INTEGER NOT NULL, 
    symbol VARCHAR(32) NOT NULL, 
    side VARCHAR(8) NOT NULL, 
    order_type VARCHAR(16) NOT NULL, 
    intent VARCHAR(8) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    quantity FLOAT NOT NULL, 
    limit_price FLOAT, 
    reference_price FLOAT, 
    fill_price FLOAT, 
    stop_loss FLOAT, 
    take_profit FLOAT, 
    fees FLOAT NOT NULL, 
    slippage FLOAT NOT NULL, 
    position_id VARCHAR(32), 
    strategy VARCHAR(64) NOT NULL, 
    reason TEXT NOT NULL, 
    reject_reason TEXT, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    filled_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders (created_at);

CREATE INDEX IF NOT EXISTS ix_orders_seq ON orders (seq);

CREATE INDEX IF NOT EXISTS ix_orders_status ON orders (status);

CREATE INDEX IF NOT EXISTS ix_orders_symbol ON orders (symbol);

CREATE TABLE IF NOT EXISTS positions (
    id VARCHAR(32) NOT NULL, 
    seq INTEGER NOT NULL, 
    symbol VARCHAR(32) NOT NULL, 
    direction VARCHAR(8) NOT NULL, 
    status VARCHAR(8) NOT NULL, 
    quantity FLOAT NOT NULL, 
    entry_price FLOAT NOT NULL, 
    entry_time TIMESTAMP WITH TIME ZONE NOT NULL, 
    stop_loss FLOAT NOT NULL, 
    take_profit FLOAT NOT NULL, 
    initial_stop_loss FLOAT NOT NULL, 
    entry_fee FLOAT NOT NULL, 
    entry_slippage FLOAT NOT NULL, 
    trailing_active BOOLEAN NOT NULL, 
    strategy VARCHAR(64) NOT NULL, 
    entry_order_id VARCHAR(32) NOT NULL, 
    entry_reason TEXT NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_positions_seq ON positions (seq);

CREATE INDEX IF NOT EXISTS ix_positions_status ON positions (status);

CREATE INDEX IF NOT EXISTS ix_positions_symbol ON positions (symbol);

CREATE TABLE IF NOT EXISTS signals (
    id SERIAL NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    symbol VARCHAR(32) NOT NULL, 
    direction VARCHAR(8) NOT NULL, 
    price FLOAT, 
    atr FLOAT, 
    source VARCHAR(16) NOT NULL, 
    strategy VARCHAR(64) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    reason TEXT NOT NULL, 
    order_id VARCHAR(32), 
    payload JSONB, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_signals_symbol ON signals (symbol);

CREATE INDEX IF NOT EXISTS ix_signals_timestamp ON signals (timestamp);

CREATE TABLE IF NOT EXISTS strategy_versions (
    id SERIAL NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    source VARCHAR(16) NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    params JSONB NOT NULL, 
    metrics JSONB, 
    rationale TEXT NOT NULL, 
    optimization_run_id INTEGER, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_strategy_versions_status ON strategy_versions (status);

CREATE TABLE IF NOT EXISTS trades (
    id VARCHAR(32) NOT NULL, 
    seq INTEGER NOT NULL, 
    position_id VARCHAR(32) NOT NULL, 
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL, 
    symbol VARCHAR(32) NOT NULL, 
    direction VARCHAR(8) NOT NULL, 
    side VARCHAR(8) NOT NULL, 
    order_type VARCHAR(16) NOT NULL, 
    entry_time TIMESTAMP WITH TIME ZONE NOT NULL, 
    entry_price FLOAT NOT NULL, 
    exit_price FLOAT NOT NULL, 
    quantity FLOAT NOT NULL, 
    stop_loss FLOAT NOT NULL, 
    take_profit FLOAT NOT NULL, 
    fees FLOAT NOT NULL, 
    slippage FLOAT NOT NULL, 
    gross_pnl FLOAT NOT NULL, 
    net_pnl FLOAT NOT NULL, 
    return_pct FLOAT NOT NULL, 
    r_multiple FLOAT NOT NULL, 
    strategy VARCHAR(64) NOT NULL, 
    reason TEXT NOT NULL, 
    entry_reason TEXT NOT NULL, 
    PRIMARY KEY (id)
);

CREATE INDEX IF NOT EXISTS ix_trades_seq ON trades (seq);

CREATE INDEX IF NOT EXISTS ix_trades_symbol ON trades (symbol);

CREATE INDEX IF NOT EXISTS ix_trades_timestamp ON trades (timestamp);

ALTER TABLE bot_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE bot_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE equity_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE optimization_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE strategy_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE trades ENABLE ROW LEVEL SECURITY;
