# Contributing to PicX MCP

## Setup

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Type-Think-AI/picx-mcp.git
cd picx-mcp
uv sync              # installs exact pinned dependencies
cp .env.example .env # then fill in at minimum PICX_API_BASE and REDIS_URL
```

**Use `uv`, not `pip`.** FastMCP documents a pip-specific breakage: upgrading across major versions can leave a half-removed install because code moved between distributions. `uv` uninstalls before installing and is unaffected.

Verify the install:

```bash
uv run fastmcp version   # should print FastMCP 4.0.0b3, MCP 2.1.1, Python 3.13.x
uv run python -m picx_mcp  # starts the server on :8000
```

## Running Tests

```bash
uv run pytest
```

Tests use FastMCP's in-memory `Client` (pass the server object directly — no network, no credits spent) and [respx](https://github.com/lundberg/respx) to mock `httpx` calls to `/v1`.

**No live API calls in CI or in `pytest`.** Every generation costs real credits. The test suite validates tool schemas, input validation, error handling, and response shaping — all against mocked HTTP responses.

There is an opt-in smoke script for manual verification against the real API:

```bash
PICX_API_KEY=pxsk_... uv run python tests/smoke.py
```

This generates one image end-to-end. Never run it in CI.

## Adding a Tool

Every tool module lives in `src/picx_mcp/tools/` and exposes exactly one function:

```python
def register(mcp: FastMCP) -> None:
    ...
```

`server.py` calls each module's `register()` at startup via the `MODULES` tuple in `tools/__init__.py`.

### The 6 conventions

1. **Name:** `picx_<verb>_<noun>`, snake_case. The prefix avoids collision with tools from other MCP servers a client may have connected.

2. **Description is written for a model, not a human.** Say what it does, that it costs credits (or doesn't), and when NOT to use it. The description is the entire basis on which a model decides to call the tool.

3. **Annotate effects truthfully.** `readOnlyHint=False` on anything that spends credits or mutates state; `destructiveHint=True` on deletes. Clients surface these annotations to users — a wrong hint is a broken promise.

4. **Return resource links for media, never base64.** Inlining image bytes destroys the client's context window. CDN URLs are permanent.

5. **Never log or echo an API key.** Use `client.redact()` if you must reference one in an error message.

6. **Never call a model provider directly.** Go through `PicXClient` → `/v1`, which owns pricing, credit deduction, refund-on-failure, and request logging. Reimplementing any of that risks a billing divergence.

### Adding a new module

1. Create `src/picx_mcp/tools/your_module.py` with a `register(mcp)` function
2. Add `"your_module"` to the `MODULES` tuple in `tools/__init__.py`
3. Write tests in `tests/test_your_module.py` with mocked HTTP
4. Run `uv run pytest` and `uv run mypy src/` before pushing

## Dependency Management

**Exact version pins only.** Production dependencies use `==` pins — never `^`, `~`, `>=`, or `*`. This is FastMCP's own recommendation for production deployments and it is non-negotiable here.

```toml
# ✅ Correct
"httpx==0.28.1"

# ❌ Never
"httpx>=0.28"
"httpx~=0.28.1"
"httpx^0.28.1"
```

To update a dependency: change the pin in `pyproject.toml`, run `uv lock`, verify `uv sync` succeeds, run the test suite, then commit both `pyproject.toml` and `uv.lock` together.

Dev dependencies (`[project.optional-dependencies] dev`) follow the same rule.

## Type Checking & Linting

```bash
uv run mypy src/
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
```

CI runs all three. Fix issues before pushing.

## Project Structure

```
src/picx_mcp/
├─ __init__.py
├─ __main__.py       # `python -m picx_mcp` entry point
├─ server.py         # FastMCP instance + ASGI app factory
├─ settings.py       # pydantic-settings, env-driven config
├─ client.py         # PicX /v1 HTTP client (the only outbound caller)
└─ tools/
   ├─ __init__.py    # MODULES list + register_all()
   ├─ images.py      # picx_generate_image, picx_edit_image
   ├─ videos.py      # picx_generate_video (task=True)
   ├─ assets.py      # upload, list, delete
   ├─ models.py      # picx_list_models (cached)
   ├─ templates.py   # search, get (cached)
   ├─ account.py     # get_account, get_usage
   └─ generations.py # picx_list_generations (blocked on backend)
```
