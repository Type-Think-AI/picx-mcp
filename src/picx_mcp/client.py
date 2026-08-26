"""The PicX `/v1` HTTP client — the only thing in this service that talks to PicX.

## Why this is a thin HTTP client and not a model integration

`/v1` already owns everything that matters about a generation. Verified in
`web-app/api/app/public_api/router.py`:

    auth + rate limit + daily credit cap
      -> scope check
      -> price from config (an unpriced size is REJECTED, never guessed)
      -> apply discount
      -> idempotency check
      -> DEDUCT CREDITS
      -> call provider
      -> persist output_url
      -> REFUND CREDITS on provider failure  (transaction_type="api_refund")
      -> write request log

Reimplementing any of that here would eventually diverge, and a divergence in
money logic is a billing bug: silent, and permanently trust-eroding. So this
client's entire job is to forward a caller's credential to `/v1` and hand back
what it says.

It is also deliberately incapable of reaching PicX's *session* routes, which
accept `pxsk_` keys without enforcing scopes, rate limits or the credit cap.
The base URL is pinned to `/v1` with no escape hatch.
"""

from __future__ import annotations

from typing import Any

import httpx

from .settings import get_settings


class PicXError(Exception):
    """A `/v1` call failed. Carries the status so callers can map it faithfully."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload

    @property
    def is_insufficient_credits(self) -> bool:
        return self.status_code == 402 or "credit" in str(self).lower()

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429


def redact(token: str | None) -> str:
    """`pxsk_…last4`. Never let a full key reach a log or an error message."""
    if not token:
        return "<none>"
    return f"pxsk_…{token[-4:]}"


class PicXClient:
    """One instance per request, carrying that request's credential.

    Deliberately NOT a long-lived singleton holding a service credential: each
    MCP call forwards the identity of whoever made it, so a key this service
    never stores is a key it cannot leak.
    """

    def __init__(self, api_key: str, *, base_url: str | None = None) -> None:
        if not api_key:
            raise PicXError("no API key supplied", status_code=401)
        settings = get_settings()
        self.api_key = api_key
        self.base_url = (base_url or settings.picx_api_base).rstrip("/")
        if not self.base_url.endswith("/v1"):
            raise PicXError(f"base_url must end in /v1 (got {self.base_url!r})")
        self._timeout = settings.picx_api_timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "picx-mcp/0.1.0",
        }

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Call `/v1`. `path` is relative, e.g. `/images/generate`."""
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        # Drop None values — the API distinguishes "absent" from "null" on several
        # optional fields, and sending null where absent was meant changes behaviour.
        payload = {k: v for k, v in (json or {}).items() if v is not None} if json else None
        query = {k: v for k, v in (params or {}).items() if v is not None} if params else None

        async with httpx.AsyncClient(timeout=timeout or self._timeout) as http:
            try:
                resp = await http.request(
                    method, url, headers=self._headers(), json=payload, params=query
                )
            except httpx.TimeoutException as exc:
                raise PicXError(f"timed out calling {path}", status_code=504) from exc
            except httpx.HTTPError as exc:
                raise PicXError(f"network error calling {path}: {exc}", status_code=502) from exc

        if resp.status_code >= 400:
            detail: Any
            try:
                body = resp.json()
                detail = body.get("detail") or body
            except Exception:
                detail = resp.text[:500]
            raise PicXError(str(detail), status_code=resp.status_code, payload=detail)

        if not resp.content:
            return None
        try:
            return resp.json()
        except Exception:
            return resp.text

    # Thin verbs, so tool modules read as intent rather than plumbing.
    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def upload(self, *, content: bytes, filename: str, mime: str) -> Any:
        """`POST /v1/assets` — multipart, so it bypasses the JSON path above.

        Load-bearing: `/v1/images/edit` rejects data URIs, so every local file
        must become an https URL here before it can be edited or used as a frame.
        """
        url = f"{self.base_url}/assets"
        headers = {"Authorization": f"Bearer {self.api_key}", "User-Agent": "picx-mcp/0.1.0"}
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            resp = await http.post(
                url, headers=headers, files={"file": (filename, content, mime)}
            )
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except Exception:
                detail = resp.text[:500]
            raise PicXError(str(detail), status_code=resp.status_code)
        return resp.json()
