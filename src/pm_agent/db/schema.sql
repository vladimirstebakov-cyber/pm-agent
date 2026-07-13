-- ============================================================================
-- pm-agent — TimescaleDB schema (Phase A)
-- Principles: append-only, point-in-time, no leakage, replay-honest.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------- Venues ----------
CREATE TABLE IF NOT EXISTS venues (
    id            TEXT PRIMARY KEY,           -- 'polymarket', 'kalshi'
    name          TEXT NOT NULL,
    base_currency TEXT NOT NULL,              -- 'USDC', 'USD'
    created_at    TIMESTAMPTZ DEFAULT now()
);

INSERT INTO venues(id, name, base_currency) VALUES
    ('polymarket', 'Polymarket', 'USDC'),
    ('kalshi', 'Kalshi', 'USD')
ON CONFLICT (id) DO NOTHING;

-- ---------- Events ----------
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,         -- venue-native event id (prefixed)
    venue_id        TEXT NOT NULL REFERENCES venues(id),
    venue_event_id  TEXT NOT NULL,
    title           TEXT,
    category        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (venue_id, venue_event_id)
);

-- ---------- Markets ----------
CREATE TABLE IF NOT EXISTS markets (
    id                TEXT PRIMARY KEY,        -- venue-native market id (prefixed: 'poly:<cid>' / 'kalshi:<ticker>')
    venue_id          TEXT NOT NULL REFERENCES venues(id),
    venue_market_id   TEXT NOT NULL,
    event_id          TEXT REFERENCES events(id),
    title             TEXT,
    slug             TEXT,
    category          TEXT,
    status            TEXT,                    -- 'active','closed','archived','resolved'
    open_time         TIMESTAMPTZ,
    close_time        TIMESTAMPTZ,
    resolve_time      TIMESTAMPTZ,
    first_seen_at     TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now(),
    UNIQUE (venue_id, venue_market_id)
);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_close_time ON markets(close_time);

-- Market version history (append-only raw payload for reproducibility)
CREATE TABLE IF NOT EXISTS market_versions (
    id            BIGSERIAL PRIMARY KEY,
    market_id     TEXT NOT NULL REFERENCES markets(id),
    observed_at   TIMESTAMPTZ NOT NULL,
    payload_hash  TEXT NOT NULL,
    raw_payload   JSONB NOT NULL,
    UNIQUE (market_id, observed_at, payload_hash)
);
CREATE INDEX IF NOT EXISTS idx_market_versions_market_time ON market_versions(market_id, observed_at);

-- ---------- Resolution criteria store (critical for arb & narrative) ----------
CREATE TABLE IF NOT EXISTS market_rules (
    id                  BIGSERIAL PRIMARY KEY,
    market_id           TEXT NOT NULL REFERENCES markets(id),
    observed_at        TIMESTAMPTZ NOT NULL,
    rules_text          TEXT,
    resolution_source   TEXT,
    cutoff_time         TIMESTAMPTZ,
    timezone            TEXT,
    dispute_window_sec  INT,
    settlement_logic    TEXT,
    rules_hash          TEXT,
    raw_payload         JSONB,
    UNIQUE (market_id, observed_at, rules_hash)
);
CREATE INDEX IF NOT EXISTS idx_rules_market_time ON market_rules(market_id, observed_at);

-- ---------- Outcomes / tokens ----------
CREATE TABLE IF NOT EXISTS outcomes (
    id              TEXT PRIMARY KEY,          -- 'poly:<cid>:YES' / 'kalshi:<ticker>:Y'
    market_id       TEXT NOT NULL REFERENCES markets(id),
    outcome_name    TEXT NOT NULL,             -- 'YES','NO', or team name
    venue_token_id  TEXT,                      -- CLOB token_id (Polymarket) or side code (Kalshi)
    side            TEXT,                       -- 'YES'/'NO'
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (market_id, outcome_name)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_token ON outcomes(venue_token_id);

-- ---------- Order book snapshots (hypertable) ----------
CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id              BIGSERIAL,
    venue_id        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    outcome_id      TEXT NOT NULL,
    ts_exchange     TIMESTAMPTZ,               -- exchange timestamp (if provided)
    ts_collected    TIMESTAMPTZ NOT NULL,      -- when we saw it (decision boundary)
    best_bid        NUMERIC(10,4),
    best_ask        NUMERIC(10,4),
    mid             NUMERIC(10,4),
    spread          NUMERIC(10,4),
    depth_top_json  JSONB,                      -- [{side,level,price,size},...]
    payload_hash    TEXT,
    raw_payload     JSONB,
    PRIMARY KEY (id, ts_collected)
);
CREATE INDEX IF NOT EXISTS idx_ob_snap_market_ts ON orderbook_snapshots(market_id, outcome_id, ts_collected DESC);

SELECT create_hypertable('orderbook_snapshots', 'ts_collected',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

-- ---------- Trade prints (hypertable) ----------
CREATE TABLE IF NOT EXISTS trade_prints (
    id            BIGSERIAL,
    venue_id      TEXT NOT NULL,
    trade_id      TEXT,                        -- venue-native trade id (for tape-confirmed fills)
    market_id     TEXT NOT NULL,
    outcome_id    TEXT NOT NULL,
    ts_exchange   TIMESTAMPTZ,
    ts_collected  TIMESTAMPTZ NOT NULL,
    price         NUMERIC(10,4),
    size          NUMERIC(18,6),
    side          TEXT,                        -- 'BUY'/'SELL' or taker side
    raw_payload   JSONB,
    PRIMARY KEY (id, ts_collected)
);
CREATE INDEX IF NOT EXISTS idx_trades_market_ts ON trade_prints(market_id, outcome_id, ts_exchange);
CREATE INDEX IF NOT EXISTS idx_trades_price_time ON trade_prints(market_id, price, ts_exchange);

SELECT create_hypertable('trade_prints', 'ts_collected',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

-- ---------- Resolutions ----------
CREATE TABLE IF NOT EXISTS resolutions (
    market_id            TEXT PRIMARY KEY,
    outcome_id           TEXT,
    resolved_value       TEXT,                  -- 'YES'/'NO'/winner
    resolved_at          TIMESTAMPTZ,           -- when event actually resolved
    resolution_known_at  TIMESTAMPTZ,          -- when market officially announced (>= resolved_at)
    source_url           TEXT,
    raw_payload          JSONB
);

-- ---------- Collection runs (provenance) ----------
CREATE TABLE IF NOT EXISTS collection_runs (
    id            BIGSERIAL PRIMARY KEY,
    collector    TEXT NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL,
    finished_at  TIMESTAMPTZ,
    status       TEXT,
    endpoint     TEXT,
    params       JSONB,
    response_hash TEXT,
    error        TEXT
);

-- ============================================================================
-- Replay / paper trading (decision-grade honesty)
-- ============================================================================

CREATE TABLE IF NOT EXISTS replay_runs (
    id          BIGSERIAL PRIMARY KEY,
    started_at  TIMESTAMPTZ NOT NULL,
    config_json JSONB NOT NULL,
    data_cutoff TIMESTAMPTZ NOT NULL,           -- replay can only see data <= cutoff
    fill_mode   TEXT NOT NULL,                  -- naive|latency_adjusted|tape_confirmed|conservative
    result_json JSONB
);

CREATE TABLE IF NOT EXISTS paper_orders (
    id              BIGSERIAL PRIMARY KEY,
    replay_run_id   BIGINT REFERENCES replay_runs(id),
    signal_time     TIMESTAMPTZ NOT NULL,       -- when scanner produced signal
    decision_time   TIMESTAMPTZ NOT NULL,       -- when agent decided (>= signal_time)
    venue_id        TEXT NOT NULL,
    market_id       TEXT NOT NULL,
    outcome_id      TEXT NOT NULL,
    side            TEXT NOT NULL,              -- 'BUY'/'SELL'
    limit_price     NUMERIC(10,4),
    size            NUMERIC(18,6),
    fill_mode       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'  -- open|filled|cancelled|rejected
);
CREATE INDEX IF NOT EXISTS idx_paper_orders_run ON paper_orders(replay_run_id);

CREATE TABLE IF NOT EXISTS paper_fills (
    id                  BIGSERIAL PRIMARY KEY,
    paper_order_id      BIGINT REFERENCES paper_orders(id),
    simulated_fill_time TIMESTAMPTZ NOT NULL,
    fill_price          NUMERIC(10,4),
    fill_size           NUMERIC(18,6),
    fill_reason         TEXT,                   -- 'naive'|'latency'|'tape_confirmed'|'conservative_reject'
    tape_trade_id       TEXT,                   -- reference to real trade_prints.trade_id if tape-confirmed
    notes               TEXT
);

-- ============================================================================
-- No-leakage invariants (enforced in code, documented here):
--  1. Replay uses ONLY data with ts_collected <= decision_time.
--  2. resolutions.resolved_at NOT visible until resolution_known_at.
--  3. market_rules version WHERE observed_at <= decision_time.
--  4. Snapshots are append-only; never UPDATE in place.
-- ============================================================================
