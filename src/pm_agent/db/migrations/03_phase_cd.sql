-- ============================================================================
-- pm-agent — Phase C/D migrations (diagnostics, signals, sizing, transcripts)
-- Extends Phase A + B schema. Idempotent.
-- ============================================================================

-- ---------- Signals table (unified across all patterns) ----------
CREATE TABLE IF NOT EXISTS signals (
    id                  BIGSERIAL PRIMARY KEY,
    signal_id           TEXT UNIQUE NOT NULL,           -- 'arb:...', 'narrative:...', 'incumbent:...', 'creep:...'
    pattern             TEXT NOT NULL,                  -- 'arb'|'narrative_yes_fade'|'incumbent'|'pre_resolution_creep'
    market_id           TEXT NOT NULL REFERENCES markets(id),
    outcome_id          TEXT NOT NULL,
    side                TEXT NOT NULL,                  -- 'BUY'|'SELL'
    limit_price         NUMERIC(10,4) NOT NULL,
    size                NUMERIC(18,6) NOT NULL,
    signal_time         TIMESTAMPTZ NOT NULL,
    model_probability   NUMERIC(6,4),                  -- p_model (for #2/#3)
    market_probability  NUMERIC(6,4),                  -- market implied p
    edge                NUMERIC(6,4),                  -- p_model - market_p
    sizing_method       TEXT,                          -- 'fractional_kelly_0.15'|'paired_arb'|'fixed'
    confidence          NUMERIC(6,4),
    rationale           TEXT,
    group_id            BIGINT REFERENCES paper_order_groups(id),  -- for arb pairs
    created_at          TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_signals_pattern ON signals(pattern);
CREATE INDEX IF NOT EXISTS idx_signals_market ON signals(market_id);

-- Add pattern + signal linkage to paper_orders
ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS pattern TEXT,
    ADD COLUMN IF NOT EXISTS signal_id TEXT REFERENCES signals(signal_id),
    ADD COLUMN IF NOT EXISTS sizing_method TEXT,
    ADD COLUMN IF NOT EXISTS model_probability NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS market_probability NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS edge NUMERIC(6,4);

-- ---------- Pattern diagnostics (Phase C — adverse selection measurement) ----------
CREATE TABLE IF NOT EXISTS pattern_diagnostics (
    id                      BIGSERIAL PRIMARY KEY,
    pattern                 TEXT NOT NULL,
    signal_id               TEXT REFERENCES signals(signal_id),
    market_id               TEXT NOT NULL,
    naive_pnl               NUMERIC(12,6),
    latency_pnl_2s          NUMERIC(12,6),
    latency_pnl_10s         NUMERIC(12,6),
    latency_pnl_30s         NUMERIC(12,6),
    tape_confirmed_pnl      NUMERIC(12,6),
    conservative_pnl        NUMERIC(12,6),
    illusion_ratio          NUMERIC(10,4),             -- naive / max(abs(conservative), eps)
    adverse_selection_loss  NUMERIC(12,6),             -- naive - conservative
    fill_rate_by_mode       JSONB,                     -- {naive:0.9, latency:0.6, tape:0.3, conservative:0.15}
    quote_survival_rate     NUMERIC(6,4),
    slippage_bps            NUMERIC(10,2),
    time_to_resolution_sec  INT,
    bucket                  TEXT,                      -- '0-1h','1-6h','6-24h','24h+'
    created_at              TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_diag_pattern ON pattern_diagnostics(pattern);
CREATE INDEX IF NOT EXISTS idx_diag_bucket ON pattern_diagnostics(bucket);

-- ---------- Pattern #2: transcript sources + mention base rates ----------
CREATE TABLE IF NOT EXISTS transcript_sources (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL REFERENCES markets(id),
    speaker         TEXT,
    source_type     TEXT NOT NULL,           -- 'youtube_auto'|'official'|'cspan'|'rev'
    source_url      TEXT NOT NULL,
    transcript_text TEXT,
    fetched_at      TIMESTAMPTZ DEFAULT now(),
    reviewed        BOOLEAN DEFAULT FALSE,   -- human QA flag
    UNIQUE (market_id, source_url)
);

CREATE TABLE IF NOT EXISTS mention_base_rates (
    id              BIGSERIAL PRIMARY KEY,
    market_id       TEXT NOT NULL REFERENCES markets(id),
    phrase           TEXT NOT NULL,           -- target word/phrase
    transcript_count INT NOT NULL,
    mention_count    INT NOT NULL,
    base_rate        NUMERIC(6,4) NOT NULL,   -- mention_count / transcript_count
    confidence       NUMERIC(6,4),            -- based on sample size
    market_price     NUMERIC(6,4),            -- market YES price at calc time
    edge             NUMERIC(6,4),            -- base_rate - market_price (negative = fade YES)
    computed_at      TIMESTAMPTZ DEFAULT now()
);

-- ---------- Pattern #3: context validation (incumbent) ----------
CREATE TABLE IF NOT EXISTS context_validations (
    id                  BIGSERIAL PRIMARY KEY,
    market_id           TEXT NOT NULL REFERENCES markets(id),
    incumbent           TEXT,
    country             TEXT,
    is_oecd             BOOLEAN,
    approval_rating     NUMERIC(6,4),
    economy_context     TEXT,                  -- 'strong'|'weak'|'recession'
    scandal_severity    TEXT,                  -- 'none'|'minor'|'severe'
    base_rate_override  NUMERIC(6,4),         -- override default 0.68 if context breaks it
    validated_by        TEXT,                 -- 'llm'|'human'
    validation_json     JSONB,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- ---------- Diagnostic summary view (Phase C reporting) ----------
CREATE OR REPLACE VIEW pattern_diagnostic_summary AS
SELECT
    pattern,
    bucket,
    count(*) AS signals,
    avg(naive_pnl) AS avg_naive_pnl,
    avg(tape_confirmed_pnl) AS avg_tape_pnl,
    avg(conservative_pnl) AS avg_conservative_pnl,
    avg(illusion_ratio) AS avg_illusion_ratio,
    avg(adverse_selection_loss) AS avg_adverse_selection_loss,
    avg(slippage_bps) AS avg_slippage_bps
FROM pattern_diagnostics
GROUP BY pattern, bucket
ORDER BY pattern, bucket;
