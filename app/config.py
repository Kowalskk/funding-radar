"""
app/config.py — Application configuration via Pydantic Settings.
Reads values from environment variables / .env file.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────
    app_name: str = "funding-radar"
    app_env: Literal["development", "staging", "production"] = "development"
    app_debug: bool = False
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: SecretStr = Field(..., min_length=16)
    allowed_origins: list[str] = ["http://localhost:3000"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v):
        if isinstance(v, str):
            # Handle both JSON array and comma-separated strings
            v = v.strip()
            if v.startswith("["):
                import json
                return json.loads(v)
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        if isinstance(v, list):
            return v
        return [str(v)]

    # ── PostgreSQL / TimescaleDB ──────────────────────────
    database_url: str = Field(
        ...,
        description="Async DSN: postgresql+asyncpg://user:pass@host:port/db",
    )
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout: int = 30

    # ── Redis ─────────────────────────────────────────────
    redis_url: str = Field(..., description="redis://:pass@host:port/db")
    redis_max_connections: int = 20
    cache_ttl_seconds: int = 30

    # ── Auth / JWT ────────────────────────────────────────
    jwt_secret_key: SecretStr = Field(..., min_length=16)
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30

    # ── Stripe ────────────────────────────────────────────
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    stripe_price_id_basic: str | None = None
    stripe_price_id_pro: str | None = None

    # ── Telegram ──────────────────────────────────────────
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # ── DEX API Endpoints ─────────────────────────────────
    hyperliquid_api_url: str = "https://api.hyperliquid.xyz"
    hyperliquid_ws_url: str = "wss://api.hyperliquid.xyz/ws"
    aster_api_url: str = "https://fapi.asterdex.com"
    aster_ws_url: str = "wss://fstream.asterdex.com"

    # ── Scheduler ─────────────────────────────────────────
    scheduler_funding_rate_interval_seconds: int = 10
    scheduler_cache_cleanup_interval_minutes: int = 60

    # ── Logging ───────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # ── Computed properties ───────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()
