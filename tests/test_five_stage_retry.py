import uuid
from datetime import datetime, timezone
import pytest

from test_core4 import core_settings
from test_five_stage_flow import setup_flow


async def test_failed_pm_is_atomically_paused_and_restart_fills_settlement_gap(core_settings):
    repository, _, task_id, _ = await setup_flow(core_settings)
    worker = str(uuid.uuid4())
    run = await repository.claim_run("retry", worker, 60)
    status = await repository.retry_run(task_id, worker, str(run["claim_token"]), {"type": "Timeout", "message": "unverified result"})
    assert status == "waiting_supervisor"
    assert await repository.claim_run("no-replay", str(uuid.uuid4()), 60) is None
    await repository.reconcile_pending_supervision()
    task = await repository.get_run(task_id)
    assert task["request_json"]["task_turn_count"] == 1
    assert task["request_json"]["failed_turn_ref"]
    await repository.reconcile_pending_supervision()
    assert (await repository.get_run(task_id))["request_json"]["task_turn_count"] == 1
    with pytest.raises(PermissionError, match="successful supervision"):
        await repository.resume_supervision_degraded(task_id=task_id, turn_number=1, reason="unreachable")


async def test_supervisor_uses_three_requested_backoffs_and_stops(core_settings):
    repository, _, task_id, _ = await setup_flow(core_settings)
    worker = str(uuid.uuid4())
    task = await repository.claim_run("retry", worker, 60)
    await repository.retry_run(task_id, worker, str(task["claim_token"]), {"type": "Timeout"})
    await repository.reconcile_pending_supervision()
    for index, seconds in enumerate([30, 120, 600]):
        worker = str(uuid.uuid4())
        sup = await repository.claim_run("governor", worker, 60)
        assert str(sup["agent_key"]).endswith(":supervisor")
        before = datetime.now(timezone.utc)
        assert await repository.retry_run(str(sup["run_id"]), worker, str(sup["claim_token"]), {"type": "TemporaryFailure"}) == "queued"
        queued = await repository.get_run(str(sup["run_id"]))
        delay = (queued["available_at"] - before).total_seconds()
        assert seconds-1 <= delay <= seconds+2
        async with repository.database.connection() as connection:
            await connection.execute("UPDATE assistant.runs SET available_at=now() WHERE run_id=%s", (sup["run_id"],))
            await connection.commit()
    worker = str(uuid.uuid4())
    sup = await repository.claim_run("governor", worker, 60)
    assert await repository.retry_run(str(sup["run_id"]), worker, str(sup["claim_token"]), {"type": "TemporaryFailure"}) == "dead_letter"
