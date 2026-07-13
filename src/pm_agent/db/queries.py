"""Read-side queries for collectors and replay (point-in-time, no leakage)."""
from __future__ import annotations

from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn


async def active_watchlist(venue: str | None = None, limit: int = 100) -> list[dict]:
    """Markets with outcomes that have token ids, status active, not yet resolved.
    Used by orderbook collector to pick snapshot targets."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if venue:
                await cur.execute(
                    """
                    SELECT m.id AS market_id, m.venue_id, m.venue_market_id,
                           o.outcome_name, o.venue_token_id
                    FROM markets m JOIN outcomes o ON o.market_id = m.id
                    WHERE m.venue_id=%s AND m.status='active'
                      AND o.venue_token_id IS NOT NULL
                    LIMIT %s
                    """,
                    (venue, limit),
                )
            else:
                await cur.execute(
                    """
                    SELECT m.id AS market_id, m.venue_id, m.venue_market_id,
                           o.outcome_name, o.venue_token_id
                    FROM markets m JOIN outcomes o ON o.market_id = m.id
                    WHERE m.status='active' AND o.venue_token_id IS NOT NULL
                    LIMIT %s
                    """,
                    (limit,),
                )
            return await cur.fetchall()


async def matched_pairs() -> list[dict]:
    """Candidate cross-platform matched markets (stub — real NLP matcher in Phase B).
    Returns markets with similar titles across venues for human review."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT a.id AS poly_id, a.title AS poly_title,
                       b.id AS kalshi_id, b.title AS kalshi_title
                FROM markets a
                JOIN markets b ON b.venue_id='kalshi'
                  AND a.venue_id='polymarket'
                  AND a.status='active' AND b.status='active'
                  AND similarity(lower(a.title), lower(b.title)) > 0.5
                LIMIT 100
                """,
            )
            return await cur.fetchall()


async def get_latest_yes_price(market_id: str) -> float | None:
    """Latest YES best_ask for a market (used by base rate computation)."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT best_ask FROM orderbook_snapshots
                WHERE market_id=%s AND outcome_id LIKE '%%:YES'
                ORDER BY ts_collected DESC LIMIT 1
                """,
                (market_id,),
            )
            row = await cur.fetchone()
            return float(row["best_ask"]) if row and row["best_ask"] else None
