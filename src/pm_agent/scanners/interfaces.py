"""Scanner interface contract. Phase A: interface + stubs only.
Phase B implements actual edge logic per pattern."""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator


@dataclass
class Signal:
    """A scanner-produced trading signal. Scanners produce these; replay engine consumes."""
    market_id: str
    outcome_id: str
    side: str                          # 'BUY'/'SELL'
    limit_price: float
    size: float
    signal_time: datetime
    pattern: str                       # 'arb'|'narrative_yes_fade'|'incumbent'|'pre_resolution_creep'
    rationale: str                     # human-readable reason for audit log
    confidence: float = 0.0            # 0..1, scanner's self-assessed confidence
    model_probability: float | None = None   # p_model (for #2/#3 sizing)
    market_probability: float | None = None  # market implied p
    sizing_method: str = "fixed"       # 'fractional_kelly_0.15'|'fractional_kelly_0.25'|'paired_arb'|'fixed'

    def to_paper_order(self, fill_mode, decision_latency_sec: float = 1.0):
        """Convert Signal to PaperOrder for single-leg replay path (engine.py).
        Arb pairs use arb_engine instead."""
        from datetime import timedelta
        from pm_agent.replay.engine import PaperOrder
        return PaperOrder(
            market_id=self.market_id,
            outcome_id=self.outcome_id,
            side=self.side,
            limit_price=self.limit_price,
            size=self.size,
            signal_time=self.signal_time,
            decision_time=self.signal_time + timedelta(seconds=decision_latency_sec),
            fill_mode=fill_mode,
        )


class Scanner(abc.ABC):
    """Base scanner interface. Phase A: stubs return empty. Phase B: real logic."""

    @property
    @abc.abstractmethod
    def name(self) -> str: ...

    @abc.abstractmethod
    async def scan(self) -> AsyncIterator[Signal]:
        """Yield signals. Implementations must use point-in-time data only."""
        ...
        if False:
            yield  # pragma: no cover  (make it a generator)
