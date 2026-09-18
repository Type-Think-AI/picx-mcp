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
    # Topology (revised 2026-09-18): picx-studio is the OAuth 2.1 authorization
    # server; this connector is a PURE RESOURCE SERVER. It verifies tokens
    # picx-studio minted and holds no upstream client credential of its own.
    picx_mcp_base_url: str = "https://mcp.picxstudio.com"

    picx_auth_issuer: str | None = Field(
        default=None,
        description=(
            "The OAuth 2.1 authorization server that mints the tokens this "
            "connector verifies. Compared verbatim against a token's `iss` "
            "claim and used to fetch JWKS at {issuer}/.well-known/jwks.json "
            "(RFC 8414 discovery, issuer on the API host). Decided value: "
            "https://api.picxstudio.com. Unset → OAuth is off and the server "
            "runs in pxsk_ passthrough mode advertising no OAuth surface."
        ),
    )

    picx_internal_secret: str | None = Field(
        default=None,
        description=(
            "Shared service-to-service secret sent as X-PicX-Internal-Secret to "
            "POST /api/internal/session-keys/resolve, which exchanges a verified "
            "OAuth subject claim for a scoped PicX session key. Must match "
            "MCP_INTERNAL_SECRET on the PicX API. Never logged."
        ),
    )

    # ── OAuth scaffolding retained only because tests / config may reference it ─
    # These belonged to the withdrawn topology where the connector fronted Google
    # as an OAuthProxy issuer. They are NO LONGER passed to any provider — a
    # resource server issues nothing and stores no upstream refresh token — but
    # the fields stay so an existing .env carrying them does not fail to load.
    google_client_id: str | None = None
    google_client_secret: str | None = None
    jwt_signing_key: str | None = Field(
        default=None,
        description=(
            "Unused under the resource-server topology (was the issuer's JWT "
            "signing key). Token verification now uses the issuer's JWKS, not a "
            "local signing key. Retained so a stale .env still loads."
        ),
    )
    storage_encryption_key: str | None = Field(
        default=None,
        description=(
            "Unused under the resource-server topology (was the Fernet key for "
            "upstream-token storage). A resource server holds no upstream token. "
            "Retained so a stale .env still loads."
        ),
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
        """True when the resource server has what it needs to verify tokens.

        Under the resource-server topology that is exactly the issuer: from it
        the connector derives the JWKS URI, the expected `iss`, and the
        authorization server it names in protected-resource metadata. The old
        preconditions (google_client_id/secret, jwt_signing_key,
        storage_encryption_key) belonged to the issuer role picx-studio now
        owns, so they are no longer required here.

        Fail-closed: with the issuer unset, build_auth() returns None and the
        server stays in pxsk_ passthrough mode advertising no OAuth surface.
        """
        return bool(self.picx_auth_issuer)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
