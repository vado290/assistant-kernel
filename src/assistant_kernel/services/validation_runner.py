from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.services.semantic_map import SemanticMapService


SKILL_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
VALIDATION_SCOPES = {
    "structure", "component", "contract", "workflow_path",
    "workflow_e2e", "runtime_observation",
}


class ValidationRunner:
    """Deterministic structural validation between Implementation and Review."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def _merge_overlay(
        self, formal: Path, candidate: Path, output: Path,
        deleted: set[tuple[str, ...]], relative: tuple[str, ...] = (),
    ) -> None:
        output.mkdir(parents=True, exist_ok=True)
        names = set()
        if formal.is_dir():
            names.update(item.name for item in formal.iterdir())
        if candidate.is_dir():
            names.update(item.name for item in candidate.iterdir())
        for name in sorted(names):
            child_relative = (*relative, name)
            if child_relative in deleted:
                continue
            source = formal / name
            changed = candidate / name
            target = output / name
            if changed.is_file():
                shutil.copy2(changed, target)
            elif changed.is_dir():
                self._merge_overlay(
                    source if source.is_dir() else Path("/__missing_formal__"),
                    changed, target, deleted, child_relative,
                )
            elif source.is_dir() and any(
                path[:len(child_relative)] == child_relative for path in deleted
            ):
                self._merge_overlay(
                    source, changed, target, deleted, child_relative,
                )
            elif source.exists():
                target.symlink_to(source, target_is_directory=source.is_dir())

    def _run_python_unittest_profiles(
        self, *, project_id: str, run_id: str, candidate_root: Path,
        policy: dict[str, Any], report_root: Path,
        deleted_by_root: dict[str, set[tuple[str, ...]]],
        issues: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        profiles = policy.get("python_unittest") or []
        if not isinstance(profiles, list):
            self._issue(
                issues, "validation_python_unittest_type", "kernel-policy",
                "python_unittest must be a list",
            )
            return []
        if not profiles:
            return []
        roots = {
            root.key: root for root in WorkspaceScopeService(self.settings).resolve(
                project_id, run_id, None,
            ) if root.key not in {"candidate", "run_work"}
        }
        targets: list[dict[str, Any]] = []
        overlay_base = report_root / "overlay"
        evidence_root = report_root / "commands"
        evidence_root.mkdir(parents=True, exist_ok=True)
        for index, raw in enumerate(profiles, start=1):
            if not isinstance(raw, dict):
                self._issue(
                    issues, "validation_python_unittest_entry", "kernel-policy",
                    "python_unittest entry must be an object",
                )
                continue
            target_id = str(raw.get("target_id") or f"python-unittest-{index}")
            root_key = str(raw.get("root_key") or "")
            module = str(raw.get("module") or "")
            required = bool(raw.get("required", True))
            timeout = min(max(int(raw.get("timeout_seconds") or 120), 1), 600)
            min_tests = max(int(raw.get("min_tests") or 1), 1)
            if root_key not in roots or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_.]{0,255}", module
            ):
                self._issue(
                    issues, "validation_python_unittest_config", target_id,
                    "python_unittest needs an authorized root_key and safe module",
                )
                continue
            overlay = overlay_base / root_key
            if overlay.exists():
                shutil.rmtree(overlay)
            self._merge_overlay(
                roots[root_key].path,
                candidate_root / root_key,
                overlay, deleted_by_root.get(root_key, set()),
            )
            unshare = shutil.which("unshare")
            command = (
                [
                    unshare, "--user", "--map-root-user", "--mount", "--",
                    "/bin/sh", "-c",
                    (
                        'mount --make-rprivate / && mount --bind "$1" "$1" && '
                        'mount -o remount,bind,ro "$1" && shift && exec "$@"'
                    ),
                    "validation-shell", str(self.settings.workspace_root),
                    sys.executable, "-m", "unittest", "-v", module,
                ]
                if unshare and sys.platform == "linux" else
                [sys.executable, "-m", "unittest", "-v", module]
            )
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(overlay),
                "TASK_VALIDATION_NETWORK": "enabled",
            }
            try:
                completed = subprocess.run(
                    command, cwd=str(overlay), env=environment,
                    capture_output=True, text=True, timeout=timeout, check=False,
                )
                output = completed.stdout + "\n" + completed.stderr
                match = re.search(r"Ran\s+(\d+)\s+tests?", output)
                tests_run = int(match.group(1)) if match else 0
                passed = completed.returncode == 0 and tests_run >= min_tests
                evidence = {
                    "schema_version": "validation-command.v1",
                    "target_id": target_id,
                    "runner": "python_unittest",
                    "root_key": root_key,
                    "module": module,
                    "argv": command,
                    "exit_code": completed.returncode,
                    "tests_run": tests_run,
                    "min_tests": min_tests,
                    "network_guard": "network-enabled",
                    "stdout": completed.stdout[-20000:],
                    "stderr": completed.stderr[-20000:],
                }
            except subprocess.TimeoutExpired as exc:
                passed = False
                evidence = {
                    "schema_version": "validation-command.v1",
                    "target_id": target_id, "runner": "python_unittest",
                    "root_key": root_key, "module": module, "argv": command,
                    "exit_code": None, "tests_run": 0, "min_tests": min_tests,
                    "network_guard": "network-enabled",
                    "timeout": True,
                    "stdout": str(exc.stdout or "")[-20000:],
                    "stderr": str(exc.stderr or "")[-20000:],
                }
            evidence_path = evidence_root / f"{target_id}.json"
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )
            evidence_uri = (
                f"vault://{project_id}/"
                f"{evidence_path.relative_to(self.runtime.ensure(project_id).vault_root).as_posix()}"
            )
            targets.append({
                "target_id": target_id, "scope": "workflow_e2e",
                "required": required, "status": "passed" if passed else "failed",
                "evidence": [evidence_uri],
            })
            if not passed and required:
                self._issue(
                    issues, "validation_python_unittest_failed", target_id,
                    f"{module} failed or ran fewer than {min_tests} tests",
                )
        return targets

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _issue(
        issues: list[dict[str, str]], code: str, path: str, message: str
    ) -> None:
        issues.append({"code": code, "path": path, "message": message})

    def _validate_skill(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        text = path.read_text(encoding="utf-8")
        match = SKILL_FRONTMATTER.match(text)
        if match is None:
            self._issue(issues, "skill_frontmatter", relative, "SKILL.md lacks YAML frontmatter")
            return
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "skill_frontmatter", relative, f"invalid frontmatter: {exc}")
            return
        if not str(metadata.get("name") or "").strip():
            self._issue(issues, "skill_name", relative, "Skill name is required")
        if not str(metadata.get("description") or "").strip():
            self._issue(issues, "skill_description", relative, "Skill description is required")
        if not re.search(r"^##\s+任务目标\s*$", text, re.MULTILINE):
            self._issue(issues, "skill_task_goal", relative, "Skill must contain '## 任务目标'")

    def _validate_agent_config(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "agent_config_yaml", relative, f"invalid YAML: {exc}")
            return
        if not isinstance(data, dict):
            self._issue(issues, "agent_config_type", relative, "Agent config must be an object")
            return
        if not str(data.get("agent_key") or "").strip():
            self._issue(issues, "agent_key", relative, "agent_key is required")
        schema_version = str(data.get("schema_version") or "")
        if schema_version == "agent-config.v2":
            for key in ("agent_id", "kind", "status", "role", "scope_profile"):
                if not data.get(key):
                    self._issue(
                        issues, f"agent_{key}", relative, f"{key} is required for agent-config.v2"
                    )
            if not isinstance(data.get("harness"), dict):
                self._issue(
                    issues, "agent_harness", relative,
                    "harness must be an object for agent-config.v2",
                )
            if not isinstance(data.get("entrypoints"), dict):
                self._issue(
                    issues, "agent_entrypoints", relative,
                    "entrypoints must be an object for agent-config.v2",
                )
        if "skills" in data and not isinstance(data["skills"], (list, dict)):
            self._issue(issues, "agent_skills", relative, "skills must be a list or object")
        if "tools" in data and not isinstance(data["tools"], list):
            self._issue(issues, "agent_tools", relative, "tools must be a list")
        if "mcp_servers" in data and not isinstance(data["mcp_servers"], dict):
            self._issue(issues, "agent_mcp", relative, "mcp_servers must be an object")

    def _validate_mcp_manifest(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "mcp_manifest_yaml", relative, f"invalid YAML: {exc}")
            return
        if not isinstance(data, dict):
            self._issue(issues, "mcp_manifest_type", relative, "MCP manifest must be an object")
            return
        if not str(data.get("server_id") or "").strip():
            self._issue(issues, "mcp_server_id", relative, "server_id is required")
        transport = str(data.get("transport") or "")
        if transport not in {"stdio", "streamable-http"}:
            self._issue(
                issues, "mcp_transport", relative,
                "transport must be stdio or streamable-http",
            )
        tools = data.get("tools")
        if not isinstance(tools, list) or not tools:
            self._issue(issues, "mcp_tools", relative, "tools must be a nonempty list")
            return
        names: list[str] = []
        for item in tools:
            name = str(item.get("name") or "") if isinstance(item, dict) else ""
            if not name:
                self._issue(issues, "mcp_tool_name", relative, "every Tool needs a name")
            names.append(name)
            effect = str(item.get("effect") or "") if isinstance(item, dict) else ""
            if effect not in {"read", "write", "external", "trade"}:
                self._issue(
                    issues, "mcp_tool_effect", relative,
                    "every Tool needs effect: read, write, external, or trade",
                )
        duplicates = sorted({name for name in names if name and names.count(name) > 1})
        if duplicates:
            self._issue(
                issues, "mcp_tool_duplicate", relative,
                "duplicate Tool names: " + ", ".join(duplicates),
            )
        if transport == "stdio" and not str(data.get("entrypoint") or "").endswith(".py"):
            self._issue(
                issues, "mcp_entrypoint", relative,
                "STDIO MCP requires a relative Python entrypoint",
            )
        if transport == "streamable-http" and not str(
            data.get("endpoint_ref") or ""
        ).startswith("env://"):
            self._issue(
                issues, "mcp_endpoint_ref", relative,
                "Streamable HTTP MCP requires an env:// endpoint_ref",
            )

    def _validate_project(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "project_yaml", relative, f"invalid YAML: {exc}")
            return
        if not isinstance(data, dict):
            self._issue(issues, "project_type", relative, "project.yaml must be an object")
            return
        if data.get("schema_version") != "project.v2":
            self._issue(issues, "project_schema", relative, "schema_version must be project.v2")
        if data.get("layout_version") != 2:
            self._issue(issues, "project_layout", relative, "layout_version must be 2")
        for key in ("project_id", "status", "default_agent"):
            if not data.get(key):
                self._issue(issues, f"project_{key}", relative, f"{key} is required")
        routing = data.get("routing")
        if routing is not None:
            if not isinstance(routing, dict):
                self._issue(
                    issues, "project_routing", relative, "routing must be an object"
                )
            else:
                for key in (
                    "aliases", "topics", "typical_requests", "exclusions",
                    "related_projects",
                ):
                    value = routing.get(key, [])
                    if not isinstance(value, list) or any(
                        not isinstance(item, str) or not item.strip() for item in value
                    ):
                        self._issue(
                            issues, f"project_routing_{key}", relative,
                            f"routing.{key} must be a list of nonempty strings",
                        )

    def _validate_source_map(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "source_map_yaml", relative, f"invalid YAML: {exc}")
            return
        if not isinstance(data, dict) or data.get("schema_version") != "project-source-map.v1":
            self._issue(
                issues, "source_map_schema", relative,
                "schema_version must be project-source-map.v1",
            )
            return
        roots = data.get("roots") or {}
        if not isinstance(roots, dict):
            self._issue(issues, "source_map_roots", relative, "roots must be an object")
            return
        for key, raw in roots.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(key)):
                self._issue(issues, "source_map_root_key", relative, f"invalid root key: {key}")
                continue
            if not isinstance(raw, dict):
                self._issue(issues, "source_map_root", relative, f"root {key} must be an object")
                continue
            if str(raw.get("mode") or "discovery") not in {
                "live", "discovery", "lazy_snapshot", "indexed",
            }:
                self._issue(issues, "source_map_mode", relative, f"unsupported mode for {key}")
            for field in ("entrypoints", "soft_excludes"):
                values = raw.get(field) or []
                if not isinstance(values, list) or any(
                    not isinstance(item, str) or not item.strip() for item in values
                ):
                    self._issue(
                        issues, f"source_map_{field}", relative,
                        f"{key}.{field} must be a list of nonempty strings",
                    )
            for entrypoint in raw.get("entrypoints") or []:
                candidate = Path(str(entrypoint).replace("\\", "/"))
                if candidate.is_absolute() or ".." in candidate.parts:
                    self._issue(
                        issues, "source_map_entrypoint", relative,
                        f"{key} entrypoint escapes its authorized root",
                    )

    def _validate_schedule(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "schedule_yaml", relative, f"invalid YAML: {exc}")
            return
        if not isinstance(data, dict):
            self._issue(issues, "schedule_type", relative, "Schedule must be an object")
            return
        for key in ("schedule_id", "timezone", "trigger", "target"):
            if not data.get(key):
                self._issue(issues, f"schedule_{key}", relative, f"{key} is required")
        execution = data.get("execution") or {}
        if execution and not isinstance(execution, dict):
            self._issue(issues, "schedule_execution", relative, "execution must be an object")
            return
        overlap = str(execution.get("overlap_policy") or "") if execution else ""
        if overlap and overlap not in {"skip", "queue-one", "replace"}:
            self._issue(
                issues, "schedule_overlap", relative,
                "overlap_policy must be skip, queue-one, or replace",
            )

    def _validate_semantic_map(
        self, path: Path, relative: str, issues: list[dict[str, str]],
    ) -> None:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            self._issue(issues, "semantic_map_yaml", relative, str(exc))
            return
        if not isinstance(data, dict) or data.get("schema_version") != "semantic-map.v1":
            self._issue(
                issues, "semantic_map_schema", relative,
                "semantic map must use semantic-map.v1",
            )
            return
        components = data.get("components") or []
        if not isinstance(components, list):
            self._issue(
                issues, "semantic_map_components", relative,
                "semantic map components must be a list",
            )
            return
        seen: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                self._issue(
                    issues, "semantic_map_component", relative,
                    "semantic component must be an object",
                )
                continue
            component_id = str(component.get("component_id") or "")
            if not component_id or component_id in seen:
                self._issue(
                    issues, "semantic_map_component_id", relative,
                    "semantic component IDs must be present and unique",
                )
            seen.add(component_id)
            if not str(component.get("purpose") or "").strip():
                self._issue(
                    issues, "semantic_map_purpose", relative,
                    f"semantic component has no purpose: {component_id}",
                )

    def _validate_file(
        self, path: Path, relative: str, issues: list[dict[str, str]]
    ) -> None:
        lowered = relative.lower()
        if path.suffix.lower() == ".py":
            try:
                compile(path.read_bytes(), relative, "exec")
            except (SyntaxError, ValueError) as exc:
                self._issue(issues, "python_syntax", relative, str(exc))
        if path.name == "SKILL.md":
            self._validate_skill(path, relative, issues)
        if path.name in {"agent-config.yaml", "config.yaml"} and (
            "/agents/" in f"/{lowered}" or "/stages/" in f"/{lowered}"
            or path.name == "agent-config.yaml"
        ):
            self._validate_agent_config(path, relative, issues)
        if path.name == "mcp-manifest.yaml":
            self._validate_mcp_manifest(path, relative, issues)
        if path.name == "project.yaml":
            self._validate_project(path, relative, issues)
        if path.name == "source-map.yaml":
            self._validate_source_map(path, relative, issues)
        if lowered.endswith("knowledge/map/index.yaml"):
            self._validate_semantic_map(path, relative, issues)
        if path.suffix.lower() in {".yaml", ".yml"} and (
            lowered.startswith("schedules/") or "/schedules/" in f"/{lowered}"
        ):
            self._validate_schedule(path, relative, issues)
        if path.suffix.lower() == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self._issue(issues, "json_syntax", relative, str(exc))

    def validate(
        self, project_id: str, implementation_run_id: str,
        validation_policy: dict[str, Any] | None = None,
        sealed_bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime.ensure(project_id)
        live_candidate_root = (
            runtime.vault_root / "runs" / implementation_run_id / "candidate"
        ).resolve()
        candidate_root = live_candidate_root
        manifest_path = live_candidate_root / "manifest.json"
        issues: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        checked_files: list[dict[str, Any]] = []
        deleted_by_root: dict[str, set[tuple[str, ...]]] = {}
        status = "passed"
        sealed_problem = False
        if sealed_bundle is not None:
            try:
                prefix = f"vault://{project_id}/"
                bundle_uri = str(sealed_bundle.get("bundle_uri") or "")
                if not bundle_uri.startswith(prefix):
                    raise ValueError("sealed candidate bundle belongs to another Project")
                bundle_path = self.runtime.resolve_vault_file(
                    project_id, bundle_uri[len(prefix):]
                )
                bundle_bytes = bundle_path.read_bytes()
                if self._sha256(bundle_bytes) != sealed_bundle.get("bundle_file_sha256"):
                    raise ValueError("sealed candidate bundle hash changed")
                bundle_payload = json.loads(bundle_bytes.decode("utf-8"))
                if bundle_payload.get("schema_version") != "candidate-bundle.v1":
                    raise ValueError("unsupported sealed candidate bundle")
                if self._sha256(manifest_path.read_bytes()) != bundle_payload.get(
                    "manifest_sha256"
                ):
                    raise ValueError("candidate manifest changed after sealing")
                candidate_root = bundle_path.parent / "files"
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                sealed_problem = True
                self._issue(
                    issues, "sealed_candidate_mismatch", "candidate-bundle",
                    str(exc),
                )

        manifest: dict[str, Any] = {}
        if not manifest_path.is_file():
            status = "failed"
            self._issue(
                issues, "candidate_manifest_missing", "manifest.json",
                "Implementation must produce a candidate manifest",
            )
            summary = "No candidate manifest was produced."
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                manifest = {}
                self._issue(issues, "candidate_manifest", "manifest.json", str(exc))
            if manifest.get("schema_version") != "candidate-manifest.v2":
                self._issue(
                    issues, "candidate_schema", "manifest.json",
                    "schema_version must be candidate-manifest.v2",
                )
            entries = manifest.get("files")
            if not isinstance(entries, list) or not entries:
                self._issue(
                    issues, "candidate_files", "manifest.json",
                    "candidate manifest must contain files",
                )
                entries = []
            seen_targets: set[str] = set()
            for raw in entries:
                if not isinstance(raw, dict):
                    self._issue(
                        issues, "candidate_entry", "manifest.json",
                        "every manifest entry must be an object",
                    )
                    continue
                operation = str(raw.get("operation") or "")
                target_uri = str(raw.get("target_uri") or "")
                candidate_uri = raw.get("candidate_uri")
                if operation not in {"create", "update", "delete"}:
                    self._issue(
                        issues, "candidate_operation", target_uri or "manifest.json",
                        "operation must be create, update, or delete",
                    )
                if not target_uri.startswith("workspace://"):
                    self._issue(
                        issues, "candidate_target", target_uri or "manifest.json",
                        "target_uri must be a workspace URI",
                    )
                if target_uri in seen_targets:
                    self._issue(
                        issues, "candidate_duplicate", target_uri,
                        "target_uri appears more than once",
                    )
                seen_targets.add(target_uri)
                if operation == "delete":
                    if candidate_uri is not None:
                        self._issue(
                            issues, "candidate_delete", target_uri,
                            "delete entries cannot contain candidate_uri",
                        )
                    if target_uri.startswith("workspace://"):
                        target_parts = PurePosixPath(
                            target_uri[len("workspace://"):]
                        ).parts
                        if len(target_parts) > 1 and ".." not in target_parts:
                            deleted_by_root.setdefault(target_parts[0], set()).add(
                                tuple(target_parts[1:])
                            )
                    continue
                prefix = "workspace://candidate/"
                if not isinstance(candidate_uri, str) or not candidate_uri.startswith(prefix):
                    self._issue(
                        issues, "candidate_uri", target_uri,
                        "create/update entries need a candidate workspace URI",
                    )
                    continue
                relative = candidate_uri[len(prefix):]
                pure = PurePosixPath(relative)
                if pure.is_absolute() or ".." in pure.parts or not pure.parts:
                    self._issue(issues, "candidate_path", relative, "candidate path is invalid")
                    continue
                candidate = (candidate_root / Path(*pure.parts)).resolve()
                if candidate_root not in candidate.parents:
                    self._issue(issues, "candidate_escape", relative, "candidate path escaped")
                    continue
                if not candidate.is_file():
                    self._issue(issues, "candidate_missing", relative, "candidate file is missing")
                    continue
                payload = candidate.read_bytes()
                actual_hash = self._sha256(payload)
                if raw.get("candidate_sha256") != actual_hash:
                    self._issue(issues, "candidate_hash", relative, "candidate SHA-256 differs")
                if int(raw.get("size") or -1) != len(payload):
                    self._issue(issues, "candidate_size", relative, "candidate size differs")
                checked_files.append({
                    "path": relative, "sha256": actual_hash, "size": len(payload),
                })
                self._validate_file(candidate, relative, issues)
            semantic_map = SemanticMapService(self.settings).load(project_id)
            registered = {
                str(ref): component
                for component in semantic_map.get("components") or []
                for ref in component.get("source_refs") or []
                if str(ref).startswith("workspace://")
            }
            affected = {
                str(component.get("component_id")): component
                for target in seen_targets
                for ref, component in registered.items()
                if target == ref or target.startswith(ref.rstrip("/") + "/")
            }
            map_impact = manifest.get("map_impact")
            if map_impact is not None and not isinstance(map_impact, dict):
                self._issue(
                    issues, "map_impact_type", "manifest.json",
                    "map_impact must be an object",
                )
                map_impact = {}
            declared_ids = {
                str(item) for item in (map_impact or {}).get("affected_component_ids") or []
            }
            changed_map_files = {
                str(item) for item in (map_impact or {}).get("changed_map_files") or []
            }
            critical_affected = {
                component_id: component for component_id, component in affected.items()
                if SemanticMapService.is_critical(component)
            }
            if critical_affected:
                if not map_impact:
                    self._issue(
                        issues, "map_impact_missing", "manifest.json",
                        "critical semantic components require map_impact",
                    )
                missing_ids = sorted(set(critical_affected) - declared_ids)
                if missing_ids:
                    self._issue(
                        issues, "map_impact_components", "manifest.json",
                        "critical affected components were not declared: "
                        + ", ".join(missing_ids),
                    )
                if not bool((map_impact or {}).get("documentation_required")):
                    self._issue(
                        issues, "map_documentation_required", "manifest.json",
                        "critical component changes require documentation_required=true",
                    )
                if not changed_map_files or not (changed_map_files & seen_targets):
                    self._issue(
                        issues, "map_documentation_change", "manifest.json",
                        "critical component changes must include a declared Map file in the candidate",
                    )
            elif affected and not map_impact:
                warnings.append({
                    "code": "map_impact_recommended", "path": "manifest.json",
                    "message": "registered non-critical components changed without map_impact",
                })
            status = "failed" if issues else "passed"
            summary = (
                f"Validated {len(checked_files)} candidate files; "
                f"{len(issues)} issue(s) found."
            )

        targets: list[dict[str, Any]] = [{
            "target_id": "candidate:manifest-and-files",
            "scope": "structure",
            "required": True,
            "status": "failed" if issues else "passed",
            "evidence": [item["path"] for item in checked_files],
        }]
        checked_paths = [str(item["path"]) for item in checked_files]
        python_paths = [path for path in checked_paths if path.lower().endswith(".py")]
        contract_paths = [
            path for path in checked_paths
            if path.lower().endswith("/skill.md")
            or path.lower().endswith("/config.yaml")
            or path.lower().endswith("/agent-config.yaml")
            or path.lower().endswith("/mcp-manifest.yaml")
            or path.lower().endswith("/project.yaml")
            or path.lower().endswith("/source-map.yaml")
            or path.lower().endswith("/knowledge/map/index.yaml")
            or "/schedules/" in f"/{path.lower()}"
        ]
        if python_paths:
            targets.append({
                "target_id": "kernel:python-syntax",
                "scope": "component",
                "required": True,
                "status": "failed" if any(
                    item["code"] == "python_syntax" for item in issues
                ) else "passed",
                "evidence": python_paths,
            })
        if contract_paths:
            contract_issue_prefixes = (
                "skill_", "agent_", "mcp_", "project_", "schedule_", "source_map_",
                "semantic_map_",
            )
            targets.append({
                "target_id": "kernel:resource-contracts",
                "scope": "contract",
                "required": True,
                "status": "failed" if any(
                    str(item["code"]).startswith(contract_issue_prefixes)
                    for item in issues
                ) else "passed",
                "evidence": contract_paths,
            })
        report_root = runtime.vault_root / "runs" / implementation_run_id / "validation"
        report_root.mkdir(parents=True, exist_ok=True)
        policy = dict(validation_policy or {})
        if not sealed_problem:
            targets.extend(self._run_python_unittest_profiles(
                project_id=project_id, run_id=implementation_run_id,
                candidate_root=candidate_root, policy=policy,
                report_root=report_root, issues=issues,
                deleted_by_root=deleted_by_root,
            ))
        deferred_targets: list[dict[str, Any]] = []
        unresolved_required: list[str] = []
        required_scopes = policy.get("required_scopes") or ["structure"]
        if not isinstance(required_scopes, list):
            self._issue(
                issues, "validation_policy_type", "kernel-policy",
                "required_scopes must be a list",
            )
            required_scopes = ["structure"]
        for scope in {str(item) for item in required_scopes}:
            if scope not in VALIDATION_SCOPES:
                self._issue(
                    issues, "validation_policy_scope", "kernel-policy",
                    f"unsupported required validation scope: {scope}",
                )
                continue
            if not any(
                target["scope"] == scope and target["status"] == "passed"
                for target in targets
            ):
                target_id = f"kernel-policy:{scope}"
                unresolved_required.append(target_id)
                targets.append({
                    "target_id": target_id,
                    "scope": scope,
                    "required": True,
                    "status": "not_run",
                    "evidence": [],
                })
        declared_plan = manifest.get("validation_plan") or []
        if not isinstance(declared_plan, list):
            self._issue(
                issues, "validation_plan_type", "manifest.json",
                "validation_plan must be a list",
            )
            declared_plan = []
        for raw in declared_plan:
            if not isinstance(raw, dict):
                self._issue(
                    issues, "validation_target_type", "manifest.json",
                    "every validation target must be an object",
                )
                continue
            target_id = str(raw.get("target_id") or "").strip()
            scope = str(raw.get("scope") or "").strip()
            required = bool(raw.get("required", True))
            deferred = bool(raw.get("deferred", False))
            if not target_id or scope not in VALIDATION_SCOPES:
                self._issue(
                    issues, "validation_target", "manifest.json",
                    "validation target needs target_id and a supported scope",
                )
                continue
            if any(item["target_id"] == target_id for item in targets):
                self._issue(
                    issues, "validation_target_duplicate", "manifest.json",
                    f"candidate validation target conflicts with a Kernel target: {target_id}",
                )
                continue
            if deferred:
                reason = str(raw.get("reason") or "").strip()
                acceptance_run_ref = str(raw.get("acceptance_run_ref") or "").strip()
                if not reason or not acceptance_run_ref:
                    self._issue(
                        issues, "validation_deferred_evidence", "manifest.json",
                        "deferred validation needs reason and acceptance_run_ref",
                    )
                deferred_targets.append({
                    "target_id": target_id,
                    "scope": scope,
                    "required": required,
                    "reason": reason,
                    "acceptance_run_ref": acceptance_run_ref,
                    "deadline": raw.get("deadline"),
                })
                if required:
                    unresolved_required.append(target_id)
                continue
            if scope == "structure":
                targets.append({
                    "target_id": target_id,
                    "scope": scope,
                    "required": required,
                    "status": "failed" if issues else "passed",
                    "evidence": [item["path"] for item in checked_files],
                })
            elif required:
                unresolved_required.append(target_id)
                targets.append({
                    "target_id": target_id,
                    "scope": scope,
                    "required": True,
                    "status": "not_run",
                    "evidence": [],
                })
            else:
                targets.append({
                    "target_id": target_id,
                    "scope": scope,
                    "required": False,
                    "status": "not_run",
                    "evidence": [],
                })

        if issues:
            release_gate = "fail"
            summary = (
                f"Validated {len(checked_files)} candidate files; "
                f"{len(issues)} issue(s) found."
            )
        elif unresolved_required:
            release_gate = "incomplete"
            summary += " Required validation targets were not run: " + ", ".join(
                unresolved_required
            )
        else:
            release_gate = "pass"
        status = "passed" if release_gate == "pass" else "failed"

        report = {
            "schema_version": "validation-report.v2",
            "project_id": project_id,
            "implementation_run_id": implementation_run_id,
            "status": status,
            "release_gate": release_gate,
            "summary": summary,
            "checked_files": checked_files,
            "targets": targets,
            "deferred_targets": deferred_targets,
            "unresolved_required_targets": unresolved_required,
            "residual_risks": [
                f"Deferred validation: {item['target_id']} ({item['scope']})"
                for item in deferred_targets
            ],
            "issues": issues,
            "warnings": warnings,
        }
        report_path = report_root / "report.json"
        encoded = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        temporary = report_path.with_suffix(".tmp")
        temporary.write_bytes(encoded)
        temporary.replace(report_path)
        return {
            "uri": (
                f"vault://{project_id}/"
                f"{report_path.relative_to(runtime.vault_root).as_posix()}"
            ),
            "sha256": self._sha256(encoded),
            "kind": "validation_report",
            "required": True,
            "producer_run_id": implementation_run_id,
            "producer_stage": "validation",
            "status": status,
            "release_gate": release_gate,
            "summary": summary,
            "issues": issues,
        }
