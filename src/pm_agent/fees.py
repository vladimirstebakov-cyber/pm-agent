"""Fee schedule + executable spread calculation.

Sources (verify at runtime against official docs):
- Polymarket: category taker fee, sport 0.75%, crypto 1.80%, geopolitical 0%. Makers 0 + rebates.
- Kalshi: taker fee = 0.07 * C * P * (1-P), where C=contract count, P=price. Maker = 0.25x taker.
- Gas (Polygon): amortized funding/withdrawal cost, NOT per trade (relayer covers trade gas).
"""
from __future__ import annotations

from dataclasses import dataclass

from pm_agent.db.repo import db_conn


@dataclass
class FeeQuote:
    venue: str
    category: str | None
    fee_formula: str
    taker_rate: float
    maker_rate: float


async def get_fee_schedule(venue: str, category: str | None = None) -> FeeQuote | None:
    """Fetch fee schedule for venue + category (with default fallback)."""
    async with db_conn() as conn:
        async with conn.cursor() as cur:
            # Try exact category match first, then venue default (category IS NULL)
            await cur.execute(
                """
                SELECT venue_id, category, taker_fee_rate, maker_fee_rate, fee_formula
                FROM fee_schedule
                WHERE venue_id=%s AND (category=%s OR category IS NULL)
                ORDER BY (category IS NOT NULL) DESC  -- prefer exact category match
                LIMIT 1
                """,
                (venue, category),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return FeeQuote(
                venue=row[0], category=row[1],
                fee_formula=row[4] or "percentage",
                taker_rate=float(row[2]), maker_rate=float(row[3]),
            )


def kalshi_taker_fee(contracts: int, price: float) -> float:
    """Kalshi fee = 0.07 * C * P * (1-P), rounded up to nearest cent at trade time.
    Peak near P=0.50, near zero at P=0.01/0.99."""
    import math
    raw = 0.07 * contracts * price * (1 - price)
    return math.ceil(raw * 100) / 100  # round up to cent


def polymarket_taker_fee(notional: float, taker_rate: float) -> float:
    """Polymarket percentage fee on notional."""
    return notional * taker_rate


def slippage_cost(order_size_contracts: int, book_side: list[tuple[float, float]]) -> float | None:
    """Estimate slippage for walking the book.
    book_side: list of (price, size) levels on the side we're taking from.
    Returns average fill price, or None if insufficient depth."""
    remaining = order_size_contracts
    filled = 0.0
    cost = 0.0
    for price, size in book_side:
        take = min(remaining, int(size))
        if take <= 0:
            break
        cost += take * price
        filled += take
        remaining -= take
    if filled == 0:
        return None
    return cost / filled  # avg fill price


def executable_spread(
    poly_ask_yes: float,        # price to BUY YES on Polymarket
    kalshi_ask_no: float,       # price to BUY NO on Kalshi (equivalent to selling YES)
    contracts: int,
    poly_taker_rate: float,
    kalshi_price_for_fee: float,
    poly_book_yes: list[tuple[float, float]] | None = None,
    kalshi_book_no: list[tuple[float, float]] | None = None,
    gas_amortized: float = 0.0,
) -> dict:
    """Compute net executable spread for a YES-on-poly / NO-on-kalshi arb pair.

    Arb condition: cost_of_yes_poly + cost_of_no_kalshi + fees < 1.00 (payout)
    """
    # Walk the book for realistic fill prices
    poly_fill = slippage_cost(contracts, poly_book_yes) if poly_book_yes else poly_ask_yes
    kalshi_fill = slippage_cost(contracts, kalshi_book_no) if kalshi_book_no else kalshi_ask_no

    if poly_fill is None or kalshi_fill is None:
        return {"valid": False, "reason": "insufficient_depth"}

    poly_notional = poly_fill * contracts
    poly_fee = polymarket_taker_fee(poly_notional, poly_taker_rate)
    kalshi_fee = kalshi_taker_fee(contracts, kalshi_price_for_fee)

    total_cost = (poly_fill * contracts) + (kalshi_fill * contracts) + poly_fee + kalshi_fee + gas_amortized
    payout = 1.00 * contracts  # $1 per contract
    net_profit = payout - total_cost
    net_spread_pct = net_profit / (poly_fill * contracts) if poly_fill > 0 else 0

    return {
        "valid": net_profit > 0,
        "poly_fill_price": poly_fill,
        "kalshi_fill_price": kalshi_fill,
        "poly_fee": poly_fee,
        "kalshi_fee": kalshi_fee,
        "gas_amortized": gas_amortized,
        "total_cost": total_cost,
        "payout": payout,
        "net_profit": net_profit,
        "net_spread_pct": net_spread_pct,
    }
