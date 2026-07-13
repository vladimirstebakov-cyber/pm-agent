"""Kelly criterion + position caps.

Pattern #3 (incumbent): Modified Kelly = 0.15 * full Kelly, hard cap 5% bankroll,
correlated exposure cap 10% per country/party family.

Pattern #2/#4: fractional Kelly 0.25-0.50 (configurable), hard caps apply.

Arb (#1): does not use Kelly — paired payout sizing, separate path.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PositionSize:
    fraction_of_bankroll: float    # fraction of total bankroll to deploy
    contracts: int                 # contract count (rounded)
    notional: float                # dollar cost = contracts * price
    capped: bool                   # True if hard cap applied
    cap_reason: str | None = None


def full_kelly_fraction(p_model: float, price: float) -> float:
    """Full Kelly fraction for binary outcome.
    f* = (b*p - q) / b, where b = (1-price)/price (odds), p = p_model, q = 1-p.
    For a contract at `price` paying $1 if correct:
      f* = (p_model - price) / (1 - price)   [simplified for binary $1 payout]
    """
    if price <= 0 or price >= 1:
        return 0.0
    if p_model <= price:
        return 0.0  # no edge
    return (p_model - price) / (1 - price)


def fractional_kelly(p_model: float, price: float, fraction: float = 0.15) -> float:
    """Apply fractional Kelly (default 0.15 = Modified Kelly for incumbent)."""
    return full_kelly_fraction(p_model, price) * fraction


def cap_position(
    fraction: float,
    bankroll: float,
    price: float,
    max_position_pct: float = 0.05,
    max_correlated_pct: float = 0.10,
    current_correlated_exposure: float = 0.0,
) -> PositionSize:
    """Apply hard caps to Kelly-derived fraction.
    - max_position_pct: max 5% bankroll on single position
    - max_correlated_pct: max 10% on correlated group (e.g. one country's elections)
    """
    capped = False
    reason = None
    capped_fraction = fraction

    # Single position cap
    if fraction > max_position_pct:
        capped_fraction = max_position_pct
        capped = True
        reason = f"single_position_cap_{max_position_pct}"

    # Correlated exposure cap
    remaining_correlated = max_correlated_pct - current_correlated_exposure
    if capped_fraction > remaining_correlated:
        capped_fraction = max(0.0, remaining_correlated)
        capped = True
        reason = f"correlated_cap_{max_correlated_pct}"

    if capped_fraction <= 0:
        return PositionSize(0.0, 0, 0.0, True, "no_correlated_budget")

    notional = capped_fraction * bankroll
    contracts = max(1, int(notional / price)) if price > 0 else 0
    actual_notional = contracts * price
    actual_fraction = actual_notional / bankroll if bankroll > 0 else 0

    return PositionSize(
        fraction_of_bankroll=actual_fraction,
        contracts=contracts,
        notional=actual_notional,
        capped=capped or (actual_fraction < fraction),
        cap_reason=reason,
    )


def size_incumbent(p_model: float, price: float, bankroll: float,
                   current_country_exposure: float = 0.0) -> PositionSize:
    """Pattern #3: Modified Kelly 0.15, hard cap 5%, correlated cap 10% per country."""
    kelly_frac = fractional_kelly(p_model, price, fraction=0.15)
    return cap_position(
        fraction=kelly_frac, bankroll=bankroll, price=price,
        max_position_pct=0.05, max_correlated_pct=0.10,
        current_correlated_exposure=current_country_exposure,
    )


def size_narrative_fade(p_model: float, price: float, bankroll: float) -> PositionSize:
    """Pattern #2: fractional Kelly 0.25, hard cap 5% (smaller — thin liquidity)."""
    kelly_frac = fractional_kelly(p_model, price, fraction=0.25)
    return cap_position(
        fraction=kelly_frac, bankroll=bankroll, price=price,
        max_position_pct=0.05, max_correlated_pct=0.05,
        current_correlated_exposure=0.0,
    )


def size_creep(p_model: float, price: float, bankroll: float) -> PositionSize:
    """Pattern #4: fractional Kelly 0.25, hard cap 3% (adverse selection risk)."""
    kelly_frac = fractional_kelly(p_model, price, fraction=0.25)
    return cap_position(
        fraction=kelly_frac, bankroll=bankroll, price=price,
        max_position_pct=0.03, max_correlated_pct=0.05,
        current_correlated_exposure=0.0,
    )
