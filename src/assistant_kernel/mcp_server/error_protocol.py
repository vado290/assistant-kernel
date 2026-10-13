"""Uniform, machine-readable failure payloads for every MCP tool.

The MCP boundary used to convert any unhandled exception into a bare
``Error executing tool <name>`` text: the reason never reached the agent and
no traceback reached the service logs.  Every tool registered after
:func:`install_error_protocol` is wrapped so a failure

* returns a structured JSON payload through the normal result channel
  (framework error channels differ between MCP SDK forks and are not trusted),
* logs the full traceback server side, keyed by tool name and timestamp so it
  can be correlated with ``runs/agent-traces/*.jsonl``.

Success results pass through untouched, signatures and docstrings are
preserved for schema generation, and cancellation is never converted.
"""
from __future__ import annotations

import functools
import inspect
import json
import logging
from typing import Any, Callable


LOGGER = logging.getLogger("assistant_kernel.mcp_error_protocol")

HINTS: tuple[tuple[type[BaseException], str], ...] = (
    (
        PermissionError,
        "权限或 Scope 边界拒绝：核对当前 Run 的授权快照、grant 账本与用户审批消息后重试。",
    ),
    (
        FileNotFoundError,
        "目标文件、目录或 Run 不存在，或不在授权范围内。",
    ),
    (
        KeyError,
        "引用了未知的键（root key、discovery id、project id 等）。",
    ),
    (
        ValueError,
        "参数或状态约束不满足：阅读 message 中的具体约束，修正后再重试。",
    ),
    (
        TimeoutError,
        "执行超时：缩小范围或拆分任务后重试。",
    ),
)


def hint_for(exc: BaseException) -> str:
    for kind, hint in HINTS:
        if isinstance(exc, kind):
            return hint
    return "未分类错误：按时间点在 kernel-mcp / kernel-worker 日志中查同名字工具的 traceback。"


def error_payload(exc: BaseException, tool_name: str) -> str:
    return json.dumps(
        {
            "ok": False,
            "tool": tool_name,
            "error_type": type(exc).__name__,
            "message": str(exc),
            "hint": hint_for(exc),
        },
        ensure_ascii=False,
        indent=2,
    )


def tool_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap one tool function so failures become structured results."""
    name = getattr(fn, "__name__", "tool")

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - boundary converter
                LOGGER.exception("tool %s failed", name)
                return error_payload(exc, name)
        return async_wrapper

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - boundary converter
            LOGGER.exception("tool %s failed", name)
            return error_payload(exc, name)
    return sync_wrapper


def install_error_protocol(server: Any) -> None:
    """Wrap every tool registered on ``server`` after this call.

    Single hook point: no per-tool edits, and tools registered later (including
    conditionally registered scoped tools) inherit the protocol automatically.
    """
    original = server.tool

    def tool(*args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Any]:
        register = original(*args, **kwargs)

        def decorator(fn: Callable[..., Any]) -> Any:
            return register(tool_errors(fn))

        return decorator

    server.tool = tool


def v12_tool_errors(*, mutating: bool = False):
    """Strict new-tool DTO while retaining the legacy ABI for unadopted tools."""
    def decorate(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                # Mutation may have committed before an I/O failure. Do not tell
                # a model to blindly retry an uncertain side effect.
                effect = "unknown" if mutating else "none"
                if isinstance(exc, PermissionError):
                    code = "SCOPE_DENIED"; effect = "not_started"
                elif isinstance(exc, ValueError):
                    code = "CONFLICT" if "stale" in str(exc).lower() else "INVALID_INPUT"
                elif isinstance(exc, FileNotFoundError):
                    code = "SOURCE_CHANGED"
                else:
                    code = "EFFECT_UNKNOWN" if mutating else "EXECUTION_FAILED"
                message = str(exc)[:1500] if isinstance(exc,(ValueError,PermissionError,FileNotFoundError)) else type(exc).__name__ + ": operation did not produce a verified receipt"
                LOGGER.warning("v1.2 tool %s failed: %s",fn.__name__,type(exc).__name__)
                result={"ok":False,"tool":fn.__name__,"error_type":code,"message":message,
                        "hint":"先查询当前状态及原操作回执；效果未确定时不要重放。" if mutating else "核对参数、固定引用和当前能力后读取。",
                        "effect_status":effect,"receipt_refs":[]}
                from assistant_kernel.services.contracts import contracts
                contracts().validate("ToolError",result)
                return result
        return wrapped
    return decorate
