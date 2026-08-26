# PicX MCP Server

A [FastMCP 4](https://github.com/jlowin/fastmcp) server exposing PicX Studio image and video generation to any MCP client over **sessionless Streamable HTTP**.

**Hosted endpoint:** `https://mcp.picxstudio.com/mcp`
⚠️ **Not deployed yet.** The service runs locally today; production hosting is planned (see [PLAN-MCP Phase 6](https://github.com/Type-Think-AI/picx-cli/blob/main/docs/PLAN-MCP.md)).

## Why FastMCP 4

FastMCP 4's theme is *"stateless transport without stateless application code."* The protocol revision it targets — `2026-07-28` — removes session affinity entirely. Any replica behind an ordinary load balancer can serve any request. No sticky sessions, no cookie forwarding, no shared in-memory state between requests.

This is not optional for us: MCP clients (Cursor, Claude Code) use `fetch()` internally and do not forward `Set-Cookie` headers, so sticky-session load balancing cannot work regardless of LB configuration. FastMCP 4's `stateless_http=True` mode is the only viable path to horizontal scaling.

FastMCP 4 also negotiates both protocol eras (legacy SSE and modern Streamable HTTP) from a single deployment, so older clients are not stranded.

## Tool Status

| # | Tool | Status | Notes |
|---|------|--------|-------|
| 1 | `picx_generate_image` | ✅ Working | Inline, 5–20s |
| 2 | `picx_edit_image` | ✅ Working | Requires upload first (API rejects data URIs) |
| 3 | `picx_generate_video` | ✅ Working | Background task (`task=True`); text/image/reference modes only |
| 4 | `picx_get_generation` | ✅ Working | Poll a generation by ID |
| 5 | `picx_upload_asset` | ✅ Working | Returns a CDN URL usable by edit tools |
| 6 | `picx_list_assets` | ✅ Working | |
| 7 | `picx_delete_asset` | ✅ Working | |
| 8 | `picx_list_models` | ✅ Working | Cached (5 min) |
| 9 | `picx_search_templates` | ✅ Working | 50K+ catalogue; cached |
| 10 | `picx_get_template` | ✅ Working | |
| 11 | `picx_get_account` | ✅ Working | |
| 12 | `picx_get_usage` | ✅ Working | |
| 13 | `picx_list_generations` | 🔴 Blocked | `GET /v1/generations` returns 404 — endpoint not shipped yet |

### Known limitations

- **Video modes:** Only `text`, `image`, and `reference` modes are exposed. The `frames`, `extend`, `lipsync`, and `edit` modes require fields the parameter schema cannot safely serialize without dedicated validation — exposing them would surface confusing 422 errors from the API.
- **`picx_list_generations`:** Implemented and ready to activate, but blocked on the backend shipping `GET /v1/generations`.
- **Tier limits:** Per-tier rate limit and daily cap visibility may be unavailable until the account endpoint exposes them.
- **OAuth:** Not yet wired (Phase 5). API-key auth works today.

## Quickstart

```bash
# Clone and install
git clone https://github.com/Type-Think-AI/picx-mcp.git
cd picx-mcp
uv sync

# Configure
cp .env.example .env
# Edit .env — set PICX_API_KEY to your key from https://ai.picxstudio.com/api

# Run
python -m picx_mcp
```

The server starts on `http://localhost:8000`. The MCP endpoint is at `/mcp`, health at `/health`.

## Client Configuration

### Claude Desktop

```json
{
  "mcpServers": {
    "picx": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer pxsk_your_api_key_here"
      }
    }
  }
}
```

### Claude Code

```json
{
  "mcpServers": {
    "picx": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${PICX_API_KEY}"
      }
    }
  }
}
```

### Cursor

```json
{
  "mcpServers": {
    "picx": {
      "url": "http://localhost:8000/mcp",
      "headers": {
        "Authorization": "Bearer ${PICX_API_KEY}"
      }
    }
  }
}
```

### VS Code (Copilot)

```json
{
  "mcp": {
    "servers": {
      "picx": {
        "type": "http",
        "url": "http://localhost:8000/mcp",
        "headers": {
          "Authorization": "Bearer ${PICX_API_KEY}"
        }
      }
    }
  }
}
```

Replace `localhost:8000` with `mcp.picxstudio.com` once the hosted service is live.

## Authentication

Two auth planes, one enforcement point:

| | API Key (`pxsk_…`) | OAuth (Phase 5, not yet available) |
|---|---|---|
| **Who** | Developers, CI, scripted agents, self-hosters | Everyday users on hosted clients |
| **Obtained from** | [ai.picxstudio.com/api](https://ai.picxstudio.com/api) | One-click consent screen |
| **How it works** | Key forwarded per-request — the server stores no credential | OAuth resolves to a session key |
| **Revocation** | Delete the key | Revoke the grant — real keys untouched |

Both paths converge on the same `/v1` enforcement: scopes, rate limits, daily credit cap, request logging. There is no weaker second path.

**The MCP server never holds a credential.** It forwards the caller's API key (or resolved session key) to `/v1`. A key it never stores is a key it cannot leak.

## Architecture

```
MCP Client ──▶ PicX MCP Server ──▶ api.picxstudio.com/v1 ──▶ Provider + Storage
                 (this repo)         (owns everything below)
```

This server is a **translation layer**. It converts MCP tool calls into `/v1` API calls and translates results back as resource links. It intentionally does NOT:

- **Call any model provider directly.** `/v1` owns the provider integration.
- **Touch money.** `/v1` owns credit deduction, pricing, discounts, idempotency, and refund-on-provider-failure.
- **Store media.** Results are permanent CDN URLs; nothing is cached or proxied.
- **Maintain session state.** `stateless_http=True` means each request is self-contained.

Why not call providers directly? `/v1` already performs: auth → rate limit → daily cap → scope check → price from config → apply discount → idempotency check → **deduct credits** → call provider → **refund on failure** → write request log. Reimplementing any of that here would eventually diverge, and a divergence in money logic is a billing bug — silent, and permanently trust-eroding.

## Multi-Replica Testing

The whole thesis of choosing FastMCP 4 is that no session affinity is required. To prove it locally:

```bash
docker compose up --scale app=2
```

This starts two server replicas behind a round-robin proxy plus a Valkey instance. The test that validates the architecture:

1. Start an interactive tool call on replica A (triggers `InputRequiredResult`)
2. Resume the interaction — the request lands on replica B
3. It succeeds, because `REQUEST_STATE_KEY` is shared

If `REQUEST_STATE_KEY` is not set (or differs between replicas), interactive rounds will fail with a state validation error. This is intentional — it makes misconfiguration loud rather than subtly wrong.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `PICX_API_BASE` | No (default: `https://api.picxstudio.com/v1`) | PicX API root. **Must** end in `/v1`. |
| `REQUEST_STATE_KEY` | Yes (multi-replica) | ≥32 bytes, byte-identical across all replicas. Protects interactive round state. |
| `REDIS_URL` | Yes | Valkey/Redis URL. Backs tasks, response cache, OAuth storage. |
| `SESSION_CREDIT_CEILING` | No (default: 2000) | Max credits one MCP session may spend, independent of the account's daily cap. |
| `CONFIRM_CREDIT_THRESHOLD` | No (default: 200) | Above this, the tool returns `input_required` to confirm before spending. |
| `JWT_SIGNING_KEY` | Phase 5 | Explicit JWT key. Without it, tokens die when the OAuth client secret rotates. |
| `STORAGE_ENCRYPTION_KEY` | Phase 5 | Fernet key. Without it, upstream OAuth tokens are stored in plaintext. |
| `GOOGLE_CLIENT_ID` | Phase 5 | Google OAuth client ID. |
| `GOOGLE_CLIENT_SECRET` | Phase 5 | Google OAuth client secret. |
| `PICX_MCP_BASE_URL` | Phase 5 (default: `https://mcp.picxstudio.com`) | Public URL for OAuth callbacks. |

## Honest Limits

- **Every generation costs credits.** This server does not bypass pricing — that is the point.
- **Per-session ceiling (default 2000 credits)** bounds a prompt-injected credit drain. This is separate from the account's 13,000/day cap.
- **Confirmation prompt** above the threshold (default 200 credits) before spending.
- **No offline/local generation.** All generation hits the PicX API over the network.
- **Video is async.** Even with `task=True` hiding the polling, generation takes minutes — an agent must wait.
- **Rate limits are the API's**, not this server's: 60 req/min, 10K req/day by default. The MCP server adds no additional limit.
- **The server is in beta.** FastMCP 4 is `4.0.0b3`. Expect rough edges.

## License

[MIT](LICENSE)
