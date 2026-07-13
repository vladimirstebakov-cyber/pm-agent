-- ============================================================================
-- pm-agent — Phase B migrations (contract matching + arb groups + fee schedule)
-- Extends Phase A schema. Idempotent.
-- ============================================================================

-- ---------- Contract matching (Polymarket <-> Kalshi candidate pairs) ----------
CREATE TABLE IF NOT EXISTS matched_pairs (
    id                  BIGSERIAL PRIMARY KEY,
    polymarket_market_id TEXT REFERENCES markets(id),
    kalshi_market_id     TEXT REFERENCES markets(id),
    title_similarity     FLOAT,
    candidate_status     TEXT NOT NULL DEFAULT 'candidate',  -- candidate|llm_reviewed|human_approved|rejected
    llm_verdict          JSONB,                                -- {is_same_event, same_source, same_cutoff, mismatch_risk, blocking_reason, requires_human_review}
    mismatch_risk        FLOAT,                                -- 0..1
    approved_at          TIMESTAMPTZ,
    approved_by          TEXT,                                 -- 'llm_auto' | 'human:<name>'
    notes                TEXT,
    created_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE (polymarket_market_id, kalshi_market_id)
);
CREATE INDEX IF NOT EXISTS idx_matched_pairs_status ON matched_pairs(candidate_status);
CREATE INDEX IF NOT EXISTS idx_matched_pairs_mismatch ON matched_pairs(mismatch_risk);

-- ---------- Extended resolution criteria (additive columns on market_rules) ----------
-- Phase A had: rules_text, resolution_source, cutoff_time, timezone, dispute_window_sec, settlement_logic
-- Phase B adds: resolution_type, official_source_url, settlement_timing, fallback_source
ALTER TABLE market_rules
    ADD COLUMN IF NOT EXISTS resolution_type TEXT,           -- 'official_result','media_call','certification','oracle'
    ADD COLUMN IF NOT EXISTS official_source_url TEXT,
    ADD COLUMN IF NOT EXISTS settlement_timing TEXT,          -- 'realtime','end_of_event','T+N_days'
    ADD COLUMN IF NOT EXISTS fallback_source TEXT;

-- ---------- Fee schedule (per venue + category) ----------
CREATE TABLE IF NOT EXISTS fee_schedule (
    id              SERIAL PRIMARY KEY,
    venue_id        TEXT NOT NULL REFERENCES venues(id),
    category        TEXT,                                     -- 'sports','politics','crypto','economics', NULL=default
    taker_fee_rate  FLOAT NOT NULL,                           -- fraction, e.g. 0.0075 = 0.75%
    maker_fee_rate  FLOAT NOT NULL DEFAULT 0,
    fee_formula     TEXT,                                     -- 'percentage' | 'kalshi_pxp' | 'none'
    notes           TEXT,
    updated_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (venue_id, category)
);

-- Seed known fee schedules (verify against official docs at runtime)
INSERT INTO fee_schedule (venue_id, category, taker_fee_rate, maker_fee_rate, fee_formula, notes) VALUES
    ('polymarket', 'sports',     0.0075, 0.0000, 'percentage', 'Polymarket sports taker; verify per market'),
    ('polymarket', 'politics',   0.0075, 0.0000, 'percentage', 'Polymarket politics taker'),
    ('polymarket', 'crypto',     0.0180, 0.0000, 'percentage', 'Polymarket crypto taker (highest)'),
    ('polymarket', 'geopolitical', 0.0000, 0.0000, 'none',     'Polymarket geopolitical: fee-free'),
    ('polymarket', NULL,         0.0075, 0.0000, 'percentage', 'Polymarket default'),
    ('kalshi',     NULL,         0.0000, 0.0000, 'kalshi_pxp', 'Kalshi: 0.07*C*P*(1-P) taker; maker 0.25x')
ON CONFLICT (venue_id, category) DO UPDATE SET
    taker_fee_rate = EXCLUDED.taker_fee_rate,
    maker_fee_rate = EXCLUDED.maker_fee_rate,
    fee_formula = EXCLUDED.fee_formula,
    notes = EXCLUDED.notes,
    updated_at = now();

-- ---------- Arb groups (paired paper orders) ----------
CREATE TABLE IF NOT EXISTS paper_order_groups (
    id              BIGSERIAL PRIMARY KEY,
    group_type      TEXT NOT NULL,                            -- 'arb','single'
    matched_pair_id BIGINT REFERENCES matched_pairs(id),
    replay_run_id   BIGINT REFERENCES replay_runs(id),
    created_at      TIMESTAMPTZ DEFAULT now(),
    notes           TEXT
);

ALTER TABLE paper_orders
    ADD COLUMN IF NOT EXISTS group_id BIGINT REFERENCES paper_order_groups(id),
    ADD COLUMN IF NOT EXISTS leg_role TEXT;                    -- 'A'|'B' for arb pairs

-- ---------- Arb decision gate results ----------
CREATE TABLE IF NOT EXISTS arb_gate_results (
    id                      BIGSERIAL PRIMARY KEY,
    matched_pair_id         BIGINT REFERENCES matched_pairs(id),
    replay_run_id           BIGINT REFERENCES replay_runs(id),
    detected_opportunities  INT,
    paired_tape_confirmed    INT,
    paired_conservative      INT,
    both_legs_filled         INT,
    one_leg_filled           INT,
    resolution_mismatches    INT,
    net_ev_tape              FLOAT,
    net_ev_conservative      FLOAT,
    median_executable_spread FLOAT,
    fill_rate               FLOAT,
    decision                TEXT,                             -- 'GO_LIVE'|'STAY_PAPER'|'NEEDS_MORE_DATA'
    created_at              TIMESTAMPTZ DEFAULT now()
);
