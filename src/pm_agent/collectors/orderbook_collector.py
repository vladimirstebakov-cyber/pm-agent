"""Orderbook collector — snapshots for watchlist markets at tiered frequency.

Phase A: stores snapshots only. Watchlist selection (arb/liquid/preres) is a
configurable filter; full scanner logic comes in Phase B.
"""
from __future__ import annotations

import asyncio
import logging

from pm_agent.clients.polymarket_clob import PolymarketClobClient
from pm_agent.clients.kalshi_rest import KalshiRestClient
from pm_agent.db import repo
from pm_agent.db.queries import active_watchlist

log = logging.getLogger(__name__)


async def snapshot_polymarket(market_id: str, token_id: str, outcome_name: str) -> None:
    if not token_id:
        return
    client = PolymarketClobClient()
    try:
        raw = await client.get_book(token_id)
        ob = PolymarketClobClient.normalise_book(raw, market_id, outcome_name, token_id)
        await repo.save_orderbook(ob)
    except Exception as e:
        log.warning("polymarket snapshot fail %s: %s", market_id, e)
    finally:
        await client.close()


async def snapshot_kalshi(ticker: str) -> None:
    client = KalshiRestClient()
    try:
        raw = await client.get_orderbook(ticker)
        for side in ("YES", "NO"):
            ob = KalshiRestClient.normalise_orderbook(raw, ticker, side)
            await repo.save_orderbook(ob)
    except Exception as e:
        log.warning("kalshi snapshot fail %s: %s", ticker, e)
    finally:
        await client.close()


async def run_loop(interval_sec: int, venue: str | None = None) -> None:
    """Continuously snapshot watchlist markets at `interval_sec`."""
    log.info("orderbook collector loop: interval=%ss venue=%s", interval_sec, venue or "all")
    while True:
        try:
            items = await active_watchlist(venue=venue, limit=50)
            tasks = []
            for it in items:
                if it["venue_id"] == "polymarket":
                    tasks.append(snapshot_polymarket(it["market_id"], it.get("venue_token_id") or "", it.get("outcome_name") or "YES"))
                elif it["venue_id"] == "kalshi":
                    tasks.append(snapshot_kalshi(it["venue_market_id"]))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            log.error("collector loop error: %s", e)
        await asyncio.sleep(interval_sec)
