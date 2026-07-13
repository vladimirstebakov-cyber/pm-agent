"""Polymarket Gamma API client — market discovery (read-only, no auth).

Docs: https://docs.polymarket.com (Gamma: market discovery, no auth required).
Rate limit orientation from secondary sources (~4000/10s) — treated as
configurable, not hardcoded truth.
"""
from __future__ import annotations

import hashlib
import json

from pm_agent.clients.rate_limit import HttpClient
from pm_agent.clients.schemas import NormalisedMarket, NormalisedOutcome
from pm_agent.config import settings


class PolymarketGammaClient:
    """Market discovery + metadata + rules from Gamma API."""

    def __init__(self) -> None:
        self.http = HttpClient(
            base_url=settings.polymarket_gamma_base_url,
            rps=settings.polymarket_gamma_rps,
        )

    async def list_markets(self, limit: int = 100, offset: int = 0, active_only: bool = True) -> list[dict]:
        """GET /markets — paginated market list."""
        params: dict = {"limit": limit, "offset": offset}
        if active_only:
            params["active"] = "true"
            params["closed"] = "false"
        data = await self.http.get("/markets", params=params)
        return data if isinstance(data, list) else []

    async def get_market(self, condition_id: str) -> dict:
        """GET /markets/{condition_id} — single market details."""
        data = await self.http.get(f"/markets/{condition_id}")
        return data if isinstance(data, dict) else {}

    async def list_events(self, limit: int = 100, offset: int = 0) -> list[dict]:
        data = await self.http.get("/events", params={"limit": limit, "offset": offset})
        return data if isinstance(data, list) else []

    async def close(self) -> None:
        await self.http.close()

    # ---------- Normalisation ----------

    @staticmethod
    def normalise(raw: dict) -> NormalisedMarket:
        cid = raw.get("conditionId") or raw.get("condition_id") or raw.get("id")
        return NormalisedMarket(
            venue="polymarket",
            venue_market_id=str(cid),
            venue_event_id=str(raw.get("slug")) if raw.get("slug") else None,
            title=raw.get("question") or raw.get("title") or "",
            slug=raw.get("slug"),
            category=raw.get("category") or None,
            status=("active" if raw.get("active") else "closed") if raw.get("active") is not None else None,
            open_time=raw.get("startDate"),
            close_time=raw.get("endDate"),
            resolve_time=raw.get("endDate"),
            rules_text=raw.get("description") or raw.get("rules"),
            resolution_source=raw.get("resolutionSource") or None,
            raw=raw,
        )

    @staticmethod
    def outcomes(raw: dict) -> list[NormalisedOutcome]:
        cid = str(raw.get("conditionId") or raw.get("condition_id") or raw.get("id"))
        outcomes = raw.get("outcomes") or []
        tokens = raw.get("clobTokenIds") or raw.get("clobtokenids") or []
        result: list[NormalisedOutcome] = []
        names = outcomes if isinstance(outcomes, list) else []
        for i, name in enumerate(names):
            token = tokens[i] if i < len(tokens) else None
            result.append(NormalisedOutcome(
                venue="polymarket",
                venue_market_id=cid,
                outcome_name=str(name),
                venue_token_id=str(token) if token else None,
                side=str(name).upper() if str(name).upper() in ("YES", "NO") else None,
            ))
        return result


def payload_hash(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
