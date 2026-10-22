"""Validation and publish executors (plan 2.10–2.11): deterministic, no model calls."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from assistant_kernel.core import gitdelivery, runtime
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
        manifest_problem = await asyncio.to_thread(check_repo_manifest, settings, project_id, code, task_id)
        if manifest_problem:
            report += f"\n- runtime_manifest: {manifest_problem}"
        vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
        map_problems = await asyncio.to_thread(check_repo_map, code, task_id, vault)
        report += "".join(f"\n- map: {problem}" for problem in map_problems)
        return {"passed": passed and not manifest_problem and not map_problems, "report": report}
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
    layout = await asyncio.to_thread(check_layout, settings, project_id, task_id)
    issues += "".join(f"\n- layout: {problem}" for problem in layout)
    map_problems = await asyncio.to_thread(check_map, settings, project_id, task_id)
    issues += "".join(f"\n- map: {problem}" for problem in map_problems)
    return {"passed": result.get("release_gate") == "pass" and not manifest_problem and not layout and not map_problems,
            "report": (result.get("summary") or "") + (f"\n\n{issues.strip()}" if issues.strip() else "")}


def candidate_targets(settings, project_id: str, task_id: str) -> dict[str, str]:
    """{project-relative path: operation} of the task's candidate files."""
    import json
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    listing = vault / "runs" / task_id / "candidate" / "manifest.json"
    if not listing.is_file():
        return {}
    prefix = "workspace://project/"
    return {f["target_uri"][len(prefix):]: f["operation"]
            for f in json.loads(listing.read_text("utf-8")).get("files") or [] if f["target_uri"].startswith(prefix)}


def check_layout(settings, project_id: str, task_id: str) -> list[str]:
    """The folder rule (ROUND5 §3) for what this release writes and for the project as it is."""
    from assistant_kernel.core import skills
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    written = [path for path, operation in candidate_targets(settings, project_id, task_id).items()
               if operation != "delete"]
    return skills.layout_problems(vault, written)


MAP_FILE = "knowledge/map.yaml"


def check_map(settings, project_id: str, task_id: str) -> list[str]:
    """The project map as it will be after this release (ROUND5 D12): readable, entries point at files that will
    exist, and every Python file of the product has an entry."""
    from assistant_kernel.core import projectmap
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    targets = candidate_targets(settings, project_id, task_id)
    written = {path for path, operation in targets.items() if operation in ("create", "update")}
    deleted = {path for path, operation in targets.items() if operation == "delete"}
    source = (vault / "runs" / task_id / "candidate" / "project" / MAP_FILE) if MAP_FILE in written else vault / MAP_FILE
    try:
        entries = projectmap.load(source)
    except ValueError as exc:
        return [str(exc)]

    def exists(path: str) -> bool:
        path = path.removeprefix(projectmap.PROJECT_PREFIX)   # the map already lives in the project folder
        return path in written or any(w.startswith(path + "/") for w in written) or \
            (path not in deleted and (vault / path).exists())

    required = set(projectmap.covered_files(vault, vault / "product")) - deleted
    required |= {path for path in written if path.startswith("product/") and path.endswith(".py")
                 and not path.endswith("__init__.py")
                 and not set(path.split("/")[1:-1]) & projectmap.SKIP_DIRS}
    return projectmap.problems(entries, exists, sorted(required)) if entries or required else []


def check_repo_map(code: dict[str, Any], task_id: str, vault: Path) -> list[str]:
    """Code delivery: the repository's map at the bound commit (the task worktree); `project:` entries are files of
    the project folder."""
    from assistant_kernel.core import projectmap
    worktree = gitdelivery.worktree_path(code, task_id)
    try:
        entries = projectmap.load(worktree / code["map_path"])
    except ValueError as exc:
        return [str(exc)]
    required = projectmap.covered_files(worktree, worktree)
    exists = projectmap.exists_in(worktree, vault)
    return projectmap.problems(entries, exists, required) if entries or required else []


def check_runtime_manifest(settings, project_id: str, task_id: str) -> str | None:
    """A jobs.yaml about to be published must be valid against the files the project will have."""
    import json

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


def check_repo_manifest(settings, project_id: str, code: dict[str, Any], task_id: str) -> str | None:
    """Code delivery: the repository's jobs.yaml (at its root, released with the fast-forward) must be valid."""

    worktree = gitdelivery.worktree_path(code, task_id)
    path = worktree / runtime.MANIFEST
    if not path.is_file():
        return None
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    try:
        runtime.parse_manifest(path.read_text("utf-8"), lambda p: ((vault if p.startswith("agents/") else worktree) / p)
                               .exists())
    except runtime.ManifestError as exc:
        return str(exc)
    return None


async def execute_publish(run: dict[str, Any], ctx) -> dict[str, Any]:
    """Record that all publisher threads stopped, including failure before a file journal exists."""
    try:
        return await _execute_publish(run, ctx)
    finally:
        if not getattr(ctx, "publication_writer_active", False):
            vault = ProjectRuntimeManager(ctx.services["settings"].data_root).ensure(run["project_id"]).vault_root
            stopped = vault / "runs" / str(run["run_id"]) / "publication/writer-stopped.json"
            stopped.parent.mkdir(parents=True, exist_ok=True)
            stopped.write_text(json.dumps({"project_id": run["project_id"], "publish_run_id": str(run["run_id"]),
                                           "bundle_sha": run["input"]["bundle_sha"]}), "utf-8")


async def _execute_publish(run: dict[str, Any], ctx) -> dict[str, Any]:
    """Publish, between two commits (ROUND5 D13): a snapshot of the project folder first (keeps edits made outside a
    release), then the release commit of the project folder and its writable external folders. External folders had
    their snapshot when the task's build stage began (PM edits them directly), so their release commit holds exactly
    the task's changes. The commits (number and files) go into the result for the change record. Git never blocks
    a release."""
    from assistant_kernel.core import projectgit
    settings, project_id, task_id = ctx.services["settings"], run["project_id"], str(run["task_id"])
    vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
    task, grants = await ctx.kernel.get_task(run["task_id"]), await ctx.kernel.grants(project_id)
    problems, _ = await _joined_worker(ctx, projectgit.commit_release, vault, project_id, grants,
                                      f"发布前快照（任务 {task_id[:8]}）", external=False)
    result = await _publish(run, ctx)
    if result.get("outcome") == "published":
        more, commits = await _joined_worker(ctx, projectgit.commit_release, vault, project_id, grants,
                                            f"发布：{(task or {}).get('goal', '')[:72]}\n\n任务 {task_id}")
        problems += more
        result["commits"] = result.get("commits", []) + commits
    if problems:
        warning = ("⚠ 发布已完成，但 git 提交有问题（不影响发布）：" if result.get("outcome") == "published" else
                   "⚠ 发布未完成，另有 git 快照问题：") + "；".join(problems)[:600]
        result["note"] = "\n".join(part for part in (result.get("note"), warning) if part)
    return result


async def _publish(run: dict[str, Any], ctx) -> dict[str, Any]:
    settings = ctx.services["settings"]
    project_id, task_id, bundle_sha = run["project_id"], str(run["task_id"]), run["input"]["bundle_sha"]
    if bundle_sha.startswith(NO_FILES):
        return {"outcome": "published", "files": []}
    if bundle_sha.startswith(gitdelivery.PREFIX):   # code delivery: main fast-forwards to the approved commit
        code = gitdelivery.config(settings, project_id)
        try:
            files = await _joined_worker(ctx, gitdelivery.fast_forward, code, bundle_sha[len(gitdelivery.PREFIX):])
        except gitdelivery.GitError as exc:
            return {"outcome": "failed", "error": str(exc)}
        return {"outcome": "published", "files": [f"git://{code['main']}/{f}" for f in files],
                "commits": [{"repo": "code", "commit": bundle_sha[len(gitdelivery.PREFIX):], "files": files}],
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
    evidence = manifest_evidence(settings, project_id, bundle)
    result, _ = await _joined_worker(ctx, CapabilityBundleService(settings).publish, project_id=project_id,
                                   publish_run_id=str(run["run_id"]), request=request, cancel_event=ctx.cancel)
    files = [f["target_uri"] for f in result.get("files", [])]
    published = {"outcome": "published", "files": files,
                 "publication_uri": result.get("publication_uri"), "diff_uri": result.get("diff_uri")}
    published.update(evidence)
    return published


async def _joined_worker(ctx, function, *args, **kwargs):
    worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    ctx.publication_writer_active = True
    cancelled = False
    try:
        while True:
            try:
                result = await asyncio.shield(worker)
                break
            except asyncio.CancelledError:
                ctx.cancel.set()
                cancelled = True
                if worker.cancelled():
                    raise   # process shutdown: cannot attest that its thread stopped
            except Exception:
                if not cancelled:
                    raise
                logging.getLogger(__name__).exception("Interrupted publication failed; inspect its journal")
                raise asyncio.CancelledError from None
        if cancelled:
            raise asyncio.CancelledError
        return result
    finally:
        if worker.done() and not worker.cancelled():
            ctx.publication_writer_active = False


async def require_publication_stopped(c, settings, task):
    """Leaving a failed release requires positive evidence that the previous writer cannot resume."""
    from assistant_kernel.core.db import one
    if task["stage"] != "release":
        return
    run = await one(c, "SELECT * FROM core.runs WHERE task_id=%s AND run_type='publish' ORDER BY created_at DESC LIMIT 1",
                    (task["task_id"],))
    if not run or run["input"]["bundle_sha"].startswith(NO_FILES):
        return
    vault = ProjectRuntimeManager(settings.data_root).ensure(task["project_id"]).vault_root
    root = vault / "runs" / str(run["run_id"]) / "publication"
    for filename in ("writer-stopped.json", "journal.json"):
        try:
            record = json.loads((root / filename).read_text("utf-8"))
            if not isinstance(record, dict) or record.get("project_id") != task["project_id"] or record.get("publish_run_id") != str(run["run_id"]):
                continue
            if (record.get("bundle_sha") == run["input"]["bundle_sha"] if filename == "writer-stopped.json"
                    else record.get("status") in ("completed", "rolled_back")):
                return
        except (OSError, ValueError):
            pass
    raise ValueError("旧发布写入尚未确认停止，保留核对卡片；先由维护者停止旧执行器、保存并核对副作用，记录绑定本次发布的停止回执，再处理失败或取消。")


def manifest_evidence(settings, project_id, bundle):
    """Use the approved snapshot, not a possibly changed live file; preserve the existing text hash convention."""
    for item in bundle["files"]:
        if item["target_uri"] == f"workspace://project/{runtime.MANIFEST}":
            if item["operation"] == "delete":
                return {"manifest_sha": None}
            prefix = f"vault://{project_id}/"
            if not item["artifact_uri"].startswith(prefix):
                raise ValueError("运行声明快照不属于当前项目")
            path = ProjectRuntimeManager(settings.data_root).resolve_vault_file(project_id, item["artifact_uri"][len(prefix):])
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["candidate_sha256"]:
                raise ValueError("运行声明快照内容已变化")
            return {"manifest_sha": runtime.manifest_sha(raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n"))}
    return {}


def reconcile_publication(settings, task, run):
    """Confirm existing effects; never re-seal candidates, re-publish, or manufacture a runtime deletion."""
    from assistant_kernel.core import rules
    from assistant_kernel.services.workspace_scope import WorkspaceScopeService
    bundle_sha = rules.validated_bundle(task)
    if not run or not bundle_sha or run["input"].get("bundle_sha") != bundle_sha \
            or not rules.approval_valid(task, "release"):
        raise ValueError("发布核对缺少匹配的批准对象或执行记录")
    saved = run.get("result") or {}
    if bundle_sha.startswith(NO_FILES):
        return {"outcome": "published", "files": []}
    try:
        project_id, task_id = task["project_id"], str(task["task_id"])
        if bundle_sha.startswith(gitdelivery.PREFIX):
            code = gitdelivery.config(settings, project_id)
            sha = bundle_sha[len(gitdelivery.PREFIX):]
            gitdelivery.git("merge-base", "--is-ancestor", sha, code["main"], cwd=code["repo"])
            return {"outcome": "published", "files": saved.get("files", []),
                    "commits": saved.get("commits") or [{"repo": "code", "commit": sha, "files": []}]}
        if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha):
            raise ValueError("无法定位批准的文件快照")
        vault = ProjectRuntimeManager(settings.data_root).ensure(project_id).vault_root
        journal = json.loads((vault / "runs" / str(run["run_id"]) / "publication/journal.json").read_text("utf-8"))
        if not isinstance(journal, dict) or journal.get("status") != "completed" or journal.get("project_id") != project_id \
                or journal.get("publish_run_id") != str(run["run_id"]):
            raise ValueError("发布写入终态未确认，旧执行器仍可能写入或回滚；保留核对，不得确认成功")
        bundle = json.loads((vault / "runtime/candidate-snapshots" / bundle_sha / "bundle.json").read_text("utf-8"))
        if bundle.get("bundle_sha256") != bundle_sha or not bundle.get("files"):
            raise ValueError("发布快照身份不匹配或为空")
        seed = {k: bundle[k] for k in ("schema_version", "project_id", "task_id", "manifest_sha256")}
        seed["files"] = [{k: v for k, v in f.items() if k != "artifact_uri"} for f in bundle["files"]]
        if seed["project_id"] != project_id or seed["task_id"] != task_id or hashlib.sha256(
                json.dumps(seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != bundle_sha:
            raise ValueError("批准快照内容校验失败")
        scopes = WorkspaceScopeService(settings)
        resolved = vault / "runs" / task_id / "resolved"
        plan_path = next((resolved / n for n in ("execution-plan-build.json", "execution-plan.json") if (resolved / n).is_file()),
                         resolved / "execution-plan-build.json")
        plan = json.loads(plan_path.read_text("utf-8"))["bindings"]
        pinned = scopes.load_snapshot(project_id, task_id, None, plan["workspace_scope_snapshot_uri"], plan["workspace_scope_snapshot_sha256"])
        current = scopes.resolve(project_id, task_id, None)
        for item in bundle["files"]:
            approved, target, _ = scopes.resolve_uri(pinned, item["target_uri"])
            actual, current_target, _ = scopes.resolve_uri(current, item["target_uri"])
            if target != current_target or (approved.grant_id, approved.grant_sha256) != (actual.grant_id, actual.grant_sha256):
                raise ValueError("发布目标授权已变化")
            if item["operation"] == "delete":
                if target.exists(): raise ValueError("批准删除的目标仍存在")
            elif not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != item["candidate_sha256"]:
                raise ValueError("实际文件尚未与批准内容一致")
        return {"outcome": "published", "files": [f["target_uri"] for f in bundle["files"]],
                "commits": saved.get("commits", []), **manifest_evidence(settings, project_id, bundle)}
    except (OSError, ValueError, KeyError, gitdelivery.GitError) as exc:
        raise ValueError(f"发布结果尚未核实，请先确认写入已停止、核对回执和实际目标；不要盲目重发：{exc}") from exc
