"""Verify every registered MCP tool declares all three OpenAI-required hints.

Plugin submission (annotations_required) demands readOnlyHint, openWorldHint AND
destructiveHint on every tool. Introspects the live FastMCP server so it catches
tools whose annotations dict is built bare (no description key) too.
"""

from __future__ import annotations

import asyncio

from picx_mcp.server import build_server

REQUIRED = ("read_only_hint", "open_world_hint", "destructive_hint")


async def _main() -> int:
    mcp = build_server()
    tools = await mcp.list_tools()
    missing: dict[str, list[str]] = {}
    for t in tools:
        ann = t.annotations
        gaps = [
            h for h in REQUIRED
            if ann is None or getattr(ann, h, None) is None
        ]
        if gaps:
            missing[t.name] = gaps

    print(f"tools registered: {len(tools)}")
    for t in sorted(tools, key=lambda x: x.name):
        a = t.annotations
        print(
            f"  {t.name:32} "
            f"readOnly={getattr(a, 'read_only_hint', None)!s:5} "
            f"openWorld={getattr(a, 'open_world_hint', None)!s:5} "
            f"destructive={getattr(a, 'destructive_hint', None)!s:5}"
        )

    if missing:
        print("\nMISSING HINTS:")
        for name, gaps in missing.items():
            print(f"  {name}: {gaps}")
        return 1
    print("\nOK: all tools declare readOnlyHint, openWorldHint, destructiveHint")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
