"""Optimization uses the existing stage jobs, with an explicit baseline and candidate copy."""
from __future__ import annotations

import hashlib
import json
import uuid

from psycopg.types.json import Jsonb

from assistant_kernel.repositories import ControlPlaneRepository, canonical_sha256, json_compatible
from assistant_kernel.repositories.feedback import FeedbackRepository
from assistant_kernel.repositories.stages import StageRepository
from assistant_kernel.storage import ProjectRuntimeManager


async def start_optimization(settings, project_id, request, idempotency_key):
    repo = ControlPlaneRepository(settings)
    runtime = ProjectRuntimeManager(settings.data_root)
    root = runtime.describe(project_id).vault_root
    async with repo.database.connection() as c:
        if not await (await c.execute("SELECT 1 FROM assistant.projects WHERE project_id=%s", (project_id,))).fetchone():
            raise ValueError("project not found")
        # Retry the original prepared request even if files have changed since its creation.
        previous = await (await c.execute("SELECT response_json FROM assistant.idempotency_records WHERE scope='project_optimization' AND idempotency_key=%s", (idempotency_key,))).fetchone()
        if previous:
            task = await repo.database.get_task(previous["response_json"]["task_id"])
            if task["project_id"] != project_id or task["request_json"].get("original_request") != request:
                raise ValueError("idempotency key belongs to another optimization")
            return previous["response_json"]
    feedbacks = []
    for fid in request.get("feedback_ids", []):
        item = await FeedbackRepository(settings).get(project_id, fid)
        if item is None:
            raise ValueError("feedback does not belong to this project")
        feedbacks.append(json_compatible(item))
    paths = request.get("target_paths", [])
    if not paths or len(paths) > 50 or len(set(paths)) != len(paths):
        raise ValueError("select 1-50 distinct target files for this optimization")
    baseline = {}; total = 0
    for relative in paths:
        if relative.split('/')[0] in {"runtime", "runs", "optimizer"}:
            raise ValueError("optimization cannot target runtime or its own control role")
        p = runtime.resolve_below(root, relative)
        if not p.is_file():
            raise ValueError("optimization target must be an existing file")
        data = p.read_bytes(); total += len(data)
        if total > 262144:
            raise ValueError("selected optimization source exceeds 256 KiB")
        canonical_relative = p.relative_to(root.resolve()).as_posix()
        if canonical_relative != relative:
            raise ValueError("use canonical project-relative paths without aliases")
        baseline[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "text": data.decode('utf-8')}
    change_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(["optimization", project_id, idempotency_key])))
    prepared = {**request, "original_request": request, "feedback_snapshot": feedbacks,
                "baseline": baseline, "change_request_id": change_id}
    result = await StageRepository(settings).start(project_id=project_id, request_data=prepared,
                                                   idempotency_key=idempotency_key, flow="optimization")
    return result


def prepare_candidate(settings, state, stage):
    runtime = ProjectRuntimeManager(settings.data_root)
    root = runtime.describe(state["project_id"]).vault_root
    baseline = state["request"]["baseline"]
    for rel, info in baseline.items():
        if hashlib.sha256(runtime.resolve_below(root, rel).read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("optimization baseline changed; create or rebase the proposal before continuing")
    if stage == "requirements":
        return root
    candidate = runtime.resolve_below(root, f"runs/{state['task_id']}/candidate")
    marker = runtime.resolve_below(root, f"runs/{state['task_id']}/candidate-baseline.json")
    baseline_hash = canonical_sha256(baseline)
    if marker.exists():
        if json.loads(marker.read_text())["sha256"] != baseline_hash:
            raise ValueError("candidate baseline mismatch")
        return candidate
    # A partial setup can be resumed before any candidate job has run.
    for rel, info in baseline.items():
        destination = runtime.resolve_below(candidate, rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != info["sha256"]:
            raise ValueError("untracked candidate changes during initialization")
        destination.write_text(info["text"], encoding="utf-8")
    marker.write_text(json.dumps({"sha256": baseline_hash}), encoding="utf-8")
    return candidate
