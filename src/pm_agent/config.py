"""pm-agent configuration — loaded from environment."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Database
    database_url: str = "postgresql://pm:pm@localhost:5432/pmagnt"

    # Polymarket
    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_data_base_url: str = "https://data-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"
    polymarket_ws_url: str = "wss://ws-subscriber-clob.polymarket.com/ws"
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_chain_id: int = 137
    polymarket_gamma_rps: float = 20
    polymarket_data_rps: float = 10
    polymarket_clob_rps: float = 40

    # Kalshi
    kalshi_rest_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_ws_url: str = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    kalshi_key_id: str = ""
    kalshi_private_key_path: str = ""
    kalshi_rps: float = 5

    # Snapshot tiers (seconds)
    snapshot_arb_sec: int = 5
    snapshot_liquid_sec: int = 15
    snapshot_preres_sec: int = 30
    snapshot_narrative_sec: int = 900
    universe_discovery_sec: int = 600
    rules_refresh_sec: int = 3600

    # Logging
    log_level: str = "INFO"
    log_json: bool = True


settings = Settings()
