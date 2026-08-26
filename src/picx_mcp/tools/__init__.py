"""Tool registration contract.

Every module in this package exposes exactly one function:

    def register(mcp: FastMCP) -> None

`server.py` calls each in turn. That keeps tool definitions next to their
handlers, lets modules be developed independently, and means adding a tool
touches one file plus the list in `MODULES` below.

## Conventions every tool must follow

1. **Name:** `picx_<verb>_<noun>`, snake_case.
2. **Description is written for a MODEL, not a human.** Say what it does, that it
   costs credits, and when NOT to use it. This text is the entire basis on which
   a model decides to call the tool.
3. **Annotate effects truthfully.** `readOnlyHint=False` on anything that spends
   credits or mutates state; `destructiveHint=True` on deletes. Clients surface
   these to users, so a wrong hint is a broken promise.
4. **Return resource links for media, never base64.** Inlining bytes destroys the
   client's context window and CDN URLs are permanent anyway.
5. **Never log or echo an API key.** Use `client.redact()`.
6. **Never call a model provider directly.** Go through `PicXClient` -> `/v1`,
   which owns pricing, credit deduction, refund-on-failure and logging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from fastmcp import FastMCP

# Registration order = the order tools tend to be displayed by clients.
MODULES = (
    "images",
    "videos",
    "assets",
    "models",
    "templates",
    "account",
    "generations",
)


def register_all(mcp: "FastMCP") -> list[str]:
    """Import every module in `MODULES` and call its `register(mcp)`.

    Returns the module names that registered, so `server.py` can log what is
    actually live rather than what was intended.
    """
    import importlib

    registered: list[str] = []
    for name in MODULES:
        module = importlib.import_module(f".{name}", package=__name__)
        register = getattr(module, "register", None)
        if register is None:
            raise RuntimeError(
                f"picx_mcp.tools.{name} has no register(mcp) — see the contract in "
                f"picx_mcp/tools/__init__.py"
            )
        register(mcp)
        registered.append(name)
    return registered
