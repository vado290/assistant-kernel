from pathlib import Path

import pytest

from assistant_kernel.harness.codex_models import CodexModelCatalog


MODELS = (
    {
        "slug": "large-model",
        "display_name": "Large Model",
        "priority": 1,
        "context_window": 272_000,
        "max_context_window": 872_000,
        "effective_context_window_percent": 95,
        "default_reasoning_level": "low",
        "supported_reasoning_levels": [
            {"effort": "low"},
            {"effort": "medium"},
            {"effort": "high"},
        ],
        "supported_in_api": True,
    },
    {
        "slug": "small-model",
        "display_name": "Small Model",
        "priority": 2,
        "context_window": 128_000,
        "max_context_window": 256_000,
        "effective_context_window_percent": 90,
        "default_reasoning_level": "medium",
        "supported_reasoning_levels": [{"effort": "medium"}],
        "supported_in_api": True,
    },
)


def catalog_with_models(monkeypatch) -> CodexModelCatalog:
    catalog = CodexModelCatalog("codex")

    async def fake_list_models(_codex_home: Path):
        return MODELS

    monkeypatch.setattr(catalog, "list_models", fake_list_models)
    return catalog


async def test_max_policy_selects_available_default_and_model_specific_max(
    tmp_path: Path, monkeypatch,
) -> None:
    profile = await catalog_with_models(monkeypatch).resolve(
        codex_home=tmp_path,
        provider="openai",
        model=None,
        reasoning_effort=None,
        context_policy="max",
    )

    assert profile.model == "large-model"
    assert profile.reasoning_effort == "low"
    assert profile.model_context_window_tokens == 872_000
    assert profile.model_auto_compact_token_limit == 784_800
    assert profile.effective_context_window_tokens == 828_400


async def test_model_default_policy_uses_that_models_default_window(
    tmp_path: Path, monkeypatch,
) -> None:
    profile = await catalog_with_models(monkeypatch).resolve(
        codex_home=tmp_path,
        provider="openai",
        model="small-model",
        reasoning_effort="medium",
        context_policy="model_default",
    )

    assert profile.model == "small-model"
    assert profile.model_context_window_tokens == 128_000
    assert profile.effective_context_window_tokens == 115_200


async def test_custom_policy_rejects_values_above_model_maximum(
    tmp_path: Path, monkeypatch,
) -> None:
    with pytest.raises(ValueError, match="exceeds small-model maximum"):
        await catalog_with_models(monkeypatch).resolve(
            codex_home=tmp_path,
            provider="openai",
            model="small-model",
            reasoning_effort="medium",
            context_policy="custom",
            custom_context_window_tokens=300_000,
        )


async def test_reasoning_effort_is_checked_against_selected_model(
    tmp_path: Path, monkeypatch,
) -> None:
    with pytest.raises(ValueError, match="not supported"):
        await catalog_with_models(monkeypatch).resolve(
            codex_home=tmp_path,
            provider="openai",
            model="small-model",
            reasoning_effort="high",
            context_policy="max",
        )
