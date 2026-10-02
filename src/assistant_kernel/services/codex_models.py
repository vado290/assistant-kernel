from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


CONTEXT_POLICIES = {"model_default", "max", "custom"}


@dataclass(frozen=True)
class ResolvedCodexModel:
    provider: str
    model: str
    reasoning_effort: str | None
    context_policy: str
    model_context_window_tokens: int | None
    model_auto_compact_token_limit: int | None
    effective_context_window_tokens: int | None
    catalog_context_window_tokens: int | None
    catalog_max_context_window_tokens: int | None
    effective_context_window_percent: int | None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class CodexModelCatalog:
    """Resolve model-dependent limits from the active Codex CLI catalog."""

    _cache: dict[tuple[str, str], tuple[float, tuple[dict[str, Any], ...]]] = {}

    def __init__(self, codex_binary: str, cache_ttl_seconds: int = 300):
        self.codex_binary = codex_binary
        self.cache_ttl_seconds = cache_ttl_seconds

    async def list_models(self, codex_home: Path) -> tuple[dict[str, Any], ...]:
        key = (self.codex_binary, str(codex_home.resolve()))
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < self.cache_ttl_seconds:
            return cached[1]
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        process = await asyncio.create_subprocess_exec(
            self.codex_binary,
            "debug",
            "models",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace")[-1000:]
            raise RuntimeError(f"Codex model catalog failed: {detail}")
        payload = json.loads(stdout.decode("utf-8"))
        models = tuple(
            dict(item) for item in payload.get("models") or []
            if isinstance(item, dict) and str(item.get("slug") or "")
        )
        if not models:
            raise RuntimeError("Codex model catalog returned no models")
        self._cache[key] = (now, models)
        return models

    @staticmethod
    def public_projection(item: dict[str, Any]) -> dict[str, Any]:
        default_window = int(item.get("context_window") or 0)
        maximum_window = int(item.get("max_context_window") or default_window)
        effective_percent = int(item.get("effective_context_window_percent") or 100)
        return {
            "model": str(item["slug"]),
            "display_name": str(item.get("display_name") or item["slug"]),
            "priority": int(item.get("priority") or 9999),
            "catalog_context_window_tokens": default_window,
            "catalog_max_context_window_tokens": maximum_window,
            "effective_default_context_window_tokens": (
                default_window * effective_percent // 100
            ),
            "effective_max_context_window_tokens": (
                maximum_window * effective_percent // 100
            ),
            "effective_context_window_percent": effective_percent,
            "default_reasoning_effort": item.get("default_reasoning_level"),
            "supported_reasoning_efforts": [
                str(level.get("effort"))
                for level in item.get("supported_reasoning_levels") or []
                if isinstance(level, dict) and level.get("effort")
            ],
            "supported_in_api": bool(item.get("supported_in_api", True)),
        }

    async def resolve(
        self,
        *,
        codex_home: Path,
        provider: str,
        model: str | None,
        reasoning_effort: str | None,
        context_policy: str,
        custom_context_window_tokens: int | None = None,
        custom_auto_compact_token_limit: int | None = None,
    ) -> ResolvedCodexModel:
        if context_policy not in CONTEXT_POLICIES:
            raise ValueError(
                "context_policy must be model_default, max, or custom"
            )
        if provider != "openai":
            if not model:
                raise ValueError("non-OpenAI Codex providers require an explicit model")
            if context_policy != "model_default":
                raise ValueError(
                    "max/custom context policy requires provider model catalog metadata"
                )
            return ResolvedCodexModel(
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort,
                context_policy=context_policy,
                model_context_window_tokens=None,
                model_auto_compact_token_limit=None,
                effective_context_window_tokens=None,
                catalog_context_window_tokens=None,
                catalog_max_context_window_tokens=None,
                effective_context_window_percent=None,
            )

        models = await self.list_models(codex_home)
        candidates = [item for item in models if bool(item.get("supported_in_api", True))]
        if model:
            selected = next(
                (item for item in candidates if str(item.get("slug")) == model), None
            )
            if selected is None:
                raise ValueError(f"model is not available in the active Codex catalog: {model}")
        else:
            selected = min(
                candidates,
                key=lambda item: (
                    int(item.get("priority") or 9999), str(item.get("slug") or "")
                ),
            )

        selected_model = str(selected["slug"])
        default_window = int(selected.get("context_window") or 0)
        maximum_window = int(selected.get("max_context_window") or default_window)
        if default_window <= 0 or maximum_window < default_window:
            raise RuntimeError(f"Codex catalog has invalid context metadata: {selected_model}")
        if context_policy == "model_default":
            requested_window = default_window
        elif context_policy == "max":
            requested_window = maximum_window
        else:
            if custom_context_window_tokens is None or custom_context_window_tokens <= 0:
                raise ValueError("custom context policy requires a positive context window")
            if custom_context_window_tokens > maximum_window:
                raise ValueError(
                    f"custom context window exceeds {selected_model} maximum "
                    f"({custom_context_window_tokens} > {maximum_window})"
                )
            requested_window = custom_context_window_tokens

        maximum_compact = requested_window * 90 // 100
        catalog_compact = int(selected.get("auto_compact_token_limit") or 0)
        if custom_auto_compact_token_limit is not None:
            if not 0 < custom_auto_compact_token_limit <= maximum_compact:
                raise ValueError(
                    "custom auto-compact limit must be positive and no more than "
                    "90 percent of the resolved context window"
                )
            auto_compact = custom_auto_compact_token_limit
        elif catalog_compact > 0:
            auto_compact = min(catalog_compact, maximum_compact)
        else:
            auto_compact = maximum_compact

        supported_efforts = {
            str(level.get("effort"))
            for level in selected.get("supported_reasoning_levels") or []
            if isinstance(level, dict) and level.get("effort")
        }
        resolved_effort = reasoning_effort or selected.get("default_reasoning_level")
        if resolved_effort and supported_efforts and resolved_effort not in supported_efforts:
            raise ValueError(
                f"reasoning effort {resolved_effort} is not supported by {selected_model}"
            )
        effective_percent = int(selected.get("effective_context_window_percent") or 100)
        return ResolvedCodexModel(
            provider=provider,
            model=selected_model,
            reasoning_effort=str(resolved_effort) if resolved_effort else None,
            context_policy=context_policy,
            model_context_window_tokens=requested_window,
            model_auto_compact_token_limit=auto_compact,
            effective_context_window_tokens=requested_window * effective_percent // 100,
            catalog_context_window_tokens=default_window,
            catalog_max_context_window_tokens=maximum_window,
            effective_context_window_percent=effective_percent,
        )
