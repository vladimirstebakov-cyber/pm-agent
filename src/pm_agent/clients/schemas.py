"""Shared Pydantic models for normalised market data across venues."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


VenueId = Literal["polymarket", "kalshi"]


class NormalisedMarket(BaseModel):
    """Venue-agnostic market representation for storage."""
    venue: VenueId
    venue_market_id: str
    venue_event_id: str | None = None
    title: str
    slug: str | None = None
    category: str | None = None
    status: str | None = None
    open_time: str | None = None
    close_time: str | None = None
    resolve_time: str | None = None
    rules_text: str | None = None
    resolution_source: str | None = None
    raw: dict = Field(default_factory=dict)


class NormalisedOutcome(BaseModel):
    venue: VenueId
    venue_market_id: str
    outcome_name: str
    venue_token_id: str | None = None
    side: str | None = None  # 'YES'/'NO'


class OrderbookLevel(BaseModel):
    side: Literal["bid", "ask"]
    price: float
    size: float


class NormalisedOrderbook(BaseModel):
    venue: VenueId
    venue_market_id: str
    outcome_name: str
    ts_exchange: str | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    levels: list[OrderbookLevel] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class NormalisedTrade(BaseModel):
    venue: VenueId
    venue_trade_id: str | None = None
    venue_market_id: str
    outcome_name: str
    ts_exchange: str | None = None
    price: float
    size: float
    side: str | None = None
    raw: dict = Field(default_factory=dict)
