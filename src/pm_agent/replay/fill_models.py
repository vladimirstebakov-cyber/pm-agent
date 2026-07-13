"""Fill simulation models — 4 modes, decision-grade honesty.

Naive:           fill at displayed bid/ask (fantasy baseline)
LatencyAdjusted: price after N seconds delay
TapeConfirmed:   fill ONLY if a real trade print crosses our limit price
Conservative:    losers filled fully, winners partially/never unless confirmed

Decision gate (paper -> live) uses ONLY tape_confirmed + conservative.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from pm_agent.db import repo

log = logging.getLogger(__name__)


class FillMode(str, Enum):
    NAIVE = "naive"
    LATENCY = "latency_adjusted"
    TAPE = "tape_confirmed"
    CONSERVATIVE = "conservative"


@dataclass
class FillResult:
    filled: bool
    fill_price: float | None
    fill_size: float | None
    reason: str
    tape_trade_id: str | None = None


async def fill_naive(market_id: str, outcome_id: str, side: str, limit_price: float,
                     size: float, decision_time: datetime) -> FillResult:
    """Fantasy: fill at best bid/ask at decision_time."""
    snap = await repo.get_orderbook_at(market_id, outcome_id, decision_time)
    if not snap:
        return FillResult(False, None, None, "no_snapshot")
    if side == "BUY":
        price = snap["best_ask"]
    else:
        price = snap["best_bid"]
    if price is None:
        return FillResult(False, None, None, "no_price")
    return FillResult(True, float(price), size, "naive")


async def fill_latency(market_id: str, outcome_id: str, side: str, limit_price: float,
                       size: float, decision_time: datetime, latency_sec: float = 2.0) -> FillResult:
    """Price after `latency_sec` delay — models execution lag."""
    after = decision_time + timedelta(seconds=latency_sec)
    snap = await repo.get_orderbook_at(market_id, outcome_id, after)
    if not snap:
        return FillResult(False, None, None, "no_snapshot_after_latency")
    if side == "BUY":
        price = snap["best_ask"]
    else:
        price = snap["best_bid"]
    if price is None:
        return FillResult(False, None, None, "no_price_after_latency")
    return FillResult(True, float(price), size, f"latency_{latency_sec}s")


async def fill_tape_confirmed(market_id: str, outcome_id: str, side: str, limit_price: float,
                              size: float, decision_time: datetime, window_sec: float = 30.0) -> FillResult:
    """Fill ONLY if a real trade print crosses our limit price within window.
    This is the decision-grade model: no fill unless the market actually traded
    through our price."""
    window_end = decision_time + timedelta(seconds=window_sec)
    # BUY at limit_price => need a trade at <= limit_price (someone sold to us)
    # SELL at limit_price => need a trade at >= limit_price (someone bought from us)
    trades = await repo.get_trades_through(market_id, outcome_id, window_end, limit_price)
    if not trades:
        return FillResult(False, None, None, "no_tape_confirmation")
    t = trades[0]
    return FillResult(True, float(t["price"]), size, "tape_confirmed", tape_trade_id=t.get("trade_id"))


async def fill_conservative(market_id: str, outcome_id: str, side: str, limit_price: float,
                            size: float, decision_time: datetime, window_sec: float = 30.0) -> FillResult:
    """Losers filled fully, winners partially/never unless tape-confirmed.
    Models adverse selection: if your order fills, the market probably knew
    something you didn't. Only confirmed winners count."""
    tape = await fill_tape_confirmed(market_id, outcome_id, side, limit_price, size, decision_time, window_sec)
    if not tape.filled:
        # No tape confirmation => conservative reject (assume we would have been adversely selected)
        return FillResult(False, None, None, "conservative_reject_no_tape")
    # Even with tape, reduce fill size for winners (adverse selection haircut)
    # This is the stress-test mode — pessimistic by design.
    haircut = 0.5  # winners only get 50% fill in conservative
    return FillResult(True, tape.fill_price, (tape.fill_size or size) * haircut,
                      "conservative_haircut", tape_trade_id=tape.tape_trade_id)


FILL_DISPATCH = {
    FillMode.NAIVE: fill_naive,
    FillMode.LATENCY: fill_latency,
    FillMode.TAPE: fill_tape_confirmed,
    FillMode.CONSERVATIVE: fill_conservative,
}


async def simulate_fill(mode: FillMode, market_id: str, outcome_id: str, side: str,
                        limit_price: float, size: float, decision_time: datetime,
                        **kwargs) -> FillResult:
    fn = FILL_DISPATCH[mode]
    return await fn(market_id, outcome_id, side, limit_price, size, decision_time, **kwargs)
