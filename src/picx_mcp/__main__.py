"""Run PicX MCP server: `python -m picx_mcp`.

Equivalent to `uvicorn picx_mcp.server:app` with settings from env/dotenv.
"""

from __future__ import annotations

import uvicorn

from .server import build_app
from .settings import get_settings


def main() -> None:
    settings = get_settings()
    application = build_app()
    uvicorn.run(
        application,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
    )


if __name__ == "__main__":
    main()
