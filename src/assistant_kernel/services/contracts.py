"""Pinned server-owned v1.2 contracts. No network schema resolution or model selectors."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def strict_json(encoded: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result
    def invalid_constant(_value):
        raise ValueError("non-finite JSON number")
    return json.loads(encoded, object_pairs_hook=pairs, parse_constant=invalid_constant)


class ContractError(ValueError):
    pass


class ContractRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[1] / "contracts"
        manifest = strict_json((self.root / "manifest.json").read_bytes())
        self.manifest = manifest
        for name, expected in manifest["files"].items():
            if Path(name).name != name:
                raise ContractError("invalid contract manifest path")
            if hashlib.sha256((self.root / name).read_bytes()).hexdigest() != expected:
                raise ContractError(f"installed contract hash differs: {name}")
        encoded = (self.root / "schema-library-v12.json").read_bytes()
        self.sha256 = hashlib.sha256(encoded).hexdigest()
        self.library = strict_json(encoded)
        self.registry = strict_json((self.root / "registry-v12.json").read_bytes())
        self.protocol_revision = self.registry["protocol_revision"]
        self.implementation_revision = self.registry["implementation_revision"]
        Draft202012Validator.check_schema(self.library)
        self._check_local_refs(self.library)

    def verify_bundle_copy(self, target: Path) -> None:
        if strict_json((target / "manifest.json").read_bytes()) != self.manifest:
            raise ContractError("immutable reader manifest differs")
        for name, digest in self.manifest["files"].items():
            if hashlib.sha256((target / name).read_bytes()).hexdigest() != digest:
                raise ContractError("immutable reader bundle hash differs")

    def _check_local_refs(self, item: Any) -> None:
        if isinstance(item, dict):
            if "$ref" in item:
                value = item["$ref"]
                if not value.startswith("#/$defs/") or value[8:] not in self.library["$defs"]:
                    raise ContractError("contract reference is not local and registered")
            for value in item.values():
                self._check_local_refs(value)
        elif isinstance(item, list):
            for value in item:
                self._check_local_refs(value)

    def library_ref(self) -> dict[str, Any]:
        return {
            "schema_version": "resource-ref.v1", "kind": "source",
            "id": "kernel-contract-library-v12", "document_schema": None,
            "revision_id": f"v12-implementation-{self.implementation_revision}", "sha256": self.sha256,
            "uri": f"kernel://contracts/{self.sha256}/schema-library.json",
            "purpose": "server-selected immutable implementation contracts",
        }

    @lru_cache(maxsize=128)
    def _validator(self, definition: str):
        if definition not in self.library["$defs"]:
            raise ContractError("unknown contract definition")
        return Draft202012Validator(
            {"$defs": self.library["$defs"], "$ref": "#/$defs/" + definition},
            format_checker=FormatChecker(),
        )

    def validate(self, definition: str, value: Any) -> None:
        # Errors intentionally do not echo values, which can include accidental secrets.
        errors = sorted(self._validator(definition).iter_errors(value),
                        key=lambda e: "/".join(map(str, e.absolute_path)))
        if errors:
            error = errors[0]
            path = "/" + "/".join(map(str, error.absolute_path))
            detail = ''
            if error.validator == 'required' and isinstance(error.instance, dict):
                missing = [key for key in error.validator_value if key not in error.instance]
                detail = '; missing fields: ' + ', '.join(missing)
            raise ContractError(f"{definition}{path}: violates {error.validator}{detail}")

    def document_definition(self, schema: str) -> str:
        try:
            return self.registry["document_selectors"][schema]
        except KeyError:
            raise ContractError("document Schema has no active writer") from None

    def definition_closure(self,*roots):
        """Provide the exact server definitions to configured model producers."""
        selected={};pending=list(roots)
        def visit(value):
            if isinstance(value,dict):
                if '$ref' in value:pending.append(value['$ref'].split('/')[-1])
                for child in value.values():visit(child)
            elif isinstance(value,list):
                for child in value:visit(child)
        while pending:
            name=pending.pop()
            if name in selected:continue
            if name not in self.library['$defs']:raise ContractError('Producer definition has no installed schema')
            selected[name]=self.library['$defs'][name];visit(selected[name])
        return {'$defs':selected}

    def agent_result_schema(self, phase: str) -> dict[str, Any]:
        """Model-facing projection of the installed envelope and bound role only."""
        from copy import deepcopy
        if phase not in {'supervisor', 'requirements', 'design', 'build', 'audit', 'operate'}:
            raise ContractError('Producer phase is not registered')
        schema = deepcopy(self.library['$defs']['V12_AgentResult'])
        if phase == 'supervisor':
            schema['properties']['payload'] = {'$ref': '#/$defs/Active_SupervisorPayload'}
            # Match the existing decoder: concrete Prompt/document confirmation
            # is prepared by Kernel, not a second model-written approval route.
            schema['properties']['approval_requests']['maxItems'] = 0
        else:
            # Only the bound phase's report belongs in this invocation. Keep
            # shared payload rules and its active refinements, not five copies.
            payload = deepcopy(self.library['$defs']['V12_PMPayload'])
            refinements = deepcopy(self.library['$defs']['V12_Active_PMPayload']['allOf'][1:])
            branches = payload['allOf'] + refinements
            payload['allOf'] = [item for item in branches if 'if' not in item or
                item['if']['properties']['stage']['const'] == phase]
            payload['properties']['stage'] = {'const': phase}
            schema['properties']['payload'] = payload
        schema['required'].append('protocol_revision')
        roots = set()
        def visit(value):
            if isinstance(value, dict):
                if '$ref' in value: roots.add(value['$ref'].split('/')[-1])
                for child in value.values(): visit(child)
            elif isinstance(value, list):
                for child in value: visit(child)
        visit(schema)
        schema.update(self.definition_closure(*roots))
        return schema

    def validate_document(self, document: dict[str, Any]) -> None:
        expected = self.document_definition(document.get("schema_version", ""))
        if document.get("payload_definition") != expected:
            raise ContractError("document selector differs from the server registry")
        if document.get("schema_library_ref") != self.library_ref():
            raise ContractError("document contract library is not the pinned installed revision")
        self.validate("V12_Document", document)
        self.validate(expected, document["payload"])
        actual = hashlib.sha256(canonical_json(document["payload"])).hexdigest()
        if actual != document["content_sha256"]:
            raise ContractError("document payload hash differs")

    def validate_agent_result(self, value: dict[str, Any], *, expected_phase: str) -> None:
        self.validate(self.registry["write_selectors"]["AgentResult"], value)
        if value["payload"]["stage"] != expected_phase:
            raise ContractError("output phase differs from the Kernel-bound phase")

    def preserve(self, data_root: Path) -> Path:
        """Keep the exact reader bundle before publishing a document using it.

        Archived bundles are readers only. Writers always use contracts(), the
        current installed server registry, never a model-selected old selector.
        """
        import fcntl
        archive = data_root / "runtime/contract-libraries"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / self.sha256
        with (archive / "writer.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if target.exists():
                self.verify_bundle_copy(target)
                return target
            staging = archive / (".prepare-" + uuid.uuid4().hex)
            staging.mkdir()
            for name in [*self.manifest["files"], "manifest.json"]:
                if Path(name).name != name:
                    raise ContractError("invalid reader bundle filename")
                (staging / name).write_bytes((self.root / name).read_bytes())
            self.verify_bundle_copy(staging)
            staging.rename(target)
        return target


def document_reader(data_root: Path, library_ref: dict[str, Any]) -> ContractRegistry:
    current = contracts()
    if library_ref == current.library_ref():
        return current
    digest = library_ref.get("sha256", "") if isinstance(library_ref, dict) else ""
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ContractError("document reader digest is invalid")
    path = data_root / "runtime/contract-libraries" / digest
    if not path.is_dir():
        raise ContractError("pinned historical reader is unavailable")
    historical = ContractRegistry(path)
    if historical.sha256 != digest or historical.library_ref() != library_ref:
        raise ContractError("historical reader identity differs")
    return historical


@lru_cache(maxsize=1)
def contracts() -> ContractRegistry:
    return ContractRegistry()
