import json
from pathlib import Path

import assistant_kernel.services.file_effects as file_effects
from assistant_kernel.services.file_effects import FileEffectRecorder
from assistant_kernel.services.workspace_scope import AuthorizedRoot


def test_direct_run_records_changes_and_keeps_baseline(tmp_path: Path) -> None:
    vault = tmp_path / "vaults" / "demo"
    source = tmp_path / "source"
    vault.mkdir(parents=True)
    source.mkdir()
    original = source / "strategy.py"
    original.write_text("OLD = True\n", encoding="utf-8")
    (source / ".env").write_text("TEST_ONLY\n", encoding="utf-8")
    protected = tmp_path / "protected"
    recorder = FileEffectRecorder(vault, "run-1", "attempt-1", [
        AuthorizedRoot(
            "source", source, "write", "source",
            excludes=(".env",), grant_id="grant-1",
        ),
    ], protected_backup_root=protected)
    recorder.capture_before()
    original.write_text("OLD = False\n", encoding="utf-8")
    (source / "new.py").write_text("NEW = True\n", encoding="utf-8")
    receipt = recorder.capture_after()
    assert receipt["changed_file_count"] == 2
    assert (protected / "source" / "strategy.py").read_text() == "OLD = True\n"
    assert not (protected / "source" / ".env").exists()
    assert not (recorder.root / "before").exists()
    changes = json.loads((recorder.root / "changes.json").read_text())["changes"]
    assert {item["path"] for item in changes} == {"source/strategy.py", "source/new.py"}


def test_direct_run_records_file_and_empty_directory_creation_deletion(tmp_path: Path) -> None:
    vault = tmp_path / "vaults" / "demo"
    source = tmp_path / "source"
    vault.mkdir(parents=True)
    source.mkdir()
    old_directory = source / "old_empty"
    old_directory.mkdir()
    old_file = source / "old.py"
    old_file.write_text("ORIGINAL\n", encoding="utf-8")
    protected = tmp_path / "protected"
    recorder = FileEffectRecorder(vault, "run-2", "attempt-1", [
        AuthorizedRoot("source", source, "write", "source", grant_id="grant-1"),
    ], protected_backup_root=protected)
    recorder.capture_before()
    old_file.unlink()
    old_directory.rmdir()
    (source / "new_empty").mkdir()
    (source / "new.py").write_text("NEW\n", encoding="utf-8")
    receipt = recorder.capture_after()
    changes = json.loads((recorder.root / "changes.json").read_text())["changes"]
    assert receipt["changed_file_count"] == 2
    assert receipt["changed_directory_count"] == 2
    assert {item["path"] for item in changes} == {
        "source/old_empty", "source/old.py", "source/new_empty", "source/new.py",
    }
    assert (protected / "source" / "old_empty").is_dir()
    assert (protected / "source" / "old.py").read_text() == "ORIGINAL\n"


def test_direct_run_records_symlink_without_following_it(tmp_path: Path) -> None:
    vault = tmp_path / "vaults" / "demo"
    source = tmp_path / "source"
    vault.mkdir(parents=True)
    source.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    recorder = FileEffectRecorder(vault, "run-3", "attempt-1", [
        AuthorizedRoot("source", source, "write", "source", grant_id="grant-1"),
    ], protected_backup_root=tmp_path / "protected")
    recorder.capture_before()
    (source / "link.py").symlink_to(outside)
    receipt = recorder.capture_after()
    changes = json.loads((recorder.root / "changes.json").read_text())["changes"]
    assert receipt["changed_file_count"] == 1
    assert changes[0]["path"] == "source/link.py"
    assert changes[0]["after"]["kind"] == "symlink"
    assert not (tmp_path / "protected" / "source" / "link.py").exists()


def test_large_recursive_scope_degrades_to_metadata_without_blocking(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(file_effects, "MAX_FILES", 1)
    monkeypatch.setattr(file_effects, "MAX_BYTES", 8)
    vault = tmp_path / "vaults" / "demo"
    source = tmp_path / "large-source"
    vault.mkdir(parents=True)
    source.mkdir()
    first = source / "first.py"
    second = source / "second.py"
    first.write_text("FIRST\n", encoding="utf-8")
    second.write_text("SECOND\n", encoding="utf-8")
    recorder = FileEffectRecorder(vault, "run-large", "attempt-1", [
        AuthorizedRoot("source", source, "write", "source", grant_id="grant-1"),
    ], protected_backup_root=tmp_path / "protected")
    recorder.capture_before()
    second.write_text("CHANGED\n", encoding="utf-8")
    receipt = recorder.capture_after()
    payload = json.loads((recorder.root / "changes.json").read_text())
    assert receipt["changed_file_count"] == 1
    assert payload["changes"][0]["path"] == "source/second.py"
    assert payload["baseline"]["metadata_only_file_count"] >= 1


def test_recursive_parent_root_skips_full_tree_preflight(tmp_path: Path) -> None:
    vault = tmp_path / "vaults" / "demo"
    source = tmp_path / "recursive-source"
    vault.mkdir(parents=True)
    source.mkdir()
    (source / "large.bin").write_bytes(b"x" * 1024)
    recorder = FileEffectRecorder(vault, "run-recursive", "attempt-1", [
        AuthorizedRoot(
            "source", source, "write", "source",
            grant_id="grant-1", recursive=True,
        ),
    ])
    assert recorder.enabled is False
    recorder.capture_before()
    assert recorder.capture_after() is None
