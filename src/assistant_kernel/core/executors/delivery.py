"""Validation and publish executors (plan 2.10–2.11): deterministic, no model calls."""
from __future__ import annotations

import asyncio
from typing import Any

import yaml

from assistant_kernel.services.candidate_bundle import CandidateBundleService
from assistant_kernel.storage import ProjectRuntimeManager

NO_FILES = "no-files:"


def _reseal(settings, project_id: str, task_id: str, expected: str) -> tuple[dict[str, Any] | None, str | None]:
    """Seal the task's candidate files again; report a mismatch instead of using changed files."""
    try:
        bundle = CandidateBundleService(settings).seal(project_id, task_id)
    except (FileNotFoundError, ValueError) as exc:
        return None, f"候选文件无法封存：{exc}"
    if bundle["bundle_sha256"] != expected:
        return None, "候选文件在提交实施报告之后又被修改过，需要重新提交实施报告。"
    return bundle, None


def _validation_policy(settings, project_id: str) -> dict[str, Any]:
    path = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root / "project.yaml"
    data = yaml.safe_load(path.read_text("utf-8")) if path.is_file() else {}
    return dict((data or {}).get("validation_policy") or {"required_scopes": ["structure"]})


async def execute_validation(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings = ctx.services["settings"]
    project_id, task_id, bundle_sha = run["project_id"], str(run["task_id"]), run["input"]["bundle_sha"]
    if bundle_sha.startswith(NO_FILES):
        return {"passed": True, "report": "本次交付没有候选文件（仅文档交付），无需文件验证。"}
    bundle, problem = await asyncio.to_thread(_reseal, settings, project_id, task_id, bundle_sha)
    if problem:
        return {"passed": False, "report": problem}
    from assistant_kernel.services.validation_runner import ValidationRunner
    result = await asyncio.to_thread(
        ValidationRunner(settings).validate, project_id, task_id, _validation_policy(settings, project_id),
        {"bundle_uri": bundle["bundle_uri"], "bundle_file_sha256": bundle["bundle_file_sha256"]})
    issues = "\n".join(f"- {i.get('code')}: {i.get('message')}" for i in result.get("issues") or [])
    return {"passed": result.get("release_gate") == "pass",
            "report": (result.get("summary") or "") + (f"\n\n{issues}" if issues else "")}


async def execute_publish(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings = ctx.services["settings"]
    project_id, task_id, bundle_sha = run["project_id"], str(run["task_id"]), run["input"]["bundle_sha"]
    if bundle_sha.startswith(NO_FILES):
        return {"outcome": "published", "files": []}
    bundle, problem = await asyncio.to_thread(_reseal, settings, project_id, task_id, bundle_sha)
    if problem:
        return {"outcome": "failed", "error": problem}
    from assistant_kernel.services.capability_bundle import CapabilityBundleService
    request = {"candidate_manifest_uri": f"vault://{project_id}/runs/{task_id}/candidate/manifest.json",
               "candidate_bundle_uri": bundle["bundle_uri"],
               "candidate_bundle_file_sha256": bundle["bundle_file_sha256"],
               "implementation_run_id": task_id, "task_protocol": "single-agent.v2"}
    result, _ = await asyncio.to_thread(
        CapabilityBundleService(settings).publish, project_id=project_id,
        publish_run_id=str(run["run_id"]), request=request, cancel_event=None)
    return {"outcome": "published", "files": [f["target_uri"] for f in result.get("files", [])],
            "publication_uri": result.get("publication_uri"), "diff_uri": result.get("diff_uri")}
