"""Environment-driven configuration.

Every module reads config through `get_settings()`. Nothing reads `os.environ`
directly, so the set of knobs this service has is exactly the fields below.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── PicX API ──────────────────────────────────────────────────────────────
    picx_api_base: str = Field(
        default="https://api.picxstudio.com/v1",
        description="PicX API root. MUST end in /v1.",
    )
    picx_api_timeout: float = 180.0
    picx_api_max_retries: int = 2

    # ── State / infrastructure ────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379"

    request_state_key: str | None = Field(
        default=None,
        description=(
            "Shared secret (>=32 bytes) protecting state carried between rounds of an "
            "interactive tool call. MUST be identical on every replica: a round started "
            "on replica A and resumed on replica B is validated with this key."
        ),
    )

    # ── OAuth (Phase 5) ───────────────────────────────────────────────────────
    picx_mcp_base_url: str = "https://mcp.picxstudio.com"
    google_client_id: str | None = None
    google_client_secret: str | None = None
    jwt_signing_key: str | None = Field(
        default=None,
        description=(
            "Explicit JWT signing key. Without it FastMCP derives one from the OAuth "
            "client secret, so rotating that secret invalidates every issued token."
        ),
    )
    storage_encryption_key: str | None = Field(
        default=None,
        description="Fernet key. Without it upstream OAuth tokens are stored in plaintext.",
    )

    # ── Safety rails ──────────────────────────────────────────────────────────
    session_credit_ceiling: int = Field(
        default=2000,
        description=(
            "Max credits one MCP session may spend, independent of the account's "
            "13,000/day cap. Bounds a prompt-injected credit drain."
        ),
    )
    confirm_credit_threshold: int = Field(
        default=200,
        description="Above this, a tool returns input_required to confirm before spending.",
    )

    # ── Serving ───────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    allowed_hosts: list[str] = Field(default_factory=lambda: ["mcp.picxstudio.com"])
    log_level: str = "info"

    @field_validator("picx_api_base")
    @classmethod
    def _must_end_in_v1(cls, v: str) -> str:
        # The single most common integration mistake with this API: the bare host
        # returns 404. Fail at startup rather than on the first tool call.
        if not v.rstrip("/").endswith("/v1"):
            raise ValueError(f"picx_api_base must end in /v1 (got {v!r})")
        return v.rstrip("/")

    @property
    def oauth_configured(self) -> bool:
        """True when every value the OAuth path needs is present."""
        return all(
            (
                self.google_client_id,
                self.google_client_secret,
                self.jwt_signing_key,
                self.storage_encryption_key,
            )
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
