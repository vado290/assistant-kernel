from pathlib import Path

import pytest
import tomli

from assistant_kernel.services.permission_policy import PermissionPolicyCompiler
from assistant_kernel.services.workspace_scope import AuthorizedRoot


def test_recursive_roots_compile_as_unfiltered_native_write_profiles(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    source = tmp_path / "source"
    vault.mkdir()
    source.mkdir()
    (source / ".env").write_text("TEST_ONLY\n", encoding="utf-8")
    roots = [
        AuthorizedRoot("project", vault, "write", "Project Vault"),
        AuthorizedRoot(
            "approved", source, "write", "Approved source", grant_id="grant-1",
        ),
    ]
    name = PermissionPolicyCompiler.profile_name("run-1")
    encoded, may_write = PermissionPolicyCompiler().compile("run-1", roots)
    data = tomli.loads(encoded)
    assert may_write is True
    assert data["default_permissions"] == name
    rules = data["permissions"][name]["filesystem"]
    assert rules[":root"] == "deny"
    assert rules[str(vault)]["."] == "write"
    assert rules[str(source)]["."] == "write"
    assert ".env" not in rules[str(source)]
    assert data["permissions"][name]["network"]["enabled"] is True


def test_legacy_narrow_scope_cannot_silently_become_a_writable_directory(tmp_path: Path) -> None:
    root = tmp_path / "scope"
    root.mkdir()
    (root / "allowed.py").write_text("pass\n", encoding="utf-8")
    policy = PermissionPolicyCompiler()
    with pytest.raises(ValueError, match="requires an enforceable directory Root"):
        policy.compile("run-2", [
            AuthorizedRoot("narrow", root, "write", "Narrow scope", includes=("allowed.py",)),
        ])
    with pytest.raises(ValueError, match="requires an enforceable directory Root"):
        policy.compile("run-3", [
            AuthorizedRoot("unsupported", root, "write", "Unsupported", includes=("data/*.csv",)),
        ])


def test_recursive_write_root_does_not_deny_symlinks(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    target = second / "target.sh"
    target.write_text("echo ok\n", encoding="utf-8")
    (first / "linked.sh").symlink_to(target)
    text, may_write = PermissionPolicyCompiler().compile("run-links", [
        AuthorizedRoot("first", first, "write", "first", recursive=True),
        AuthorizedRoot("second", second, "write", "second", recursive=True),
    ])
    assert may_write is True
    rules = tomli.loads(text)["permissions"]["kernel_run_run_links"]["filesystem"]
    assert rules[str(first)]["."] == "write"
    assert "linked.sh" not in rules[str(first)]


def test_candidate_only_permission_profile_disables_native_network(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    text, may_write = PermissionPolicyCompiler().compile(
        "candidate-run",
        [AuthorizedRoot("candidate", candidate, "write", "candidate")],
        network_enabled=False,
    )
    assert may_write is True
    data = tomli.loads(text)
    assert data["permissions"]["kernel_run_candidate_run"]["network"]["enabled"] is False
