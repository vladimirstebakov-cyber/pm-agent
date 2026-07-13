"""Polymarket CLOB API client — executable market data (orderbook, price, trades).

Read-only endpoints work without auth for public market data.
Order placement (Phase B) requires EIP-712 wallet signing + API key.
Docs: https://docs.polymarket.com
"""
from __future__ import annotations

from pm_agent.clients.rate_limit import HttpClient
from pm_agent.clients.schemas import NormalisedOrderbook, OrderbookLevel
from pm_agent.config import settings


class PolymarketClobClient:
    """CLOB read-only market data."""

    def __init__(self) -> None:
        self.http = HttpClient(
            base_url=settings.polymarket_clob_base_url,
            rps=settings.polymarket_clob_rps,
        )

    async def get_book(self, token_id: str) -> dict:
        """GET /book?token_id=... — full order book for an outcome token."""
        return await self.http.get("/book", params={"token_id": token_id})

    async def get_price(self, token_id: str, side: str) -> dict:
        """GET /price?token_id=...&side=BUY|SELL — best executable price."""
        return await self.http.get("/price", params={"token_id": token_id, "side": side})

    async def get_midpoint(self, token_id: str) -> dict:
        return await self.http.get("/midpoint", params={"token_id": token_id})

    async def close(self) -> None:
        await self.http.close()

    @staticmethod
    def normalise_book(raw: dict, market_id: str, outcome_name: str, token_id: str) -> NormalisedOrderbook:
        bids = raw.get("bids") or raw.get("buys") or []
        asks = raw.get("asks") or raw.get("sells") or []
        levels: list[OrderbookLevel] = []
        for b in bids:
            levels.append(OrderbookLevel(side="bid", price=float(b.get("price", 0)), size=float(b.get("size", 0))))
        for a in asks:
            levels.append(OrderbookLevel(side="ask", price=float(a.get("price", 0)), size=float(a.get("size", 0))))
        best_bid = max((l.price for l in levels if l.side == "bid"), default=None)
        best_ask = min((l.price for l in levels if l.side == "ask"), default=None)
        mid = ((best_bid + best_ask) / 2) if (best_bid is not None and best_ask is not None) else None
        return NormalisedOrderbook(
            venue="polymarket",
            venue_market_id=market_id,
            outcome_name=outcome_name,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            levels=levels,
            raw=raw,
        )
