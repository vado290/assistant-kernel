"""Validation and publish executors (plan 2.10–2.11): deterministic, no model calls."""
from __future__ import annotations

import asyncio
from typing import Any

import yaml

from assistant_kernel.core import gitdelivery
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
    if bundle_sha.startswith(gitdelivery.PREFIX):   # code delivery: the project's tests on exactly that commit
        code = gitdelivery.config(settings, project_id)
        passed, report = await asyncio.to_thread(gitdelivery.validate, code, task_id,
                                                 bundle_sha[len(gitdelivery.PREFIX):])
        return {"passed": passed, "report": report}
    bundle, problem = await asyncio.to_thread(_reseal, settings, project_id, task_id, bundle_sha)
    if problem:
        return {"passed": False, "report": problem}
    from assistant_kernel.services.validation_runner import ValidationRunner
    result = await asyncio.to_thread(
        ValidationRunner(settings).validate, project_id, task_id, _validation_policy(settings, project_id),
        {"bundle_uri": bundle["bundle_uri"], "bundle_file_sha256": bundle["bundle_file_sha256"]})
    issues = "\n".join(f"- {i.get('code')}: {i.get('message')}" for i in result.get("issues") or [])
    manifest_problem = await asyncio.to_thread(check_runtime_manifest, settings, project_id, task_id)
    if manifest_problem:
        issues += f"\n- runtime_manifest: {manifest_problem}"
    return {"passed": result.get("release_gate") == "pass" and not manifest_problem,
            "report": (result.get("summary") or "") + (f"\n\n{issues.strip()}" if issues.strip() else "")}


def check_runtime_manifest(settings, project_id: str, task_id: str) -> str | None:
    """A jobs.yaml about to be published must be valid against the files the project will have."""
    import json
    from assistant_kernel.core import runtime
    from assistant_kernel.storage import ProjectRuntimeManager
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    candidate = vault / "runs" / task_id / "candidate"
    listing = candidate / "manifest.json"
    if not listing.is_file():
        return None
    prefix = "workspace://project/"
    files = {f["target_uri"][len(prefix):]: f["operation"]
             for f in json.loads(listing.read_text("utf-8")).get("files") or [] if f["target_uri"].startswith(prefix)}
    if files.get(runtime.MANIFEST) not in ("create", "update"):
        return None

    def exists(path: str) -> bool:
        operation = files.get(path)
        return operation in ("create", "update") or (operation is None and (vault / path).exists())

    try:
        runtime.parse_manifest((candidate / "project" / runtime.MANIFEST).read_text("utf-8"), exists)
    except runtime.ManifestError as exc:
        return str(exc)
    return None


async def execute_publish(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings = ctx.services["settings"]
    project_id, task_id, bundle_sha = run["project_id"], str(run["task_id"]), run["input"]["bundle_sha"]
    if bundle_sha.startswith(NO_FILES):
        return {"outcome": "published", "files": []}
    if bundle_sha.startswith(gitdelivery.PREFIX):   # code delivery: main fast-forwards to the approved commit
        code = gitdelivery.config(settings, project_id)
        try:
            files = await asyncio.to_thread(gitdelivery.fast_forward, code, bundle_sha[len(gitdelivery.PREFIX):])
        except gitdelivery.GitError as exc:
            return {"outcome": "failed", "error": str(exc)}
        return {"outcome": "published", "files": [f"git://{code['main']}/{f}" for f in files],
                "note": code["release_note"].format(commit=bundle_sha[len(gitdelivery.PREFIX):][:12])
                if code["release_note"] else ""}
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
