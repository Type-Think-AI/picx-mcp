# ─────────────────────────────────────────────────────────────────────────────
# picx-mcp  •  Multi-stage build
# ─────────────────────────────────────────────────────────────────────────────
# WHY UV, NOT PIP:
# FastMCP v3→v4 split code between `fastmcp` and `fastmcp-slim` distributions.
# pip upgrade leaves orphan files from the old layout (half-removed install);
# uv unconditionally uninstalls before installing and is unaffected.
# See: https://gofastmcp.com/getting-started/installation
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: build dependencies ──────────────────────────────────────────────
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Layer-cache: install deps before copying source.
COPY pyproject.toml README.md ./
COPY uv.lock* ./

# Install into a virtual-env at /app/.venv so we can copy it cleanly.
RUN uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python --no-cache -e "."

# Now copy source and reinstall (editable resolves to src/).
COPY src/ ./src/
RUN uv pip install --python /app/.venv/bin/python --no-cache -e "."

# ── Stage 2: slim runtime ────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

RUN groupadd -r mcp && useradd -r -g mcp -d /app -s /sbin/nologin mcp

WORKDIR /app

# Bring only the venv and source from builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/pyproject.toml /app/pyproject.toml

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER mcp

EXPOSE 8000

# /health is unauthenticated by design — intended for LB probes.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "picx_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
