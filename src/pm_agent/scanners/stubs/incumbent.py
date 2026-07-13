"""Pattern #3: Incumbent re-election (BUY YES < 65¢).

Edge: calibration gap — markets price incumbent re-election ~61%, actual ~68%.
Strategy: filter electoral markets in OECD, incumbent < 65¢, context validation,
Modified Kelly 0.15 cap, hard cap 5% bankroll, correlated cap 10% per country.

Slow resolution (weeks-months) — won't give 30-day validation, but systematic edge.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn
from pm_agent.scanners.interfaces import Signal, Scanner
from pm_agent.sizing.kelly import size_incumbent

log = logging.getLogger(__name__)

# Base rate from Metaculus backtest (347 contracts 2018-2023)
DEFAULT_INCUMBENT_BASE_RATE = 0.68
INCUMBENT_PRICE_THRESHOLD = 0.65  # only buy YES below 65¢
OECD_COUNTRIES = {
    "usa", "uk", "germany", "france", "italy", "spain", "canada", "australia",
    "japan", "south korea", "netherlands", "belgium", "sweden", "norway",
    "finland", "denmark", "switzerland", "austria", "ireland", "portugal",
    "new zealand", "greece", "poland", "czech", "hungary", "slovakia",
    "mexico", "chile", "turkey", "estonia", "latvia", "lithuania", "slovenia",
    "luxembourg", "iceland",
}


class IncumbentScanner(Scanner):
    """Buy YES on incumbents below 65¢ in OECD elections, with context validation."""

    name = "incumbent"

    def __init__(self, bankroll: float = 5000.0) -> None:
        self.bankroll = bankroll

    async def _incumbent_markets(self, limit: int = 50) -> list[dict]:
        """Find electoral markets with incumbent context validation."""
        async with db_conn() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT m.id AS market_id, m.title, m.category, m.close_time,
                           cv.incumbent, cv.country, cv.is_oecd,
                           cv.approval_rating, cv.economy_context, cv.scandal_severity,
                           cv.base_rate_override, cv.validated_by
                    FROM markets m
                    LEFT JOIN context_validations cv ON cv.market_id = m.id
                    WHERE m.status='active'
                      AND (m.category ILIKE '%election%' OR m.title ILIKE '%re-elect%'
                           OR m.title ILIKE '%incumbent%' OR m.title ILIKE '%re-elect%')
                    LIMIT %s
                    """,
                    (limit,),
                )
                return await cur.fetchall()

    async def _latest_yes_price(self, market_id: str) -> float | None:
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

    async def scan(self) -> AsyncIterator[Signal]:
        """Yield BUY YES signals on incumbents below 65¢ in OECD, context-validated."""
        candidates = await self._incumbent_markets(limit=50)
        log.info("incumbent scanner: %d electoral markets", len(candidates))

        for c in candidates:
            # OECD filter
            country = (c.get("country") or "").lower()
            is_oecd = c.get("is_oecd")
            if is_oecd is None:
                is_oecd = any(oecd in country for oecd in OECD_COUNTRIES) if country else False
            if not is_oecd:
                continue

            yes_price = await self._latest_yes_price(c["market_id"])
            if yes_price is None or yes_price >= INCUMBENT_PRICE_THRESHOLD:
                continue

            # Context validation: if scandal severe or recession, override base rate down
            base_rate = c.get("base_rate_override") or DEFAULT_INCUMBENT_BASE_RATE
            scandal = (c.get("scandal_severity") or "none").lower()
            economy = (c.get("economy_context") or "").lower()
            if scandal == "severe" or economy == "recession":
                base_rate = min(base_rate, 0.50)  # context breaks base rate
                log.debug("incumbent %s: context override base_rate=%.2f (scandal/economy)",
                          c["market_id"], base_rate)

            edge = base_rate - yes_price
            if edge < 0.03:  # need at least 3pp edge
                continue

            # Sizing: Modified Kelly 0.15, cap 5%, correlated cap 10% per country
            sizing = size_incumbent(
                p_model=base_rate, price=yes_price, bankroll=self.bankroll,
                current_country_exposure=0.0,  # TODO: track per-country exposure
            )
            if sizing.contracts == 0:
                continue

            signal_time = datetime.now(timezone.utc)
            yield Signal(
                market_id=c["market_id"],
                outcome_id=f"{c['market_id']}:YES",
                side="BUY",
                limit_price=yes_price,
                size=float(sizing.contracts),
                signal_time=signal_time,
                pattern="incumbent",
                rationale=f"Incumbent {c.get('incumbent','?')} ({country}): YES {yes_price:.2f} vs base {base_rate:.2f}",
                confidence=min(1.0, edge * 3),
                model_probability=base_rate,
                market_probability=yes_price,
                edge=edge,
                sizing_method="fractional_kelly_0.15",
            )
