"""Async DB access layer (psycopg pool). Append-only writes, point-in-time reads."""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from pm_agent.clients.schemas import NormalisedMarket, NormalisedOrderbook, NormalisedOutcome, NormalisedTrade
from pm_agent.config import settings


_pool: AsyncConnectionPool | None = None


async def get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=8, open=False)
        await _pool.open()
        await _pool.wait()
    return _pool


@asynccontextmanager
async def db_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    pool = await get_pool()
    async with pool.connection() as conn:
        yield conn


def _hash(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()


# ---------- Markets ----------

async def upsert_event(venue: str, venue_event_id: str, title: str | None, category: str | None) -> str:
    """Upsert an event with its category (Kalshi events carry category; markets don't)."""
    event_id = f"{venue}:{venue_event_id}"
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO events (id, venue_id, venue_event_id, title, category)
            VALUES (%(id)s, %(venue)s, %(veid)s, %(title)s, %(cat)s)
            ON CONFLICT (id) DO UPDATE SET
                title=COALESCE(EXCLUDED.title, events.title),
                category=COALESCE(EXCLUDED.category, events.category)
            """,
            dict(id=event_id, venue=venue, veid=venue_event_id, title=title, cat=category),
        )
    return event_id


async def upsert_market(m: NormalisedMarket) -> str:
    """Insert market if new, or update metadata. Returns internal market id."""
    market_id = f"{m.venue}:{m.venue_market_id}"
    event_id = f"{m.venue}:{m.venue_event_id}" if m.venue_event_id else None
    async with db_conn() as conn:
        # Ensure event exists first (FK constraint)
        if event_id:
            await conn.execute(
                """
                INSERT INTO events (id, venue_id, venue_event_id, title, category)
                VALUES (%(id)s, %(venue)s, %(veid)s, %(title)s, %(cat)s)
                ON CONFLICT (id) DO NOTHING
                """,
                dict(id=event_id, venue=m.venue, veid=m.venue_event_id,
                     title=m.title, cat=m.category),
            )
        await conn.execute(
            """
            INSERT INTO markets (id, venue_id, venue_market_id, event_id, title, slug, category, status,
                                 open_time, close_time, resolve_time, updated_at)
            VALUES (%(id)s, %(venue)s, %(vmid)s, %(eid)s, %(title)s, %(slug)s, %(cat)s, %(status)s,
                    %(open)s, %(close)s, %(resolve)s, now())
            ON CONFLICT (id) DO UPDATE SET
                title=EXCLUDED.title, status=EXCLUDED.status,
                category=COALESCE(EXCLUDED.category, markets.category),
                open_time=COALESCE(EXCLUDED.open_time, markets.open_time),
                close_time=COALESCE(EXCLUDED.close_time, markets.close_time),
                resolve_time=COALESCE(EXCLUDED.resolve_time, markets.resolve_time),
                updated_at=now()
            """,
            dict(id=market_id, venue=m.venue, vmid=m.venue_market_id,
                 eid=event_id,
                 title=m.title, slug=m.slug, cat=m.category, status=m.status,
                 open=m.open_time, close=m.close_time, resolve=m.resolve_time),
        )
        # Append version (dedup by hash)
        await conn.execute(
            """
            INSERT INTO market_versions (market_id, observed_at, payload_hash, raw_payload)
            VALUES (%(mid)s, now(), %(h)s, %(raw)s)
            ON CONFLICT (market_id, observed_at, payload_hash) DO NOTHING
            """,
            dict(mid=market_id, h=_hash(m.raw), raw=json.dumps(m.raw, default=str)),
        )
    return market_id


async def upsert_outcome(o: NormalisedOutcome) -> str:
    outcome_id = f"{o.venue}:{o.venue_market_id}:{o.outcome_name}"
    market_id = f"{o.venue}:{o.venue_market_id}"
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO outcomes (id, market_id, outcome_name, venue_token_id, side)
            VALUES (%(id)s, %(mid)s, %(name)s, %(tok)s, %(side)s)
            ON CONFLICT (id) DO UPDATE SET venue_token_id=EXCLUDED.venue_token_id
            """,
            dict(id=outcome_id, mid=market_id, name=o.outcome_name, tok=o.venue_token_id, side=o.side),
        )
    return outcome_id


async def save_rules(market_id: str, rules_text: str | None, resolution_source: str | None,
                     raw: dict | None = None) -> None:
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO market_rules (market_id, observed_at, rules_text, resolution_source, rules_hash, raw_payload)
            VALUES (%(mid)s, now(), %(r)s, %(rs)s, %(h)s, %(raw)s)
            ON CONFLICT (market_id, observed_at, rules_hash) DO NOTHING
            """,
            dict(mid=market_id, r=rules_text, rs=resolution_source,
                 h=_hash({"rules": rules_text, "src": resolution_source, "raw": raw}),
                 raw=json.dumps(raw, default=str) if raw else None),
        )


# ---------- Orderbook / Trades (append-only) ----------

async def save_orderbook(ob: NormalisedOrderbook) -> None:
    market_id = f"{ob.venue}:{ob.venue_market_id}"
    outcome_id = f"{ob.venue}:{ob.venue_market_id}:{ob.outcome_name}"
    spread = (ob.best_ask - ob.best_bid) if (ob.best_bid is not None and ob.best_ask is not None) else None
    depth = [{"side": l.side, "price": l.price, "size": l.size} for l in ob.levels[:20]]
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO orderbook_snapshots
                (venue_id, market_id, outcome_id, ts_exchange, ts_collected,
                 best_bid, best_ask, mid, spread, depth_top_json, payload_hash, raw_payload)
            VALUES (%(venue)s, %(mid)s, %(oid)s, %(tse)s, now(),
                    %(bb)s, %(ba)s, %(midp)s, %(sp)s, %(depth)s, %(h)s, %(raw)s)
            """,
            dict(venue=ob.venue, mid=market_id, oid=outcome_id, tse=ob.ts_exchange,
                 bb=ob.best_bid, ba=ob.best_ask, midp=ob.mid, sp=spread,
                 depth=json.dumps(depth), h=_hash(ob.raw), raw=json.dumps(ob.raw, default=str)),
        )


async def save_trade(t: NormalisedTrade) -> None:
    market_id = f"{t.venue}:{t.venue_market_id}"
    outcome_id = f"{t.venue}:{t.venue_market_id}:{t.outcome_name}"
    async with db_conn() as conn:
        await conn.execute(
            """
            INSERT INTO trade_prints
                (venue_id, trade_id, market_id, outcome_id, ts_exchange, ts_collected, price, size, side, raw_payload)
            VALUES (%(venue)s, %(tid)s, %(mid)s, %(oid)s, %(tse)s, now(), %(p)s, %(sz)s, %(side)s, %(raw)s)
            """,
            dict(venue=t.venue, tid=t.venue_trade_id, mid=market_id, oid=outcome_id,
                 tse=t.ts_exchange, p=t.price, sz=t.size, side=t.side,
                 raw=json.dumps(t.raw, default=str)),
        )


# ---------- Point-in-time reads (no leakage) ----------

async def get_orderbook_at(market_id: str, outcome_id: str, at: object) -> dict | None:
    """Latest snapshot with ts_collected <= `at`. Enforces no-leakage."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM orderbook_snapshots
                WHERE market_id=%s AND outcome_id=%s AND ts_collected <= %s
                ORDER BY ts_collected DESC LIMIT 1
                """,
                (market_id, outcome_id, at),
            )
            return await cur.fetchone()


async def get_trades_through(market_id: str, outcome_id: str, at: object, price: float) -> list[dict]:
    """Real trade prints at/through our limit price, with ts_exchange <= `at`.
    Used by tape-confirmed fill model."""
    async with db_conn() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM trade_prints
                WHERE market_id=%s AND outcome_id=%s AND ts_exchange <= %s
                  AND price <= %s
                ORDER BY ts_exchange ASC
                """,
                (market_id, outcome_id, at, price),
            )
            return await cur.fetchall()
