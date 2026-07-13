"""Kalshi REST API client — markets, orderbook, trades.

Base: https://external-api.kalshi.com/trade-api/v2
Read-only market data does NOT require RSA signing. Order placement (Phase B)
requires RSA-PSS request signing with key_id.
Docs: https://docs.kalshi.com  (llms.txt index available)
"""
from __future__ import annotations

from pm_agent.clients.rate_limit import HttpClient
from pm_agent.clients.schemas import NormalisedMarket, NormalisedOrderbook, NormalisedOutcome, NormalisedTrade, OrderbookLevel
from pm_agent.config import settings


class KalshiRestClient:
    """Kalshi v2 REST read-only client."""

    def __init__(self) -> None:
        self.http = HttpClient(
            base_url=settings.kalshi_rest_base_url,
            rps=settings.kalshi_rps,
        )

    async def get_events(self, status: str = "open", limit: int = 100, cursor: str | None = None) -> dict:
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return await self.http.get("/events", params=params)

    async def get_markets(self, status: str = "open", limit: int = 100, cursor: str | None = None) -> dict:
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return await self.http.get("/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        return await self.http.get(f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str) -> dict:
        return await self.http.get(f"/markets/{ticker}/orderbook")

    async def get_trades(self, ticker: str, limit: int = 100) -> dict:
        return await self.http.get(f"/markets/{ticker}/trades", params={"limit": limit})

    async def close(self) -> None:
        await self.http.close()

    # ---------- Normalisation ----------

    @staticmethod
    def normalise_market(raw: dict) -> NormalisedMarket:
        ticker = raw.get("ticker", "")
        return NormalisedMarket(
            venue="kalshi",
            venue_market_id=ticker,
            venue_event_id=raw.get("event_ticker"),
            title=raw.get("title") or raw.get("subtitle") or "",
            slug=ticker,
            category=raw.get("category") or None,
            status=raw.get("status"),
            open_time=raw.get("open_time") or raw.get("open_time_iso"),
            close_time=raw.get("close_time") or raw.get("close_time_iso"),
            resolve_time=raw.get("close_time") or raw.get("close_time_iso"),
            rules_text=None,  # Kalshi rules require separate /markets/{ticker} details
            resolution_source="kalshi_official",
            raw=raw,
        )

    @staticmethod
    def outcomes(raw: dict) -> list[NormalisedOutcome]:
        ticker = raw.get("ticker", "")
        result: list[NormalisedOutcome] = []
        # Kalshi binary markets: YES/NO sides
        for side in ("yes", "no"):
            result.append(NormalisedOutcome(
                venue="kalshi",
                venue_market_id=ticker,
                outcome_name=side.upper(),
                venue_token_id=f"{ticker}:{side.upper()}",
                side=side.upper(),
            ))
        return result

    @staticmethod
    def normalise_orderbook(raw: dict, ticker: str, outcome_name: str) -> NormalisedOrderbook:
        # Kalshi orderbook: {"yes": [[price,size],...], "no": [[...]]}
        side_key = outcome_name.lower()
        book = raw.get(side_key) or []
        levels: list[OrderbookLevel] = []
        for entry in book:
            if isinstance(entry, list) and len(entry) >= 2:
                levels.append(OrderbookLevel(side="ask" if outcome_name.upper() == "YES" else "bid",
                                             price=float(entry[0]), size=float(entry[1])))
        best_bid = max((l.price for l in levels if l.side == "bid"), default=None)
        best_ask = min((l.price for l in levels if l.side == "ask"), default=None)
        return NormalisedOrderbook(
            venue="kalshi",
            venue_market_id=ticker,
            outcome_name=outcome_name,
            best_bid=best_bid,
            best_ask=best_ask,
            levels=levels,
            raw=raw,
        )

    @staticmethod
    def normalise_trade(raw: dict, ticker: str, outcome_name: str) -> NormalisedTrade:
        return NormalisedTrade(
            venue="kalshi",
            venue_trade_id=str(raw.get("trade_id") or raw.get("id")),
            venue_market_id=ticker,
            outcome_name=outcome_name,
            ts_exchange=raw.get("ts") or raw.get("create_time"),
            price=float(raw.get("price", 0)),
            size=float(raw.get("count", 0)),
            side=raw.get("side"),
            raw=raw,
        )
