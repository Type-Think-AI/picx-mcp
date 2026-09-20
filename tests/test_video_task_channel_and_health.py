"""Two behaviours that were documented wrongly rather than implemented wrongly.

## 1. The video progress channel

`picx_generate_video` is registered `task=True`, and the comment above it used to
claim the agent "needs no polling logic — the server pushes status updates". The
same tool's own description tells the model to poll `picx_get_generation`, and
the ChatGPT submission test cases document the polling flow. Both could not be
true, and which one is live decides whether ChatGPT's video flow works at all.

The answer is that `task=True` publishes `execution.taskSupport: "optional"`, so
the CLIENT chooses. A task-capable client gets a task handle; a client that
negotiates no extension — ChatGPT, since the tasks extension is 2026-07-28-era
and OpenAI's plugin docs never mention it — gets an ordinary synchronous call and
must poll.

These tests pin BOTH halves, because the dangerous drift is silent: an SDK bump
that promoted `optional` to `required` would break ChatGPT's video flow while
every existing test still passed. Asserted on the wire, not on Python objects,
since `taskSupport` is a serialized descriptor field.

## 2. The /health tool count

`/health` reported `len(registered)` — the count of tool MODULES (8) — under a
key named `tools` (19). It is DigitalOcean's configured health check, so the
contract under test is narrow and strict: the count becomes correct AND the
status code and `status` field do not move.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from starlette.testclient import TestClient

from picx_mcp import server
from picx_mcp.settings import Settings

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    # Deliberately declares NO extension capabilities — this is the ChatGPT-like
    # client whose behaviour the whole question is about.
    "MCP-Protocol-Version": "2025-06-18",
}


def _settings(**overrides: object) -> Settings:
    base = dict(
        picx_api_base="https://api.picxstudio.com/v1",
        picx_mcp_base_url="https://mcp.picxstudio.com",
        redis_url="redis://localhost:6379",
        request_state_key="x" * 32,
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _patch_settings(settings: Settings):
    targets = (
        "picx_mcp.auth.get_settings",
        "picx_mcp.server.get_settings",
        "picx_mcp.settings.get_settings",
        "picx_mcp.openai_apps.get_settings",
    )
    patches = [patch(t, return_value=settings) for t in targets]
    for p in patches:
        p.start()
    return patches


def _rpc(client: TestClient, body: dict) -> dict:
    resp = client.post("/", json=body, headers=_MCP_HEADERS)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    if "text/event-stream" in resp.headers.get("content-type", ""):
        frames = [
            line[len("data:") :].strip()
            for line in resp.text.splitlines()
            if line.startswith("data:")
        ]
        assert frames, f"no SSE data frame: {resp.text[:300]}"
        message = json.loads(frames[-1])
    else:
        message = resp.json()
    assert "error" not in message, message["error"]
    return message["result"]


class TestVideoTaskChannel:
    def test_task_support_is_optional_not_required(self) -> None:
        """`taskSupport` must stay "optional" so a non-task client still works.

        If a future SDK promotes this to "required", a client that cannot do
        tasks — ChatGPT — would be unable to call the tool at all, and the
        polling flow our submission test cases document would be dead. That
        regression is invisible to every other test, so it is pinned here.
        """
        patches = _patch_settings(_settings())
        try:
            app = server.build_app()
            with TestClient(app) as client:
                result = _rpc(
                    client,
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                )
        finally:
            for p in patches:
                p.stop()

        by_name = {t["name"]: t for t in result["tools"]}
        execution = by_name["picx_generate_video"].get("execution")
        assert execution is not None, (
            "picx_generate_video publishes no `execution` field, so task=True "
            "is no longer taking effect at all."
        )
        assert execution.get("taskSupport") == "optional", (
            f"taskSupport must be 'optional' so non-task clients keep working; "
            f"got {execution!r}."
        )

    def test_body_runs_synchronously_without_task_augmentation(self) -> None:
        """A plain tools/call must execute the tool, not hand back a task handle.

        Proven by asserting on the tool's OWN validation message: reaching
        "image_url is required when mode='image'" is only possible if the
        function body actually ran in-band. A task handle would short-circuit
        before any validation, so this is the assertion that distinguishes the
        two channels rather than merely observing a non-error.
        """
        patches = _patch_settings(_settings())
        try:
            app = server.build_app()
            with TestClient(app) as client:
                result = _rpc(
                    client,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "picx_generate_video",
                            # mode="image" without image_url — rejected by our own
                            # validation before any network call is attempted.
                            "arguments": {"mode": "image", "prompt": "a test"},
                        },
                    },
                )
        finally:
            for p in patches:
                p.stop()

        blob = json.dumps(result)
        assert "taskId" not in blob and "task_id" not in blob, (
            "A client that negotiated no tasks extension got a task handle back; "
            f"ChatGPT's video flow would be broken. Result: {blob[:300]}"
        )
        assert result.get("isError") is True
        text = " ".join(block.get("text", "") for block in result.get("content", []))
        assert "image_url is required" in text, (
            "Expected the tool's own validation error, which only appears if the "
            f"body executed in-band. Got: {text!r}"
        )

    def test_description_still_tells_the_model_to_poll(self) -> None:
        """The polling instruction is load-bearing for the client that matters.

        ChatGPT gets the synchronous 202 and has no task channel, so if this
        guidance is ever dropped from the description the model will treat the
        immediate response as a finished video.
        """
        patches = _patch_settings(_settings())
        try:
            app = server.build_app()
            with TestClient(app) as client:
                result = _rpc(
                    client,
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
                )
        finally:
            for p in patches:
                p.stop()

        description = {t["name"]: t["description"] for t in result["tools"]}[
            "picx_generate_video"
        ]
        assert "picx_get_generation" in description
        assert "Poll" in description or "poll" in description


class TestHealthToolCount:
    def test_reports_tools_not_modules(self) -> None:
        """`tools` must be the tool count, and must match tools/list exactly.

        It previously reported the number of tool MODULES (8) under this key
        while 19 tools were registered. Tied to the live tools/list result rather
        than a literal so adding a tool cannot silently desynchronise them again.
        """
        patches = _patch_settings(_settings())
        try:
            app = server.build_app()
            with TestClient(app) as client:
                health = client.get("/health")
                listed = _rpc(
                    client,
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {}},
                )
        finally:
            for p in patches:
                p.stop()

        body = health.json()
        assert body["tools"] == len(listed["tools"]), (
            f"/health says {body['tools']} tools, tools/list returns "
            f"{len(listed['tools'])}"
        )
        assert body["modules"] == 8, (
            "the old module count should still be reported, under its own key"
        )

    def test_health_contract_for_digitalocean_is_unchanged(self) -> None:
        """200 + `status: healthy` is DO's probe — changing it restarts the app."""
        patches = _patch_settings(_settings())
        try:
            app = server.build_app()
            with TestClient(app) as client:
                resp = client.get("/health")
        finally:
            for p in patches:
                p.stop()

        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"
        assert resp.json()["service"] == "picx-mcp"
