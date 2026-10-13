"""Strict global parameter validation; these values never grant business authority."""
from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant_kernel.services.contracts import ContractError, contracts, strict_json


PUBLIC_GROUPS = frozenset({
    "system", "resource_limits", "python_defaults", "agent_defaults",
    "service_defaults", "map",
})


def pointer_parts(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("configuration path must be a JSON Pointer")
    parts = path[1:].split("/")
    if any(not p or "~" in p.replace("~1", "").replace("~0", "") for p in parts):
        raise ValueError("invalid configuration JSON Pointer")
    return [p.replace("~1", "/").replace("~0", "~") for p in parts]


def pointer_value(value: dict[str, Any], path: str) -> Any:
    item: Any = value
    for part in pointer_parts(path):
        item = item[int(part)] if isinstance(item, list) else item[part]
    return deepcopy(item)


def field_schema(path: str) -> dict[str, Any]:
    item = contracts().library["$defs"]["SystemConfig"]
    for part in pointer_parts(path):
        if part in item.get("properties", {}):
            item = item["properties"][part]
        elif isinstance(item.get("additionalProperties"), dict):
            import re
            if not re.fullmatch(item.get("propertyNames", {}).get("pattern", ".+"), part):
                raise ValueError("invalid configuration object key")
            item = item["additionalProperties"]
        else:
            raise ValueError(f"configuration path is not a published field: {path}")
    return item


def changed_config(current: dict[str, Any], changes: list[dict[str, Any]]) -> dict[str, Any]:
    if not changes:
        raise ValueError("configuration changes must not be empty")
    result = deepcopy(current)
    seen = set()
    for change in changes:
        if set(change) != {"path", "value"}:
            raise ValueError("configuration change requires only path and value")
        path = change["path"]
        field_schema(path)
        if path in seen or any(path.startswith(p + "/") or p.startswith(path + "/") for p in seen):
            raise ValueError("configuration changes overlap")
        seen.add(path)
        parts = pointer_parts(path)
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("replace an array as one published configuration field")
        node[parts[-1]] = deepcopy(change["value"])
    return result


def apply_mode(path: str) -> str:
    spec = field_schema(path)
    mode = spec.get("x-apply-mode")
    if mode == "schema_identity":
        return "next_invocation"
    if mode:
        return mode
    return "next_invocation" if path.startswith("/providers/") else "pending_restart"


def leaf_values(value: Any, path: str = "") -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        return {path: value}
    result = {}
    for key, item in value.items():
        encoded = key.replace("~", "~0").replace("/", "~1")
        result.update(leaf_values(item, path + "/" + encoded))
    return result


def validate_system_config(candidate: dict[str, Any]) -> dict[str, Any]:
    issues = []
    try:
        contracts().validate("SystemConfig", candidate)
    except (ContractError, TypeError, ValueError) as exc:
        return {"valid": False, "runtime_readiness": "unverified", "issues": [str(exc)],
                "required_apply_modes": [], "affected_refs": []}
    # Relations are checked separately from Schema. Neither check claims runtime adoption.
    for path in ["/system/timezone"]:
        try:
            ZoneInfo(pointer_value(candidate, path))
        except (ZoneInfoNotFoundError, ValueError):
            issues.append(path + ": requires an IANA timezone")
    for path in ["/paths/workspace_root", "/paths/python_executable"]:
        p = PurePosixPath(pointer_value(candidate, path))
        if not p.is_absolute() or ".." in p.parts:
            issues.append(path + ": requires a normalized Linux absolute path")
    for path in ["/paths/data_directory", "/backups/directory"]:
        p = PurePosixPath(pointer_value(candidate, path))
        if p.is_absolute() or ".." in p.parts or "\\" in str(p):
            issues.append(path + ": requires a relative deployment path")
    agent = candidate["agent_defaults"]
    if agent["role_soft_limit_tokens"] > agent["role_hard_limit_tokens"]:
        issues.append("/agent_defaults: role soft limit exceeds hard limit")
    if (agent["context_policy"] == "custom" and agent["native_auto_compact_token_limit"] is not None
            and agent["native_auto_compact_token_limit"] >= agent["custom_context_window_tokens"]):
        issues.append("/agent_defaults: native compact threshold must be below the custom window")
    py = candidate["python_defaults"]
    if py["timeout_seconds"] > py["max_timeout_seconds"]:
        issues.append("/python_defaults: timeout exceeds the maximum")
    if py["cpu_pool_size"] * py["numeric_threads"] > candidate["resource_limits"]["max_cpu_slots"]:
        issues.append("/python_defaults: default pool and library threads exceed CPU slots")
    service = candidate["service_defaults"]
    if service["heartbeat_seconds"] >= service["stale_after_seconds"]:
        issues.append("/service_defaults: stale interval must exceed heartbeat interval")
    if candidate["kernel"]["health"]["heartbeat_seconds"] >= candidate["kernel"]["health"]["stale_after_seconds"]:
        issues.append("/kernel/health: stale interval must exceed heartbeat interval")
    if service["recovery"]["max_restarts"] > 0 and not service["recovery"]["backoff_seconds"]:
        issues.append("/service_defaults/recovery: enabled retries require a bounded backoff")
    notify = candidate["notifications"]
    if notify["retry_base_seconds"] > notify["retry_max_seconds"]:
        issues.append("/notifications: retry base exceeds maximum")
    for name, storage in candidate["infrastructure"]["storage"].items():
        path = PurePosixPath(storage["source"])
        if storage["type"] == "bind" and (path.is_absolute() or ".." in path.parts or "\\" in str(path)):
            issues.append(f"/infrastructure/storage/{name}/source: requires a relative bind source")
        if not PurePosixPath(storage["target"]).is_absolute():
            issues.append(f"/infrastructure/storage/{name}/target: requires a Linux container path")
    modes = sorted({apply_mode(p) for p in leaf_values(candidate) if p not in {"/$schema", "/schema_version"}})
    return {"valid": not issues, "runtime_readiness": "unverified", "issues": issues,
            "required_apply_modes": modes, "affected_refs": []}


def parse_system_config(encoded: bytes | str) -> dict[str, Any]:
    result = strict_json(encoded)
    validation = validate_system_config(result)
    if not validation["valid"]:
        raise ValueError("; ".join(validation["issues"]))
    return result


def resolve_config_bindings(
    config: dict[str, Any], source_ref: dict[str, Any], bindings: list[dict[str, Any]],
    *, owner_ref: dict[str, Any], captured_at: str, effective_group_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    from assistant_kernel.services.contracts import canonical_json
    import hashlib
    selected = {}; resolved = []
    for item in bindings:
        if set(item) - {"path", "mode", "override_value"}:
            raise ValueError("binding contains caller-supplied resolved state")
        path = item["path"]; spec = field_schema(path)
        if path in selected:
            raise ValueError("duplicate configuration binding")
        mode = item["mode"]
        if mode == "inherit":
            if item.get("override_value") is not None:
                raise ValueError("inherited binding cannot carry an override")
            value = pointer_value(config, path)
        elif mode == "override":
            if not spec.get("x-inheritable", False):
                raise ValueError("global-only configuration cannot be overridden")
            if "override_value" not in item:
                raise ValueError("explicit binding requires its value")
            value = deepcopy(item["override_value"])
            from jsonschema import Draft202012Validator, FormatChecker
            if not Draft202012Validator(spec, format_checker=FormatChecker()).is_valid(value):
                raise ValueError("override violates the published field contract")
        else:
            raise ValueError("configuration binding must be inherit or override")
        selected[path] = value
        resolved.append({"path":path,"mode":mode,"override_value":value if mode=='override' else None,
                         "resolved_value":value,"source_config_ref":source_ref,"resolved_config_ref":None})
    overrides = [{"path":b["path"],"value":b["resolved_value"]} for b in resolved if b["mode"] == "override"]
    combined = changed_config(config, overrides) if overrides else config
    validation = validate_system_config(combined)
    if not validation["valid"]:
        raise ValueError("resolved overrides violate configuration relationships")
    return {"owner_ref":owner_ref,"desired_config_ref":source_ref,
            "effective_group_refs":effective_group_refs,"bindings":resolved,"selected_values":selected,
            "subset_sha256":hashlib.sha256(canonical_json(selected)).hexdigest(),
            "captured_at":captured_at,"status":"resolved"}
