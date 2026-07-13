"""Market discovery collector — polls Polymarket Gamma + Kalshi for active markets.

Phase A: stores markets, outcomes, rules. No scanner logic.
"""
from __future__ import annotations

import asyncio
import logging

from pm_agent.clients.polymarket_gamma import PolymarketGammaClient
from pm_agent.clients.kalshi_rest import KalshiRestClient
from pm_agent.db import repo

log = logging.getLogger(__name__)


async def discover_polymarket(limit: int = 100, pages: int = 5) -> int:
    """Poll Gamma /markets, upsert markets + outcomes + rules."""
    client = PolymarketGammaClient()
    count = 0
    try:
        for page in range(pages):
            raws = await client.list_markets(limit=limit, offset=page * limit, active_only=True)
            if not raws:
                break
            for raw in raws:
                m = PolymarketGammaClient.normalise(raw)
                market_id = await repo.upsert_market(m)
                for o in PolymarketGammaClient.outcomes(raw):
                    await repo.upsert_outcome(o)
                if m.rules_text or m.resolution_source:
                    await repo.save_rules(market_id, m.rules_text, m.resolution_source, raw)
                count += 1
            await asyncio.sleep(0.1)
    finally:
        await client.close()
    log.info("polymarket discovery: %d markets", count)
    return count


async def discover_kalshi(limit: int = 100, pages: int = 5) -> int:
    """Poll Kalshi /markets, upsert markets + outcomes."""
    client = KalshiRestClient()
    count = 0
    try:
        cursor = None
        for _ in range(pages):
            data = await client.get_markets(status="open", limit=limit, cursor=cursor)
            raws = data.get("markets") or []
            cursor = data.get("cursor")
            if not raws:
                break
            for raw in raws:
                m = KalshiRestClient.normalise_market(raw)
                market_id = await repo.upsert_market(m)
                for o in KalshiRestClient.outcomes(raw):
                    await repo.upsert_outcome(o)
                # Fetch full rules for first sighting
                try:
                    detail = await client.get_market(raw.get("ticker"))
                    rules_text = detail.get("rules_primary") or detail.get("description")
                    await repo.save_rules(market_id, rules_text, "kalshi_official", detail)
                except Exception:
                    pass
                count += 1
            if not cursor:
                break
    finally:
        await client.close()
    log.info("kalshi discovery: %d markets", count)
    return count


async def discover_all() -> int:
    """Run both venues concurrently."""
    poly_count, kalshi_count = await asyncio.gather(
        discover_polymarket(), discover_kalshi(), return_exceptions=False
    )
    return poly_count + kalshi_count


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(discover_all())
