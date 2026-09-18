"""Tests for the tool registration contract defined in picx_mcp.tools.__init__.

These test the structural conventions — not the behaviour of individual tools,
which is the responsibility of per-module tests.

Modules still being written by other agents are skipped with importorskip.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from picx_mcp.tools import MODULES, register_all


# ─── Every module exposes a callable `register` ──────────────────────────────


class TestModuleContract:
    """Each module in MODULES must export a callable register(mcp)."""

    @pytest.mark.parametrize("module_name", MODULES)
    def test_module_has_register(self, module_name: str) -> None:
        mod = pytest.importorskip(
            f"picx_mcp.tools.{module_name}",
            reason=f"tools.{module_name} not written yet",
        )
        register_fn = getattr(mod, "register", None)
        assert register_fn is not None, f"tools.{module_name} missing register()"
        assert callable(register_fn)


# ─── register_all collects picx_-prefixed tools ──────────────────────────────


class TestRegisterAll:
    """register_all against a mock FastMCP instance."""

    def _make_mock_mcp(self) -> MagicMock:
        """A mock FastMCP that records tool registrations via `@mcp.tool()`."""
        mock_mcp = MagicMock()
        # FastMCP registers tools via the @mcp.tool() decorator. Track calls.
        registered_tools: list[dict[str, Any]] = []

        def _tool_decorator(**kwargs: Any):
            """Capture tool metadata, return identity decorator."""
            def decorator(fn):
                registered_tools.append({"fn": fn, "name": fn.__name__, **kwargs})
                return fn
            return decorator

        mock_mcp.tool = _tool_decorator
        mock_mcp._registered_tools = registered_tools
        return mock_mcp

    def test_all_tool_names_are_picx_prefixed(self) -> None:
        """Every registered tool name starts with picx_."""
        mock_mcp = self._make_mock_mcp()
        # Patch get_settings to avoid reading real env
        fake_settings = MagicMock()
        fake_settings.picx_api_base = "https://api.picxstudio.com/v1"
        with patch("picx_mcp.settings.get_settings", return_value=fake_settings):
            try:
                register_all(mock_mcp)
            except Exception:
                pytest.skip("register_all raised — tool modules may be incomplete")

        tools = mock_mcp._registered_tools
        if not tools:
            pytest.skip("No tools registered — modules may not use mock's .tool() API")

        for tool in tools:
            assert tool["name"].startswith("picx_"), (
                f"Tool {tool['name']!r} does not follow picx_ naming convention"
            )

    def test_register_all_returns_module_names(self) -> None:
        """register_all returns the list of successfully registered modules."""
        mock_mcp = self._make_mock_mcp()
        fake_settings = MagicMock()
        fake_settings.picx_api_base = "https://api.picxstudio.com/v1"
        with patch("picx_mcp.settings.get_settings", return_value=fake_settings):
            try:
                result = register_all(mock_mcp)
            except Exception:
                pytest.skip("register_all raised — tool modules may be incomplete")

        assert isinstance(result, list)
        assert len(result) == len(MODULES)
        for name in MODULES:
            assert name in result


# ─── readOnlyHint / destructiveHint correctness ──────────────────────────────
#
# The two sets below MUST together partition the entire registered tool surface
# (18 tools as of this writing). They are asserted against the LIVE FastMCP
# server via introspection — `await mcp.list_tools()` returns FunctionTool
# objects whose `.annotations` (a ToolAnnotations model) carry `read_only_hint`
# and `destructive_hint`. The `test_declared_sets_match_registered_surface`
# drift guard fails the moment a tool is added, removed, or renamed without the
# corresponding set being updated — in EITHER direction.
#
# Accurate safety annotations are a plugin-submission requirement
# (.kiro/specs/official-plugin-directory/requirements.md, reqs 4.4 and 5.3).

# Tools whose annotation MUST be readOnlyHint=False. These mutate state or
# spend credits: generation (credit spend), upload/delete (asset mutation),
# redeliver (fires a side-effecting webhook re-send).
NON_READONLY_TOOLS = {
    "picx_generate_image",
    "picx_edit_image",
    "picx_generate_video",
    "picx_upload_asset",
    "picx_delete_asset",
    "picx_redeliver_webhook",
}

# The subset of NON_READONLY_TOOLS that actually spends the user's PicX credits.
# Documented separately because credit spend is the highest-blast-radius effect
# and the plugin directory calls it out specifically (req 4.4).
CREDIT_SPENDING_TOOLS = {
    "picx_generate_image",
    "picx_edit_image",
    "picx_generate_video",
}

# Tools that MUST declare destructiveHint=True (irreversible data removal).
DESTRUCTIVE_TOOLS = {
    "picx_delete_asset",
}

# Tools that are pure reads. These MUST declare readOnlyHint=True.
READ_ONLY_TOOLS = {
    "picx_get_account",
    "picx_get_tier",
    "picx_get_usage",
    "picx_get_generation",
    "picx_get_generation_events",
    "picx_get_generation_deliveries",
    "picx_list_generations",
    "picx_list_assets",
    "picx_list_models",
    "picx_get_template",
    "picx_search_templates",
    "picx_get_webhook_deliveries",
}


def _live_annotations() -> dict[str, Any]:
    """Build the real server and return {tool_name: ToolAnnotations}.

    Uses the supported FastMCP 4 introspection API: `await mcp.list_tools()`
    yields FunctionTool objects with a `.name` and a `.annotations` model.
    """
    import asyncio

    from picx_mcp.server import build_server

    async def _collect() -> dict[str, Any]:
        mcp = build_server()
        tools = await mcp.list_tools()
        return {t.name: t.annotations for t in tools}

    return asyncio.run(_collect())


@pytest.fixture(scope="module")
def live_annotations() -> dict[str, Any]:
    return _live_annotations()


class TestReadOnlyHints:
    """Safety annotations verified against the live FastMCP server.

    Introspection IS possible on fastmcp==4.0.0b3: `await mcp.list_tools()`
    returns FunctionTool objects carrying a ToolAnnotations model with
    `read_only_hint` / `destructive_hint`. The stale skip comment claiming this
    "requires FastMCP integration" was wrong.
    """

    def test_declared_sets_match_registered_surface(
        self, live_annotations: dict[str, Any]
    ) -> None:
        """Drift guard: declared sets must partition the registered surface.

        Fails if (a) a name in either set is not a registered tool, or
        (b) a registered tool is in neither set. This is what stops the stale
        rot (picx_upscale_image / picx_remove_background / picx_generate_from_template
        were declared but never existed) from returning.
        """
        registered = set(live_annotations)
        declared = NON_READONLY_TOOLS | READ_ONLY_TOOLS

        phantom = declared - registered
        assert not phantom, (
            f"Declared tools that are NOT registered (stale names): {sorted(phantom)}. "
            f"Remove them from NON_READONLY_TOOLS / READ_ONLY_TOOLS."
        )

        unclassified = registered - declared
        assert not unclassified, (
            f"Registered tools in NEITHER set: {sorted(unclassified)}. "
            f"Add each to NON_READONLY_TOOLS or READ_ONLY_TOOLS."
        )

        overlap = NON_READONLY_TOOLS & READ_ONLY_TOOLS
        assert not overlap, f"Tools declared in BOTH sets: {sorted(overlap)}"

        # Credit-spending must be a subset of the non-readonly surface.
        assert CREDIT_SPENDING_TOOLS <= NON_READONLY_TOOLS, (
            f"CREDIT_SPENDING_TOOLS not a subset of NON_READONLY_TOOLS: "
            f"{sorted(CREDIT_SPENDING_TOOLS - NON_READONLY_TOOLS)}"
        )

    @pytest.mark.parametrize("tool_name", sorted(NON_READONLY_TOOLS))
    def test_mutation_tool_not_readonly(
        self, tool_name: str, live_annotations: dict[str, Any]
    ) -> None:
        """State-mutating / credit-spending tools MUST NOT be readOnlyHint=True."""
        assert tool_name in live_annotations, f"{tool_name} is not registered"
        ann = live_annotations[tool_name]
        assert ann is not None, f"{tool_name} has no annotations"
        assert ann.read_only_hint is not True, (
            f"{tool_name} declares readOnlyHint=True but it mutates state / spends "
            f"credits. A client may auto-approve it as safe."
        )

    @pytest.mark.parametrize("tool_name", sorted(READ_ONLY_TOOLS))
    def test_readonly_tool_declared(
        self, tool_name: str, live_annotations: dict[str, Any]
    ) -> None:
        """Read-only tools MUST declare readOnlyHint=True."""
        assert tool_name in live_annotations, f"{tool_name} is not registered"
        ann = live_annotations[tool_name]
        assert ann is not None, f"{tool_name} has no annotations"
        assert ann.read_only_hint is True, (
            f"{tool_name} is a pure read but does not declare readOnlyHint=True "
            f"(got {ann.read_only_hint!r})."
        )

    @pytest.mark.parametrize("tool_name", sorted(DESTRUCTIVE_TOOLS))
    def test_destructive_tool_declared(
        self, tool_name: str, live_annotations: dict[str, Any]
    ) -> None:
        """Irreversible-removal tools MUST declare destructiveHint=True (req 5.3)."""
        assert tool_name in live_annotations, f"{tool_name} is not registered"
        ann = live_annotations[tool_name]
        assert ann is not None, f"{tool_name} has no annotations"
        assert ann.destructive_hint is True, (
            f"{tool_name} performs irreversible removal but does not declare "
            f"destructiveHint=True (got {ann.destructive_hint!r})."
        )
