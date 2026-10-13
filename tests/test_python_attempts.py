"""Python correctness regressions: real processes; database fixtures are isolated."""
import asyncio
import copy
import json
import os
import sys
from types import SimpleNamespace
import uuid

import pytest

from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.reconciliation import ReconciliationService
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.worker import main as worker
from assistant_kernel.worker.python_process import (
    cleanup_python_group, inspect_python_group, python_process_identity,
)
from test_core4 import core_settings, prepare_agent


class EvidenceRepository:
    def __init__(self):
        self.evidence = []
        self.results = []
        self.owned = True

    async def record_python_attempt(self, run_id, worker_id, token, receipt, refs):
        self.evidence.append((copy.deepcopy(receipt), list(refs)))
        return self.owned

    async def finish_run(self, *args, **kwargs):
        self.results.append(kwargs)
        return self.owned


def setup_script(tmp_path, body):
    settings = SimpleNamespace(data_root=tmp_path, python_executable=sys.executable)
    root = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    (root / "probe.py").write_text(body)
    run = {"run_id": str(uuid.uuid4()), "project_id": "demo", "attempt": 1,
           "claim_token": str(uuid.uuid4()),
           "request_json": {"source_uri": "vault://demo/probe.py"}}
    return settings, root, run


async def test_failed_attempt_result_cannot_be_reused(tmp_path):
    settings, root, run = setup_script(tmp_path, '''import json,os,sys
if os.environ["ASSISTANT_ATTEMPT"] == "1":
    json.dump({"old": True}, open(os.environ["ASSISTANT_RESULT_PATH"], "w"))
    sys.exit(1)
print("second attempt has no JSON result")
''')
    repo = EvidenceRepository()
    with pytest.raises(RuntimeError, match="exited with code 1"):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.evidence[-1][0]["outcome"] == "failed"
    assert any("/attempts/1/result.json" in ref for ref in repo.evidence[-1][1])
    run["attempt"] = 2
    await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.results[-1]["result"] == {}
    assert all("/attempts/2/" in ref for ref in repo.results[-1]["artifact_refs"])
    assert not any(ref.endswith("/result.json") for ref in repo.results[-1]["artifact_refs"])
    old = root / "runs" / run["run_id"] / "attempts/1/result.json"
    assert json.loads(old.read_text()) == {"old": True}
    receipt = root / "runs" / run["run_id"] / "attempts/2/execution.json"
    original = receipt.read_bytes()
    with pytest.raises(FileExistsError):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert receipt.read_bytes() == original


async def test_invalid_result_retains_this_attempt_evidence(tmp_path):
    settings, _, run = setup_script(tmp_path, '''import os
open(os.environ["ASSISTANT_RESULT_PATH"], "w").write("invalid json")
''')
    repo = EvidenceRepository()
    with pytest.raises(json.JSONDecodeError):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert not repo.results
    assert repo.evidence[-1][0]["cleanup"]["status"] == "verified"
    assert repo.evidence[-1][0]["outcome"] == "failed"


async def test_spawn_pool_joins_and_returns_current_result(tmp_path):
    settings, _, run = setup_script(tmp_path, '''import json,os,multiprocessing
from concurrent.futures import ProcessPoolExecutor
def square(x): return x*x
if __name__ == "__main__":
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        result = list(pool.map(square, [1,2,3]))
    json.dump({"values": result}, open(os.environ["ASSISTANT_RESULT_PATH"], "w"))
''')
    repo = EvidenceRepository()
    await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.results[-1]["result"] == {"values": [1, 4, 9]}
    assert repo.evidence[-1][0]["cleanup"]["status"] == "verified"


async def test_unknown_cleanup_never_finishes_success(tmp_path, monkeypatch):
    settings, _, run = setup_script(tmp_path, 'print("done")')
    repo = EvidenceRepository()
    async def unknown(*args): return {"status": "unknown", "reason": "test probe unavailable"}
    monkeypatch.setattr(worker, "cleanup_python_group", unknown)
    with pytest.raises(RuntimeError, match="cleanup is unknown"):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.evidence[-1][0]["outcome"] == "reconciling"
    assert not repo.results


async def test_missing_executable_keeps_failure_receipt(tmp_path):
    settings, _, run = setup_script(tmp_path, 'print("unused")')
    settings.python_executable = str(tmp_path / "missing-python")
    repo = EvidenceRepository()
    with pytest.raises(FileNotFoundError):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.evidence[-1][0]["cleanup"]["status"] == "verified"
    assert repo.evidence[-1][0]["outcome"] == "failed"
    assert not repo.results


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_stop_keeps_logs_and_verified_cleanup(tmp_path, stop):
    settings, _, run = setup_script(tmp_path, 'import time; print("partial",flush=True); time.sleep(15)')
    repo = EvidenceRepository()
    cancel = asyncio.Event()
    run["request_json"]["timeout_seconds"] = 1
    task = asyncio.create_task(worker.execute_python(run, settings, repo, "w", cancel))
    if stop == "cancel":
        await asyncio.sleep(0.15)
        cancel.set()
        await task
        assert repo.results[-1]["status"] == "cancelled"
    else:
        with pytest.raises(TimeoutError):
            await task
    receipt, refs = repo.evidence[-1]
    assert receipt["cleanup"]["status"] == "verified"
    assert any(ref.endswith("stderr.log") for ref in refs)
    assert any(ref.endswith("execution.json") for ref in refs)
    assert inspect_python_group(receipt["process_identity"])["status"] == "verified"


async def test_lost_claim_does_not_publish_success(tmp_path):
    settings, _, run = setup_script(tmp_path, 'print("must not start")')
    repo = EvidenceRepository()
    repo.owned = False
    with pytest.raises(asyncio.CancelledError):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.evidence[-1][0]["process_identity"] is None
    assert not repo.results


async def test_lost_claim_after_launch_still_cleans_process(tmp_path):
    settings, _, run = setup_script(tmp_path, 'import time; time.sleep(15)')
    class LostAfterLaunch(EvidenceRepository):
        async def record_python_attempt(self, *args):
            await super().record_python_attempt(*args)
            return len(self.evidence) == 1
    repo = LostAfterLaunch()
    with pytest.raises(asyncio.CancelledError):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.evidence[-1][0]["process_identity"] is not None
    assert repo.evidence[-1][0]["cleanup"]["status"] == "verified"
    assert not repo.results


async def test_executor_rejects_parent_success_with_leftover_child(tmp_path, monkeypatch):
    settings, _, run = setup_script(tmp_path, '''import subprocess,sys,time
subprocess.Popen([sys.executable,"-c","import time; time.sleep(15)"])
time.sleep(.05)
''')
    repo = EvidenceRepository()
    async def quick_cleanup(identity):
        return await cleanup_python_group(identity, grace_seconds=.05, kill_seconds=1)
    monkeypatch.setattr(worker, "cleanup_python_group", quick_cleanup)
    with pytest.raises(RuntimeError, match="parent exited without verified child cleanup"):
        await worker.execute_python(run, settings, repo, "w", asyncio.Event())
    assert repo.evidence[-1][0]["outcome"] == "failed"
    assert repo.evidence[-1][0]["cleanup"]["status"] == "verified"
    assert not repo.results


async def launch_family(tmp_path, parent_exits=False):
    ready = tmp_path / "ready.json"
    child = ('import signal,time,json,os,sys; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
             'json.dump({"pid":os.getpid()},open(sys.argv[1],"w")); time.sleep(15)')
    parent = ('import subprocess,sys,time,pathlib; '
              'subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]); '
              'time.sleep(0.5)' if parent_exits else
              'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",sys.argv[1],sys.argv[2]]); time.sleep(15)')
    run_id = str(uuid.uuid4())
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", parent, child, str(ready), start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "ASSISTANT_RUN_ID": run_id, "ASSISTANT_ATTEMPT": "1"},
    )
    identity = python_process_identity(process.pid, run_id, 1)
    for _ in range(100):
        if ready.exists() and ready.stat().st_size:
            break
        await asyncio.sleep(0.02)
    if parent_exits:
        await asyncio.wait_for(process.wait(), 3)
    return process, identity


@pytest.mark.parametrize("parent_exits", [False, True])
async def test_parent_exit_or_term_does_not_hide_surviving_child(tmp_path, parent_exits):
    process, identity = await launch_family(tmp_path, parent_exits)
    try:
        assert inspect_python_group(identity)["status"] == "active"
        cleanup = await cleanup_python_group(identity, grace_seconds=.05, kill_seconds=1)
        assert cleanup["status"] == "verified"
        assert inspect_python_group(identity)["status"] == "verified"
    finally:
        await cleanup_python_group(identity, grace_seconds=.01, kill_seconds=1)
        await asyncio.wait_for(process.wait(), 3)


async def test_wrong_identity_never_signals(tmp_path, monkeypatch):
    process, identity = await launch_family(tmp_path)
    try:
        wrong = {**identity, "start_ticks": identity["start_ticks"] + 1}
        with monkeypatch.context() as patch:
            def forbidden(*args):
                pytest.fail("must not signal an unverified process group")
            patch.setattr("assistant_kernel.worker.python_process.os.killpg", forbidden)
            assert (await cleanup_python_group(wrong))["status"] == "unknown"
        assert inspect_python_group(identity)["status"] == "active"
    finally:
        await cleanup_python_group(identity, grace_seconds=.01, kill_seconds=1)
        await asyncio.wait_for(process.wait(), 3)


async def test_database_attempt_evidence_is_fenced(core_settings):
    prepare_agent(core_settings)
    repo = CoreRepository(core_settings)
    run = await repo.create_run(project_id="demo", agent_key="project:demo:python", run_type="python", request={})
    owner = str(uuid.uuid4())
    claimed = await repo.claim_run("test", owner, 60, queue_kind="runtime")
    receipt = {"attempt": claimed["attempt"], "process_identity": None,
               "cleanup": {"status": "unknown", "reason": "launch pending"}}
    refs = [f"vault://demo/runs/{run['run_id']}/attempts/1/execution.json"]
    assert not await repo.record_python_attempt(str(run["run_id"]), owner, str(uuid.uuid4()), receipt, refs)
    assert await repo.record_python_attempt(str(run["run_id"]), owner, str(claimed["claim_token"]), receipt, refs)
    state = await repo.retry_run(str(run["run_id"]), owner, str(claimed["claim_token"]), {"type": "LaunchUncertain"})
    assert state == "reconciling"  # Even without a captured PID, do not replay uncertain launch.
    assert not await repo.record_python_cleanup(str(run["run_id"]), 2, {"status": "verified"})
    assert await repo.record_python_cleanup(str(run["run_id"]), 1, {"status": "verified"})
    assert not await repo.record_python_attempt(str(run["run_id"]), owner, str(claimed["claim_token"]), receipt, refs)
    current = await repo.get_run(str(run["run_id"]))
    assert current["artifact_refs"] == refs
    assert current["executor_json"]["python_attempt"]["cleanup"]["status"] == "verified"


async def test_reconciliation_rechecks_group_before_retry(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    process, identity = await launch_family(tmp_path, parent_exits=True)
    run = {"run_id": "r", "run_type": "python", "project_id": "demo", "attempt": 1,
           "executor_json": {"python_attempt": {"process_identity": identity, "cleanup": {"status": "unknown"}}}}
    class Repo:
        resolved = False
        async def get_message(self, *args): return {"run_id": "r"}
        async def get_run(self, *args): return run
        async def record_python_cleanup(self, run_id, attempt, cleanup):
            run["executor_json"]["python_attempt"]["cleanup"] = cleanup
            return True
        async def resolve_reconciliation_card(self, **kwargs):
            self.resolved = True
            return {"status": "queued"}
    repo = Repo()
    service = ReconciliationService(settings, repo)
    try:
        with pytest.raises(PermissionError, match="cleanup is not verified"):
            await service.resolve_card(card_message_id="c", external_event_id="e", actor_user_id="u", action="retry")
        assert not repo.resolved
        await cleanup_python_group(identity, grace_seconds=.01, kill_seconds=1)
        assert (await service.resolve_card(card_message_id="c", external_event_id="e2", actor_user_id="u", action="retry"))["status"] == "queued"
    finally:
        await cleanup_python_group(identity, grace_seconds=.01, kill_seconds=1)
        await asyncio.wait_for(process.wait(), 3)


async def test_database_rejects_bypassing_cleanup_guard(core_settings):
    prepare_agent(core_settings)
    repo = CoreRepository(core_settings)
    incoming = await repo.ingest_message(project_id="demo", agent_key="project:demo:manager",
        channel="discord", chat_id="test", user_id="u", external_event_id=uuid.uuid4().hex,
        external_message_id=None, content={"text": "test"})
    run = await repo.create_run(project_id="demo", agent_key="project:demo:python", run_type="python",
        trigger_message_id=str(incoming["message"]["message_id"]), request={})
    owner = str(uuid.uuid4())
    claimed = await repo.claim_run("test", owner, 60, queue_kind="runtime")
    await repo.record_python_attempt(str(run["run_id"]), owner, str(claimed["claim_token"]),
        {"attempt": 1, "cleanup": {"status": "unknown"}}, [])
    await repo.retry_run(str(run["run_id"]), owner, str(claimed["claim_token"]), {"type": "ProbeUnknown"})
    card = await repo.queue_reconciliation_card(run_id=str(run["run_id"]),
        diagnostic={"actions": [{"id": "retry"}, {"id": "cancel"}, {"id": "fail"}, {"id": "wait"}]})
    for action in ("retry", "cancel", "fail"):
        with pytest.raises(PermissionError, match="cleanup must be verified"):
            await repo.resolve_reconciliation_card(card_message_id=str(card["message_id"]),
                external_event_id=uuid.uuid4().hex, actor_user_id="u", action=action)
    await repo.record_python_cleanup(str(run["run_id"]), 1, {"status": "verified"})
    accepted = await repo.resolve_reconciliation_card(card_message_id=str(card["message_id"]),
        external_event_id=uuid.uuid4().hex, actor_user_id="u", action="retry")
    assert accepted["status"] == "queued"
    next_claim = await repo.claim_run("test", str(uuid.uuid4()), 60, queue_kind="runtime")
    assert next_claim["attempt"] == 2
    assert not await repo.record_python_cleanup(str(run["run_id"]), 1, {"status": "unknown"})


async def test_cross_project_independent_runs_and_shared_write_protection(core_settings):
    prepare_agent(core_settings, "one")
    prepare_agent(core_settings, "two")
    repo = CoreRepository(core_settings)
    for project in ("one", "two"):
        root = ProjectRuntimeManager(core_settings.data_root).ensure(project).vault_root
        (root / "probe.py").write_text('import os,time,json\na=time.time(); time.sleep(.3)\njson.dump({"start":a,"end":time.time()},open(os.environ["ASSISTANT_RESULT_PATH"],"w"))')
        await repo.create_run(project_id=project, agent_key=f"project:{project}:python", run_type="python",
                              request={"source_uri": f"vault://{project}/probe.py"})
    owners = [str(uuid.uuid4()), str(uuid.uuid4())]
    claims = await asyncio.gather(*(repo.claim_run("test", owner, 60, queue_kind="runtime") for owner in owners))
    await asyncio.gather(*(worker.execute_python(run, core_settings, repo, owner, asyncio.Event())
                          for run, owner in zip(claims, owners)))
    results = [(await repo.get_run(str(run["run_id"])))["result_json"] for run in claims]
    assert max(result["start"] for result in results) < min(result["end"] for result in results)
    for project in ("one", "two"):
        await repo.create_run(project_id=project, agent_key=f"project:{project}:python", run_type="python",
                              request={"source_uri": f"vault://{project}/probe.py", "shared_write": True})
    assert await repo.claim_run("test", str(uuid.uuid4()), 60, queue_kind="runtime")
    assert await repo.claim_run("test", str(uuid.uuid4()), 60, queue_kind="runtime") is None
