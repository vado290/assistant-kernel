from __future__ import annotations

import hashlib
import ast
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.file_effects import FileEffectRecorder
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.project_changes import ProjectChangeService
from assistant_kernel.services.semantic_map import SemanticMapService
from assistant_kernel.services.workspace_scope import AuthorizedRoot
from assistant_kernel.storage import ProjectRuntimeManager


def setup(tmp_path):
    settings = KernelSettings.from_environment().model_copy(update={"data_root": tmp_path})
    maps = SemanticMapService(settings)
    maps.bootstrap("demo", purpose="demo")
    vault = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    root = AuthorizedRoot("project", vault, "write", "demo")
    return settings, maps, vault, [root]


class FakeCursor:
    def __init__(self, row): self.row = row
    async def fetchone(self): return self.row


class FakeDatabase:
    def __init__(self): self.events = {}; self.fail = False
    @asynccontextmanager
    async def connection(self): yield self
    @asynccontextmanager
    async def transaction(self): yield self
    async def execute(self, query, values):
        if self.fail: raise RuntimeError("database unavailable")
        if "SELECT value FROM assistant.kernel_meta" in query:
            return FakeCursor(None)  # This fixture covers the retained legacy reader/writer.
        if "INSERT INTO" in query:
            payload = values[4].obj
            prior = self.events.setdefault(payload["change_id"], payload)
            return FakeCursor({"content_json": prior})
        project_id, change_id = values
        record = self.events.get(change_id)
        return FakeCursor({"content_json": record} if record and record["project_id"] == project_id else None)


def test_generation_reads_ignore_unpublished_index_and_verify_hash(tmp_path):
    _, maps, vault, _ = setup(tmp_path)
    index = vault / "knowledge/map/index.yaml"
    payload = yaml.safe_load(index.read_text())
    payload["components"] = [{"component_id": "demo.component", "purpose": "new unaccepted content"}]
    index.write_text(yaml.safe_dump(payload))
    assert maps.load("demo")["components"] == []
    maps.publish_current("demo")
    assert maps.load("demo")["components"][0]["component_id"] == "demo.component"
    ref = maps.current_ref("demo")
    target = vault / ref["uri"].split("vault://demo/", 1)[1]
    target.write_text(target.read_text() + "\n# tampered\n")
    with pytest.raises(ValueError, match="hash differs"):
        maps.load("demo")


@pytest.mark.asyncio
async def test_known_batch_is_idempotent_and_updates_files(tmp_path):
    settings, maps, vault, roots = setup(tmp_path)
    database = FakeDatabase()
    service = ProjectChangeService(settings, database)
    target = vault / "main.py"
    target.write_text("print(1)\n", encoding="utf-8")
    args = dict(batch_id="turn-1", roots=roots, changes=[{
        "target_uri": "workspace://project/main.py", "operation": "create",
        "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "lines_added": 1, "lines_deleted": 0, "purpose": "demo entrypoint"}],
        reason="approved design", reason_source_refs=[{"id": "decision-1"}])
    first = await service.capture_batch("demo", **args)
    second = await service.capture_batch("demo", **args)
    assert first["changes"] == second["changes"]
    assert len(database.events) == 1
    file = maps.load("demo")["files"][0]
    assert file["purpose"] == "demo entrypoint"
    assert file["lines_added"] == 1
    assert file["last_change_id"] == first["changes"][0]["change_id"]
    assert first["changes"][0]["context_visibility"] == "structured"
    target.unlink()
    deleted = await service.refresh_map("demo", roots=roots)
    assert deleted["changes"][0]["operation"] == "delete"
    assert deleted["changes"][0]["occurred_at"] is None
    assert maps.load("demo")["files"] == []


@pytest.mark.asyncio
async def test_candidate_does_not_mutate_formal_map_and_ledger_failure_is_pending(tmp_path):
    settings, maps, vault, roots = setup(tmp_path)
    database = FakeDatabase()
    service = ProjectChangeService(settings, database)
    before = maps.current_ref("demo")
    result = await service.record_candidate_manifest("demo", "task", "run", {
        "files": [{"target_uri": "workspace://project/main.py", "operation": "create",
                   "candidate_uri": "workspace://candidate/project/main.py", "candidate_sha256": "f"*64}]},
        roots=roots)
    assert result["scope_kind"] == "candidate"
    assert maps.current_ref("demo") == before
    database.fail = True
    (vault / "main.py").write_text("print(1)\n")
    pending = await service.refresh_map("demo", roots=roots, source_uris=["workspace://project/main.py"])
    assert not pending["ledger_committed"]
    assert pending["map_refresh_pending"]
    assert maps.current_ref("demo")["generation_id"] == before["generation_id"]
    assert maps.current_ref("demo")["freshness"] == "stale"
    # A later external edit must not inherit the earlier batch's reason/diff.
    (vault / "main.py").write_text("print(2)\n")
    database.fail = False
    replay = await service.reconcile_pending("demo", roots=roots)
    assert replay["recovered_batches"]
    assert not list((vault / "runtime/change-pending").glob("*.json"))
    assert maps.current_ref("demo")["freshness"] == "current"
    observed = next(record for record in database.events.values() if record["scope_kind"] == "external")
    assert observed["occurred_at"] is None
    recovered_file = maps.load("demo")["files"][0]
    assert recovered_file["sha256"] == hashlib.sha256((vault / "main.py").read_bytes()).hexdigest()
    assert recovered_file["cause_status"] == "unknown"
    assert len([record for record in database.events.values() if record["scope_kind"] == "external"]) == 2


def test_file_effects_store_text_diff_and_binary_has_null_line_counts(tmp_path):
    vault = tmp_path / "demo"; vault.mkdir()
    source = tmp_path / "source"; source.mkdir()
    (source / "text.py").write_text("a\nb\n", encoding="utf-8")
    (source / "data.bin").write_bytes(b"\x00a")
    recorder = FileEffectRecorder(vault, "run", "invocation", [
        AuthorizedRoot("source", source, "write", "source")])
    recorder.capture_before()
    (source / "text.py").write_text("a\nc\nd\n", encoding="utf-8")
    (source / "data.bin").write_bytes(b"\x00b")
    result = recorder.capture_after()
    changes = {item["path"]: item for item in result["changes"]}
    text = changes["source/text.py"]
    assert text["lines_added"] == 2 and text["lines_deleted"] == 1
    assert text["diff_ref"]["sha256"]
    assert changes["source/data.bin"]["lines_added"] is None


@pytest.mark.asyncio
async def test_diff_reader_is_project_scoped_and_hash_checked(tmp_path):
    settings, _, vault, roots = setup(tmp_path)
    database = FakeDatabase()
    service = ProjectChangeService(settings, database)
    diff = vault / "changes.patch"; diff.write_text("+hello\n", encoding="utf-8")
    batch = await service.capture_batch("demo", batch_id="diff", roots=roots,
        changes=[{"target_uri": "workspace://project/main.py", "diff_ref": {
            "uri": "vault://demo/changes.patch", "sha256": hashlib.sha256(diff.read_bytes()).hexdigest()}}])
    change_id = batch["changes"][0]["change_id"]
    assert (await service.read_change_diff("demo", change_id))["text"] == "+hello\n"
    with pytest.raises(FileNotFoundError):
        await service.read_change_diff("other", change_id)
    diff.write_text("tampered\n")
    with pytest.raises(ValueError, match="hash differs"):
        await service.read_change_diff("demo", change_id)


def test_failed_refresh_retains_last_complete_generation(tmp_path, monkeypatch):
    _, maps, _, roots = setup(tmp_path)
    before = maps.current_ref("demo")
    def fail(*args, **kwargs): raise RuntimeError("scan failure")
    monkeypatch.setattr(maps, "scan_files", fail)
    with pytest.raises(RuntimeError): maps.refresh_files("demo", roots=roots)
    after = maps.current_ref("demo")
    assert after["generation_id"] == before["generation_id"]
    assert after["freshness"] == "stale"


def test_context_manifest_pins_generation_and_source_hash_and_reports_stale(tmp_path):
    _, maps, vault, roots = setup(tmp_path)
    source = vault / "main.py"; source.write_text("hello\n")
    index = vault / "knowledge/map/index.yaml"
    payload = yaml.safe_load(index.read_text())
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    payload["components"] = [{"component_id": "demo.source", "purpose": "source entry",
                              "source_refs": ["workspace://project/main.py"], "source_sha256": expected}]
    index.write_text(yaml.safe_dump(payload)); maps.publish_current("demo")
    context = maps.materialize_context("demo", "turn-1", task_kind="requirements",
                                       query_terms=["source"], component_ids=["demo.source"])
    assert "/generations/" in context["map_uri"]
    assert context["entries"][0]["ref"]["sha256"] == expected
    assert context["source_diagnostics"][0]["status"] == "current"
    source.write_text("changed\n")
    query = maps.query("demo", task_kind="requirements", query_terms=["source"], roots=roots)
    assert query["source_diagnostics"][0]["status"] == "stale"
    assert query["map_ref"]["freshness"] == "stale"


def test_recursive_grant_parent_is_not_an_automatic_map_discovery_root(tmp_path):
    _, maps, _, _ = setup(tmp_path)
    source = tmp_path / "authorized-parent"; source.mkdir()
    (source / "managed.py").write_text("managed\n")
    (source / "unrelated.py").write_text("unrelated\n")
    roots = [AuthorizedRoot("source", source, "read", "broad Grant", recursive=True)]
    assert maps.scan_files("demo", roots) == {}
    maps.refresh_files("demo", roots=roots, changes=[{"target_uri": "workspace://source/managed.py"}])
    files = maps.load("demo")["files"]
    assert [item["path"] for item in files] == ["managed.py"]
    assert maps.load("demo")["files_coverage"] == "managed_sources"
    assert roots[0].recursive is True


@pytest.mark.asyncio
async def test_butler_creation_privilege_is_bound_to_actual_intake_actor():
    # Load the actual authorization function without starting a stdio server.
    from assistant_kernel.services.kernel_management import RETIRED_TOOLS
    assert {"create_project", "propose_project"} <= RETIRED_TOOLS


@pytest.mark.asyncio
async def test_kernel_document_receipt_indexes_only_latest_head_without_expanding_model_scope(tmp_path):
    settings, maps, vault, _ = setup(tmp_path)
    roots = [AuthorizedRoot("project", vault, "read", "model view", excludes=("runs", "runs/**"))]
    database = FakeDatabase(); service = ProjectChangeService(settings, database)
    documents = TaskDocumentService(settings)
    def revision(value):
        return documents.write_revision(project_id="demo", task_id="task", document_kind="requirements",
            document_schema="requirements.v1", payload={"requirements": [value]}, status="ready",
            actor_type="pm", actor_id="pm", run_id="task", reason="updated requirement")
    first = revision("one")
    receipt1 = documents.change_receipt("demo", first, purpose="当前需求", state_revision=1, context_epoch=1)
    with pytest.raises(PermissionError):
        from assistant_kernel.services.workspace_scope import WorkspaceScopeService
        WorkspaceScopeService(settings).resolve_uri(roots, receipt1["target_uri"])
    batch1 = await service.capture_batch("demo", batch_id="doc-1", roots=roots, changes=[receipt1],
        actor="kernel", scope_kind="artifact", reason="Kernel accepted requirement revision", reason_source_refs=[first])
    assert batch1["ledger_committed"] is True
    assert maps.load("demo")["files"][0]["kind"] == "document"
    second = revision("two")
    receipt2 = documents.change_receipt("demo", second, previous_ref=first, purpose="当前需求",
                                         state_revision=2, context_epoch=2)
    assert documents.change_receipt("demo", second, previous_ref=second) is None
    assert receipt2["diff_basis"] == "previous_document_revision" and receipt2["lines_added"] > 0
    batch2 = await service.capture_batch("demo", batch_id="doc-2", roots=roots, changes=[receipt2],
        actor="kernel", scope_kind="artifact", reason="Kernel accepted revised requirement", reason_source_refs=[second])
    files = maps.load("demo")["files"]
    assert len(files) == 1 and files[0]["document_ref"] == second
    diff = await service.read_change_diff("demo", batch2["changes"][0]["change_id"])
    assert '"one"' in diff["text"] and '"two"' in diff["text"]
    assert roots[0].excludes == ("runs", "runs/**")
    await service.capture_batch("demo", batch_id="late-old-doc", roots=roots, changes=[receipt1],
        actor="kernel", scope_kind="artifact", reason="late historical record")
    assert maps.load("demo")["files"][0]["document_ref"] == second
    assert len(database.events) == 3  # Historical creation evidence remains auditable.


@pytest.mark.asyncio
async def test_pending_reconciliation_has_a_batch_bound(tmp_path):
    settings, _, vault, roots = setup(tmp_path)
    database = FakeDatabase(); database.fail = True
    service = ProjectChangeService(settings, database)
    for index in range(3):
        target = vault / f"source-{index}.py"; target.write_text(f"print({index})\n")
        await service.capture_batch("demo", batch_id=f"pending-{index}", roots=roots,
            changes=[{"target_uri": f"workspace://project/{target.name}",
                      "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}])
    database.fail = False
    recovered = await service.reconcile_pending("demo", roots=roots, max_batches=1)
    assert len(recovered["recovered_batches"]) == 1 and recovered["pending_batches_remaining"] == 2
    assert len(list((vault / "runtime/change-pending").glob("*.json"))) == 2
    with pytest.raises(ValueError): await service.reconcile_pending("demo", roots=roots, max_batches=0)
