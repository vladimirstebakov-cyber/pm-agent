"""Pattern #2: Fade YES on narrative/mention markets.

Edge: "Yes Bias" — traders overpay for YES on narrative-driven contracts.
Strategy: compute base rate from transcripts, compare to market price, fade YES
when market >> base rate. News fade: wait 90min after spike >10%, then fade.

Semi-manual for first 20 markets: LLM classifies mention markets, agent/owner
adds transcript URLs. Base rate = mention_count / transcript_count.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from psycopg.rows import dict_row

from pm_agent.db.repo import db_conn
from pm_agent.scanners.interfaces import Signal, Scanner
from pm_agent.sizing.kelly import size_narrative_fade

log = logging.getLogger(__name__)

FADE_THRESHOLD = 0.15      # market_price - base_rate > 15pp => fade YES
NEWS_FADE_WAIT_SEC = 5400  # 90 minutes
NEWS_SPIKE_PCT = 0.10      # 10% price move = spike


class NarrativeYesFadeScanner(Scanner):
    """Fade YES on mention/narrative markets where base_rate << market_price."""

    name = "narrative_yes_fade"

    def __init__(self, bankroll: float = 1000.0) -> None:
        self.bankroll = bankroll

    async def _mention_markets_with_base_rates(self, limit: int = 50) -> list[dict]:
        """Markets that have computed mention base rates."""
        async with db_conn() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT m.id AS market_id, m.title, m.close_time,
                           mbr.base_rate, mbr.market_price, mbr.edge, mbr.phrase
                    FROM mention_base_rates mbr
                    JOIN markets m ON m.id = mbr.market_id
                    WHERE m.status='active' AND mbr.base_rate IS NOT NULL
                    LIMIT %s
                    """,
                    (limit,),
                )
                return await cur.fetchall()

    async def _recent_price_spike(self, market_id: str, outcome_id: str, lookback_sec: int = 7200) -> bool:
        """Check if price moved >10% in last 2h (news spike). If so, require 90min wait."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=lookback_sec)
        async with db_conn() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT best_ask FROM orderbook_snapshots
                    WHERE market_id=%s AND outcome_id=%s AND ts_collected >= %s
                    ORDER BY ts_collected ASC
                    """,
                    (market_id, outcome_id, cutoff),
                )
                rows = await cur.fetchall()
                if len(rows) < 2:
                    return False
                first = float(rows[0]["best_ask"] or 0)
                last = float(rows[-1]["best_ask"] or 0)
                if first <= 0:
                    return False
                return abs(last - first) / first > NEWS_SPIKE_PCT

    async def scan(self) -> AsyncIterator[Signal]:
        """Yield SELL YES (buy NO) signals where market overprices YES vs base rate."""
        candidates = await self._mention_markets_with_base_rates(limit=50)
        log.info("narrative scanner: %d mention markets with base rates", len(candidates))

        for c in candidates:
            market_price = float(c.get("market_price") or 0)
            base_rate = float(c.get("base_rate") or 0)
            edge = base_rate - market_price  # negative = market overprices YES => fade

            # Fade YES when market_price > base_rate + threshold
            if market_price - base_rate < FADE_THRESHOLD:
                continue

            outcome_id = f"{c['market_id']}:YES"
            # News spike check: if recent spike, require 90min wait (skip for now in paper)
            spike = await self._recent_price_spike(c["market_id"], outcome_id)
            if spike:
                # In live: would wait 90min. In paper/diagnostic: flag but proceed.
                log.debug("news spike detected on %s — would wait 90min in live", c["market_id"])

            # Sizing: p_model = base_rate, price = market_price
            # We're SELLING YES (buying NO), so invert: p_model_no = 1 - base_rate, price_no = 1 - market_price
            p_model_no = 1.0 - base_rate
            price_no = 1.0 - market_price
            sizing = size_narrative_fade(p_model=p_model_no, price=price_no, bankroll=self.bankroll)
            if sizing.contracts == 0:
                continue

            signal_time = datetime.now(timezone.utc)
            yield Signal(
                market_id=c["market_id"],
                outcome_id=f"{c['market_id']}:NO",  # buy NO = fade YES
                side="BUY",
                limit_price=price_no,
                size=float(sizing.contracts),
                signal_time=signal_time,
                pattern="narrative_yes_fade",
                rationale=f"Fade YES: market {market_price:.2f} vs base_rate {base_rate:.2f} (phrase: {c.get('phrase', '')})",
                confidence=min(1.0, abs(edge) * 2),
                model_probability=p_model_no,
                market_probability=price_no,
                edge=p_model_no - price_no,
                sizing_method="fractional_kelly_0.25",
            )
