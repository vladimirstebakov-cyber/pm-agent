"""Contract matching engine — LLM-assisted NLP for Polymarket <-> Kalshi pairs.

Flow:
  1. Candidate pairs: title similarity > threshold (pg_trgm)
  2. LLM verdict (GLM-5.2 routine, Opus for disputes): JSON with mismatch_risk, blocking_reason
  3. Auto-approve only if mismatch_risk < 0.1 AND hard fields match
  4. Otherwise: human-review queue

LLM is called via OpenRouter (GLM-5.2 / Claude Opus). Swap models in config.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import httpx
from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn

log = logging.getLogger(__name__)

# LLM endpoints (OpenRouter-compatible). Configure via env.
OPENROUTER_BASE = os.getenv("OPENROUTER_BASE", "https://openrouter.ai/api/v1")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
GLM_MODEL = os.getenv("GLM_MODEL", "z-ai/glm-5.2")
OPUS_MODEL = os.getenv("OPUS_MODEL", "anthropic/claude-opus-4.8")


MATCH_PROMPT = """You are a prediction-market contract matching analyst.
Compare two markets from different platforms and determine if they are the SAME event
with the SAME resolution criteria. A mismatch turns arbitrage into speculation.

Market A (Polymarket):
  title: {title_a}
  rules: {rules_a}
  resolution_source: {source_a}
  cutoff: {cutoff_a}

Market B (Kalshi):
  title: {title_b}
  rules: {rules_b}
  resolution_source: {source_b}
  cutoff: {cutoff_b}

Known mismatch patterns (CRITICAL):
- "popular vote" vs "electoral college" => NOT same event
- "media call" vs "official certification" => different settlement timing
- Different cutoff dates/timezones => settlement gap risk
- "France reaches final" vs "France wins" => different outcomes

Respond as STRICT JSON only:
{{
  "is_same_event": true/false,
  "same_resolution_source": true/false,
  "same_cutoff": true/false,
  "mismatch_risk": 0.0-1.0,
  "blocking_reason": "string or null",
  "requires_human_review": true/false
}}"""


@dataclass
class LLMVerdict:
    is_same_event: bool
    same_resolution_source: bool
    same_cutoff: bool
    mismatch_risk: float
    blocking_reason: str | None
    requires_human_review: bool


async def call_llm(model: str, prompt: str) -> dict:
    """Call OpenRouter chat completions. Returns parsed JSON.
    Robust to models that don't support response_format or wrap JSON in markdown."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set — cannot call LLM for matching")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
            },
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"OpenRouter error: {str(data['error'])[:300]}")
        choices = data.get("choices")
        if not choices:
            raise RuntimeError(f"OpenRouter: no choices in response: {str(data)[:300]}")
        content = choices[0].get("message", {}).get("content", "")
        return _extract_json(content)


def _extract_json(content: str) -> dict:
    """Parse JSON from LLM content, handling markdown code fences."""
    import re
    if not content:
        raise RuntimeError("OpenRouter: empty content")
    # Try direct parse first
    try:
        return json.loads(content)
    except Exception:
        pass
    # Try extracting from ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try finding first { ... last }
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(content[start:end+1])
        except Exception:
            pass
    raise RuntimeError(f"OpenRouter: could not parse JSON from content: {content[:200]}")


def parse_verdict(raw: dict) -> LLMVerdict:
    return LLMVerdict(
        is_same_event=bool(raw.get("is_same_event", False)),
        same_resolution_source=bool(raw.get("same_resolution_source", False)),
        same_cutoff=bool(raw.get("same_cutoff", False)),
        mismatch_risk=float(raw.get("mismatch_risk", 1.0)),
        blocking_reason=raw.get("blocking_reason"),
        requires_human_review=bool(raw.get("requires_human_review", True)),
    )


async def fetch_candidate_pairs(threshold: float = 0.5, limit: int = 100) -> list[dict]:
    """Candidate pairs by title similarity (requires pg_trgm + GIN index for speed)."""
    async with db_conn() as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_markets_title_trgm "
            "ON markets USING GIN (lower(title) gin_trgm_ops)"
        )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT set_config('pg_trgm.similarity_threshold', %s, false)", (str(threshold),))
            await cur.execute(
                """
                SELECT a.id AS poly_id, a.title AS poly_title,
                       b.id AS kalshi_id, b.title AS kalshi_title,
                       similarity(lower(a.title), lower(b.title)) AS sim
                FROM markets a
                JOIN markets b ON b.venue_id='kalshi' AND a.venue_id='polymarket'
                  AND a.status='active' AND b.status='active'
                  AND lower(a.title) % lower(b.title)
                WHERE similarity(lower(a.title), lower(b.title)) > %s
                LIMIT %s
                """,
                (threshold, limit),
            )
            return await cur.fetchall()


async def fetch_rules(market_id: str) -> dict | None:
    """Latest rules version for a market."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM market_rules
                WHERE market_id=%s ORDER BY observed_at DESC LIMIT 1
                """,
                (market_id,),
            )
            return await cur.fetchone()


async def evaluate_pair(poly: dict, kalshi: dict, model: str = GLM_MODEL) -> LLMVerdict:
    """Run LLM matching verdict on one candidate pair."""
    rules_a = await fetch_rules(poly["poly_id"]) or {}
    rules_b = await fetch_rules(kalshi["kalshi_id"]) or {}
    prompt = MATCH_PROMPT.format(
        title_a=poly["poly_title"], rules_a=rules_a.get("rules_text", "")[:2000],
        source_a=rules_a.get("resolution_source", ""), cutoff_a=str(rules_a.get("cutoff_time", "")),
        title_b=kalshi["kalshi_title"], rules_b=rules_b.get("rules_text", "")[:2000],
        source_b=rules_b.get("resolution_source", ""), cutoff_b=str(rules_b.get("cutoff_time", "")),
    )
    raw = await call_llm(model, prompt)
    return parse_verdict(raw)


async def store_pair(poly: dict, kalshi: dict, verdict: LLMVerdict, model: str) -> int:
    """Insert/update matched_pairs with verdict."""
    auto_approve = (
        verdict.is_same_event
        and verdict.same_resolution_source
        and verdict.same_cutoff
        and verdict.mismatch_risk < 0.1
    )
    status = "human_approved" if auto_approve else "llm_reviewed"
    approved_by = "llm_auto" if auto_approve else None
    verdict_json = {
        "is_same_event": verdict.is_same_event,
        "same_resolution_source": verdict.same_resolution_source,
        "same_cutoff": verdict.same_cutoff,
        "mismatch_risk": verdict.mismatch_risk,
        "blocking_reason": verdict.blocking_reason,
        "requires_human_review": verdict.requires_human_review,
        "model": model,
    }
    from datetime import datetime, timezone
    approved_at_val = datetime.now(timezone.utc) if auto_approve else None
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO matched_pairs
                    (polymarket_market_id, kalshi_market_id, title_similarity,
                     candidate_status, llm_verdict, mismatch_risk, approved_at, approved_by)
                VALUES (%(pid)s, %(kid)s, %(sim)s, %(status)s, %(vj)s, %(mr)s, %(at)s, %(ab)s)
                ON CONFLICT (polymarket_market_id, kalshi_market_id) DO UPDATE SET
                    candidate_status=EXCLUDED.candidate_status,
                    llm_verdict=EXCLUDED.llm_verdict,
                    mismatch_risk=EXCLUDED.mismatch_risk,
                    approved_at=COALESCE(matched_pairs.approved_at, EXCLUDED.approved_at),
                    approved_by=COALESCE(matched_pairs.approved_by, EXCLUDED.approved_by)
                RETURNING id
                """,
                dict(pid=poly["poly_id"], kid=kalshi["kalshi_id"], sim=poly.get("sim", 0),
                     status=status, vj=json.dumps(verdict_json), mr=verdict.mismatch_risk,
                     at=approved_at_val, ab=approved_by),
            )
            row = await cur.fetchone()
            return row["id"] if row else 0


async def run_matching(threshold: float = 0.5, limit: int = 50, model: str = GLM_MODEL) -> dict:
    """Full matching pipeline: candidates -> LLM verdict -> store.
    Use Opus for disputes by re-running with model=OPUS_MODEL on requires_human_review pairs."""
    candidates = await fetch_candidate_pairs(threshold=threshold, limit=limit)
    log.info("matching: %d candidate pairs", len(candidates))
    auto_approved = 0
    human_queue = 0
    for c in candidates:
        poly = {"poly_id": c["poly_id"], "poly_title": c["poly_title"], "sim": c["sim"]}
        kalshi = {"kalshi_id": c["kalshi_id"], "kalshi_title": c["kalshi_title"]}
        try:
            verdict = await evaluate_pair(poly, kalshi, model=model)
            await store_pair(poly, kalshi, verdict, model)
            if verdict.mismatch_risk < 0.1 and verdict.is_same_event:
                auto_approved += 1
            else:
                human_queue += 1
        except Exception as e:
            log.warning("matching failed for %s / %s: %s", c["poly_id"], c["kalshi_id"], e)
            human_queue += 1
        await asyncio.sleep(0.5)  # be gentle on LLM API
    return {"candidates": len(candidates), "auto_approved": auto_approved, "human_queue": human_queue}


async def human_review_queue(limit: int = 50) -> list[dict]:
    """Pairs awaiting human review (LLM verdict but not auto-approved)."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT mp.id, mp.polymarket_market_id, mp.kalshi_market_id,
                       mp.mismatch_risk, mp.llm_verdict, mp.notes,
                       a.title AS poly_title, b.title AS kalshi_title
                FROM matched_pairs mp
                JOIN markets a ON a.id=mp.polymarket_market_id
                JOIN markets b ON b.id=mp.kalshi_market_id
                WHERE mp.candidate_status='llm_reviewed'
                ORDER BY mp.mismatch_risk ASC, mp.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return await cur.fetchall()


async def human_approve(pair_id: int, reviewer: str, notes: str | None = None) -> None:
    """Manually approve a pair after human review."""
    async with db_conn() as conn:
        await conn.execute(
            """
            UPDATE matched_pairs
            SET candidate_status='human_approved', approved_at=now(),
                approved_by=%s, notes=COALESCE(%s, notes)
            WHERE id=%s
            """,
            (f"human:{reviewer}", notes, pair_id),
        )
