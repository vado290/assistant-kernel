from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.services.agent_files import (
    AgentFileLoader, ChannelRouter, RouteResolver, unbound_route,
)
from assistant_kernel.services.project_directory import (
    ProjectDirectoryService, default_phase_profiles, default_pm_config, default_work_modes,
)
from assistant_kernel.storage import ProjectRuntimeManager


def setup_project(tmp_path: Path):
    settings = KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path, "provider_secrets_path": tmp_path / "providers.json",
    })
    root = ProjectRuntimeManager(tmp_path).ensure("demo").vault_root
    (root / "project.yaml").write_text(yaml.safe_dump({
        "schema_version": "project.v2", "project_id": "demo", "layout_version": 2,
    }), encoding="utf-8")
    agent_root = root / "agents" / "pm"
    agent_root.mkdir(parents=True)
    config = default_pm_config("demo")
    config["mcp_servers"] = {"data": {"enabled_tools": ["read", "write"], "purpose": "configured source"}}
    (agent_root / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (agent_root / "role.md").write_text("# Domain PM\nNever invent trading rules.\n", encoding="utf-8")
    loader = AgentFileLoader(settings)
    return settings, root, loader, loader.load("demo", "project:demo:manager")


@pytest.mark.parametrize(("mode", "skill", "display"), [
    ("requirements", "assistant-requirements", "requirements"),
    ("design", "assistant-solution-design", "design"),
    ("build", "assistant-implementation", "implementation"),
    ("audit", "assistant-release-audit", "review"),
    ("operate", "assistant-operation", "operation"),
])
def test_exact_phase_skills_tools_and_domain_role(tmp_path, mode, skill, display):
    _, _, loader, base = setup_project(tmp_path)
    profile = default_phase_profiles()[mode]
    selected = loader.apply_phase_profile(base, profile)
    assert selected.phase_mode == mode
    assert "Never invent trading rules." in selected.role_text
    assert f"# Active phase · {display}" in selected.role_text
    assert {b["name"] for b in selected.skill_bindings} == {
        item["name"] for item in profile["skills"]["required"]
    }
    assert skill in {b["name"] for b in selected.skill_bindings}
    assert selected.allowed_tools == tuple(profile["tools"])
    assert selected.mcp_servers == ()
    assert ("write_candidate_file" in selected.allowed_tools) == (mode == "build")
    assert selected.config_sha256 != base.config_sha256


def test_phase_switch_replaces_append_and_preserves_capability_ceiling(tmp_path):
    _, _, loader, base = setup_project(tmp_path)
    profiles = default_phase_profiles()
    requirements = loader.apply_phase_profile(base, profiles["requirements"])
    design = loader.apply_phase_profile(requirements, profiles["design"])
    implementation = loader.apply_phase_profile(design, profiles["build"])
    assert "# Active phase · requirements" not in implementation.role_text
    assert "# Active phase · design" not in implementation.role_text
    assert "write_candidate_file" in implementation.allowed_tools
    assert "assistant-requirements" not in {b["name"] for b in implementation.skill_bindings}
    assert loader.apply_phase_profile(implementation, profiles["build"]).config_sha256 == implementation.config_sha256


def test_profile_skill_and_tool_expansion_is_rejected(tmp_path):
    _, _, loader, base = setup_project(tmp_path)
    profile = copy.deepcopy(default_phase_profiles()["design"])
    profile["tools"].append("ungranted_write")
    with pytest.raises(PermissionError, match="additional MCP tools"):
        loader.apply_phase_profile(base, profile)
    profile = copy.deepcopy(default_phase_profiles()["design"])
    profile["skills"]["available"].append({"name": "not-published", "source": "kernel"})
    with pytest.raises(PermissionError, match="not configured"):
        loader.apply_phase_profile(base, profile)
    with pytest.raises(PermissionError, match="not available"):
        loader.apply_phase_profile(base, default_phase_profiles()["design"], requested_skills=["assistant-operation"])
    profile = copy.deepcopy(default_phase_profiles()["design"])
    profile["tools"].append("write_candidate_file")
    profile["disabled_tools"] = []
    with pytest.raises(PermissionError, match="cannot expose candidate writes"):
        loader.apply_phase_profile(base, profile)


def test_optional_skill_and_exact_mcp_selection(tmp_path):
    _, _, loader, base = setup_project(tmp_path)
    profile = copy.deepcopy(default_phase_profiles()["design"])
    profile["mcp_servers"] = {"data": {"enabled_tools": ["read"]}}
    selected = loader.apply_phase_profile(base, profile, requested_skills=["assistant-agent-design"])
    assert "assistant-agent-design" in {b["name"] for b in selected.skill_bindings}
    assert selected.mcp_servers[0]["enabled_tools"] == ["read"]
    assert selected.mcp_servers[0]["purpose"] == "configured source"
    profile["mcp_servers"]["data"]["enabled_tools"] = ["unknown"]
    with pytest.raises(PermissionError, match="additional MCP server tools"):
        loader.apply_phase_profile(base, profile)


def test_common_protocol_cannot_be_removed_and_repair_is_build(tmp_path):
    _, _, loader, base = setup_project(tmp_path)
    profile = copy.deepcopy(default_phase_profiles()["build"])
    profile["mode_id"] = "repair"
    assert loader.apply_phase_profile(base, profile).phase_mode == "build"
    profile["skills"]["required"] = [
        item for item in profile["skills"]["required"] if item["name"] != "assistant-source-scope"
    ]
    with pytest.raises(ValueError, match="retain continuity"):
        loader.apply_phase_profile(base, profile)


def test_snapshot_keeps_phase_context_after_live_skill_changes(tmp_path):
    _, root, loader, base = setup_project(tmp_path)
    path = root / ".agents" / "skills" / "assistant-solution-design" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("Original design instructions", encoding="utf-8")
    selected = loader.apply_phase_profile(base, default_phase_profiles()["design"])
    uri, sha = loader.snapshot("demo", selected)
    path.write_text("Unaccepted new instructions", encoding="utf-8")
    pinned = loader.load_snapshot("demo", uri, sha)
    assert pinned.phase_mode == "design"
    assert pinned.skill_texts == selected.skill_texts
    assert pinned.common_role_text == selected.common_role_text
    assert pinned.available_tools == selected.available_tools
    assert pinned.available_skills == selected.available_skills
    assert pinned.available_mcp_servers == selected.available_mcp_servers


def test_bootstrap_copies_fixed_five_stage_templates_and_legacy_system_path(tmp_path):
    settings = KernelSettings.from_environment().model_copy(update={
        "data_root": tmp_path, "provider_secrets_path": tmp_path / "providers.json",
    })
    service = ProjectDirectoryService(settings, SimpleNamespace())
    service._bootstrap("demo", "Test project", proposal={"source_run_id": "run-1"},
                       proposal_uri="vault://system/proposal.yaml", proposal_sha256="abc")
    root = ProjectRuntimeManager(tmp_path).describe("demo").vault_root
    project = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    assert project["single_agent"]["design_required"] is True
    assert set(project["work_modes"]) == {"requirements", "design", "build", "audit", "operate"}
    assert set(default_work_modes()) == set(default_phase_profiles())
    config = AgentFileLoader(settings).load("demo", "project:demo:manager")
    selected = AgentFileLoader(settings).apply_phase_profile(config, default_phase_profiles()["design"])
    assert all(binding["source"] == "project" for binding in selected.skill_bindings)
    assert (root / ".agents" / "skills" / "assistant-operation" / "SKILL.md").is_file()
    template_root = service.seed_base_templates()
    manifest_before = (template_root / "template-manifest.json").read_bytes()
    assert service.seed_base_templates() == template_root
    assert (template_root / "template-manifest.json").read_bytes() == manifest_before
    assert ProjectRuntimeManager(tmp_path).describe("system").vault_root == tmp_path / "butler"
    manifest = json.loads(manifest_before)
    assert "design" in manifest["profiles"]


def test_new_unbound_intake_route_uses_canonical_project_only_after_migration(tmp_path):
    settings, _, _, _ = setup_project(tmp_path)
    router = ChannelRouter(settings)
    assert router.resolve("discord", "new-chat", "user") == ("kernel", "retired:management")
    path = tmp_path / "vaults" / "butler-maintenance" / "agents" / "intake" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("kind: butler\n", encoding="utf-8")
    expected = ("kernel", "retired:management")
    assert unbound_route(settings) == expected
    assert router.resolve("discord", "new-chat", "user") == expected
    assert router.resolve("discord", "old-chat", "user", "system", "system-butler") == expected


@pytest.mark.asyncio
async def test_legacy_question_reply_keeps_its_original_system_identity(tmp_path):
    settings, _, _, _ = setup_project(tmp_path)
    path = tmp_path / "vaults" / "butler-maintenance" / "agents" / "intake" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("kind: butler\n", encoding="utf-8")
    class Repository:
        async def get_message(self, message_id):
            return {"direction": "outbound", "channel": "discord", "chat_id": "old-chat",
                    "user_id": "user", "project_id": "system", "run_id": "old-run"}
        async def get_run(self, run_id):
            return {"agent_key": "system-butler"}
    assert await RouteResolver(settings, Repository()).resolve(
        "discord", "old-chat", "user", reply_to_message_id="old-question",
    ) == ("system", "system-butler")


@pytest.mark.asyncio
async def test_existing_home_system_binding_maps_only_new_messages_to_canonical(tmp_path):
    settings, _, _, _ = setup_project(tmp_path)
    path = tmp_path / "vaults" / "butler-maintenance" / "agents" / "intake" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("kind: butler\n", encoding="utf-8")
    class Repository:
        binding = ("system", "system-butler")
        async def resolve_route_binding(self, *args):
            return self.binding
        async def get_message(self, identity):
            return {"direction": "outbound", "channel": "discord", "chat_id": "home", "user_id": "user",
                    "project_id": "system", "run_id": "old-run"}
        async def get_run(self, identity):
            return {"agent_key": "system-butler"}
    repository = Repository()
    resolver = RouteResolver(settings, repository)
    assert await resolver.resolve("discord", "home", "user") == ("kernel", "retired:management")
    assert await resolver.resolve("discord", "home", "user", reply_to_message_id="old-question") == ("system", "system-butler")
    repository.binding = ("demo", "project:demo:manager")
    assert await resolver.resolve("discord", "home", "user") == repository.binding


def test_new_project_proposals_use_canonical_vault_and_legacy_retry_keeps_hash(tmp_path):
    settings, _, _, _ = setup_project(tmp_path)
    service = ProjectDirectoryService(settings, SimpleNamespace())
    with pytest.raises(PermissionError,match="retired"):
        service.propose("old-run", "new-project", "purpose")
