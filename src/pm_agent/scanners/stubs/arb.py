"""Pattern #1: Cross-platform arbitrage (Polymarket <-> Kalshi).

Phase B implementation. Scans APPROVED matched pairs only, computes executable
spread after fees + slippage, emits ArbOpportunity (paired signals) when
net_spread_pct > threshold.

NEVER scan un-approved pairs — resolution mismatch turns arb into speculation.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn
from pm_agent.fees import executable_spread, get_fee_schedule
from pm_agent.scanners.interfaces import Signal, Scanner

log = logging.getLogger(__name__)

ARB_THRESHOLD_PCT = 0.03  # 3% net after fees + slippage + gas
DEFAULT_CONTRACTS = 100   # contract count for spread calc


@dataclass
class ArbOpportunity:
    """Paired arb signal: BUY YES on cheaper venue + BUY NO on expensive venue.
    Both legs must fill for valid arb; one-leg fill = leg risk."""
    arb_id: str
    matched_pair_id: int
    signal_time: datetime
    leg_a: Signal  # BUY YES on cheaper venue
    leg_b: Signal  # BUY NO on expensive venue (equivalent to selling YES)
    expected_net_spread: float
    resolution_mismatch_risk: float
    fee_breakdown: dict


class ArbScanner(Scanner):
    """Scans approved matched pairs for executable arb spreads."""

    name = "arb"

    def __init__(self, threshold_pct: float = ARB_THRESHOLD_PCT, contracts: int = DEFAULT_CONTRACTS) -> None:
        self.threshold_pct = threshold_pct
        self.contracts = contracts

    async def _approved_pairs(self, limit: int = 50) -> list[dict]:
        async with db_conn() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT mp.id, mp.polymarket_market_id, mp.kalshi_market_id,
                           mp.mismatch_risk,
                           a.title AS poly_title, a.category AS poly_category,
                           b.title AS kalshi_title, b.category AS kalshi_category
                    FROM matched_pairs mp
                    JOIN markets a ON a.id=mp.polymarket_market_id
                    JOIN markets b ON b.id=mp.kalshi_market_id
                    WHERE mp.candidate_status='human_approved'
                      AND a.status='active' AND b.status='active'
                    LIMIT %s
                    """,
                    (limit,),
                )
                return await cur.fetchall()

    async def _latest_orderbooks(self, poly_market_id: str, kalshi_market_id: str) -> dict:
        """Fetch latest point-in-time orderbook snapshots for both venues."""
        async with db_conn() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                # Polymarket YES outcome
                await cur.execute(
                    """
                    SELECT * FROM orderbook_snapshots
                    WHERE market_id=%s AND outcome_id LIKE '%%:YES'
                    ORDER BY ts_collected DESC LIMIT 1
                    """,
                    (poly_market_id,),
                )
                poly_yes = await cur.fetchone()
                # Kalshi YES and NO
                await cur.execute(
                    """
                    SELECT * FROM orderbook_snapshots
                    WHERE market_id=%s
                    ORDER BY ts_collected DESC LIMIT 1
                    """,
                    (kalshi_market_id,),
                )
                kalshi_rows = await cur.fetchall()
                kalshi_no = next((r for r in kalshi_rows if "NO" in (r.get("outcome_id") or "")), None)
                return {"poly_yes": poly_yes, "kalshi_no": kalshi_no}

    async def scan(self) -> AsyncIterator[ArbOpportunity]:
        """Yield arb opportunities on approved pairs. Uses point-in-time snapshots only."""
        pairs = await self._approved_pairs(limit=50)
        log.info("arb scanner: %d approved pairs", len(pairs))

        for pair in pairs:
            try:
                books = await self._latest_orderbooks(pair["polymarket_market_id"], pair["kalshi_market_id"])
                poly_yes = books["poly_yes"]
                kalshi_no = books["kalshi_no"]
                if not poly_yes or not kalshi_no:
                    continue

                # Best ask on Polymarket YES (cost to buy YES)
                poly_ask_yes = float(poly_yes.get("best_ask") or 0)
                # Best ask on Kalshi NO (cost to buy NO = equivalent to selling YES)
                kalshi_ask_no = float(kalshi_no.get("best_ask") or 0)

                if poly_ask_yes <= 0 or kalshi_ask_no <= 0:
                    continue

                # Fee schedules
                poly_fee = await get_fee_schedule("polymarket", pair.get("poly_category"))
                poly_taker = poly_fee.taker_rate if poly_fee else 0.0075

                # Compute executable spread
                result = executable_spread(
                    poly_ask_yes=poly_ask_yes,
                    kalshi_ask_no=kalshi_ask_no,
                    contracts=self.contracts,
                    poly_taker_rate=poly_taker,
                    kalshi_price_for_fee=poly_ask_yes,  # Kalshi fee uses price ~ P
                    poly_book_yes=None,  # TODO: parse depth_top_json for real slippage
                    kalshi_book_no=None,
                    gas_amortized=0.001 * self.contracts,  # amortized, tiny per contract
                )

                if not result.get("valid"):
                    continue
                if result["net_spread_pct"] < self.threshold_pct:
                    continue

                # Emit paired signals: buy YES cheap on Polymarket, buy NO cheap on Kalshi
                signal_time = datetime.now(timezone.utc)
                arb_id = f"arb:{pair['id']}:{uuid.uuid4().hex[:8]}"
                leg_a = Signal(
                    market_id=pair["polymarket_market_id"],
                    outcome_id=f"{pair['polymarket_market_id']}:YES",
                    side="BUY",
                    limit_price=result["poly_fill_price"],
                    size=float(self.contracts),
                    signal_time=signal_time,
                    pattern="arb",
                    rationale=f"BUY YES cheap on Polymarket (arb {arb_id})",
                    confidence=1.0 - pair.get("mismatch_risk", 1.0),
                )
                leg_b = Signal(
                    market_id=pair["kalshi_market_id"],
                    outcome_id=f"{pair['kalshi_market_id']}:NO",
                    side="BUY",
                    limit_price=result["kalshi_fill_price"],
                    size=float(self.contracts),
                    signal_time=signal_time,
                    pattern="arb",
                    rationale=f"BUY NO cheap on Kalshi (arb {arb_id})",
                    confidence=1.0 - pair.get("mismatch_risk", 1.0),
                )
                yield ArbOpportunity(
                    arb_id=arb_id,
                    matched_pair_id=pair["id"],
                    signal_time=signal_time,
                    leg_a=leg_a,
                    leg_b=leg_b,
                    expected_net_spread=result["net_spread_pct"],
                    resolution_mismatch_risk=pair.get("mismatch_risk", 0),
                    fee_breakdown=result,
                )
            except Exception as e:
                log.warning("arb scan failed for pair %s: %s", pair.get("id"), e)
