"""Change 1 (P0): MCP tool failures must become structured, visible results."""
from __future__ import annotations

import asyncio
import inspect
import json

from assistant_kernel.mcp_server.error_protocol import (
    install_error_protocol,
    tool_errors,
)


def test_async_failure_returns_structured_payload():
    @tool_errors
    async def boom(message: str) -> dict:
        raise PermissionError(message)

    assert inspect.iscoroutinefunction(boom)
    payload = json.loads(asyncio.run(boom("the user message did not name this folder")))
    assert payload["ok"] is False
    assert payload["tool"] == "boom"
    assert payload["error_type"] == "PermissionError"
    assert payload["message"] == "the user message did not name this folder"
    assert payload["hint"]


def test_sync_failure_returns_structured_payload():
    @tool_errors
    def boom() -> dict:
        raise ValueError("source range is too large for a bounded native Scope; narrow the folder")

    assert not inspect.iscoroutinefunction(boom)
    payload = json.loads(boom())
    assert payload["error_type"] == "ValueError"
    assert "source range" in payload["message"]
    assert "约束" in payload["hint"]


def test_success_passes_through_unchanged():
    sentinel = {"ok": True}

    @tool_errors
    async def fine() -> dict:
        return sentinel

    assert asyncio.run(fine()) is sentinel


def test_signature_and_docstring_preserved_for_schema_generation():
    @tool_errors
    async def documented(alpha: int, beta: str = "x") -> dict:
        """Docstring stays visible for schema generation."""
        return {}

    assert list(inspect.signature(documented).parameters) == ["alpha", "beta"]
    assert "Docstring" in documented.__doc__
    assert documented.__name__ == "documented"


def test_install_error_protocol_wraps_later_registrations():
    registered: dict = {}

    class FakeServer:
        def tool(self, *args, **kwargs):
            def register(fn):
                registered[fn.__name__] = fn
                return fn
            return register

    server = FakeServer()
    install_error_protocol(server)

    @server.tool(annotations={"readOnlyHint": True})
    async def scoped_read(uri: str) -> dict:
        raise FileNotFoundError(uri)

    @server.tool()
    def sync_write(uri: str) -> dict:
        raise KeyError("alpaca_history_scripts")

    payload = json.loads(asyncio.run(registered["scoped_read"]("workspace://project/nope")))
    assert payload["error_type"] == "FileNotFoundError"
    assert payload["tool"] == "scoped_read"
    sync_payload = json.loads(registered["sync_write"]("workspace://x/y"))
    assert sync_payload["error_type"] == "KeyError"


def test_cancellation_propagates_unconverted():
    @tool_errors
    async def cancelled() -> dict:
        raise asyncio.CancelledError()

    try:
        asyncio.run(cancelled())
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("CancelledError must not become a tool result")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("error protocol tests passed")
