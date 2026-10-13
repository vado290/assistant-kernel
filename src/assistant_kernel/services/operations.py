"""Dispatch approved, hash-bound runbook scripts through existing Python Runs."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.repositories.core import approval_decision, canonical_sha256
from assistant_kernel.services.document_runtime import TaskDocumentService
from assistant_kernel.services.task_queue import ProjectTaskQueue
from assistant_kernel.services.workspace_scope import WorkspaceScopeService
from assistant_kernel.storage import ProjectRuntimeManager


def _action_material(spec: dict[str, Any]) -> dict[str, Any]:
    """Publication may hydrate only the source hash, never the approved behavior."""
    copied = json.loads(json.dumps(spec))
    if isinstance(copied.get("script_ref"), dict):
        copied["script_ref"].pop("sha256", None)
    return copied


def validate_action_inputs(value: Any, schema: dict[str, Any], *, depth: int = 0) -> None:
    if not isinstance(schema, dict):
        raise ValueError("action input schema must be an object")
    supported = {"type", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern",
                 "minItems", "maxItems", "items", "properties", "required", "additionalProperties",
                 "description", "title", "$schema"}
    if set(schema) - supported:
        raise ValueError("action input schema uses unsupported constraints; revise the approved contract")
    if depth > 5:
        raise ValueError("action input nesting exceeds the approved data contract")
    kind = schema.get("type", "object")
    types = {"object": dict, "array": list, "string": str, "integer": int,
             "number": (int, float), "boolean": bool, "null": type(None)}
    if kind not in types or not isinstance(value, types[kind]) or (
        kind in {"integer", "number"} and isinstance(value, bool)
    ):
        raise ValueError(f"action input must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("action input is outside approved choices")
    if kind in {"integer", "number"}:
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            raise ValueError("action input is outside its approved numeric range")
    elif kind == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 2048):
            raise ValueError("action input is outside its approved length")
        if "pattern" in schema and not re.fullmatch(str(schema["pattern"]), value):
            raise ValueError("action input does not match the approved pattern")
    elif kind == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 64):
            raise ValueError("action input is outside its approved item count")
        if not isinstance(schema.get("items"), dict):
            raise ValueError("array action input requires an approved item schema")
        for item in value:
            validate_action_inputs(item, schema["items"], depth=depth + 1)
    elif kind == "object":
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict) or schema.get("additionalProperties", False) is not False:
            raise ValueError("action inputs require a closed object schema")
        if set(value) - set(properties) or set(schema.get("required") or []) - set(value):
            raise ValueError("action inputs contain unknown or missing approved fields")
        for key, item in value.items():
            validate_action_inputs(item, properties[key], depth=depth + 1)


class ProjectOperations:
    def __init__(self, settings: KernelSettings, repository: CoreRepository | None = None,
                 queue: ProjectTaskQueue | None = None):
        self.settings = settings
        self.repository = repository or CoreRepository(settings)
        self.queue = queue or ProjectTaskQueue(settings, self.repository)
        self.runtime = ProjectRuntimeManager(settings.data_root)
        self.documents = TaskDocumentService(settings)
        self.scopes = WorkspaceScopeService(settings)

    async def _baseline(self, project_id: str) -> dict[str, Any]:
        async with self.repository.database.connection() as connection:
            row = await (await connection.execute(
                "SELECT value FROM assistant.kernel_meta WHERE key=%s",
                (f"project-runtime:{project_id}",))).fetchone()
        baseline = ((row or {}).get("value") or {}).get("operational_baseline_ref")
        if isinstance(baseline,dict) and baseline.get('document_schema')=='operational-baseline.v1':
            from assistant_kernel.services.document_runtime import same_resource_ref
            payload=self.documents.read(project_id,baseline)['payload'];creator=payload['creating_task_ref']
            task=await self.repository.get_run(creator['id'])
            state=(task or {}).get('request_json') or {};accepted=state.get('accepted_documents') or {}
            if (not task or task['project_id']!=project_id or task['status']!='succeeded'
                    or not same_resource_ref(state.get('operational_baseline_ref'),baseline)
                    or not same_resource_ref(state.get('acceptance_record_ref'),payload['acceptance_record_ref'])
                    or not same_resource_ref(accepted.get('activation'),payload['activation_ref'])
                    or not same_resource_ref(accepted.get('task_contract'),payload['task_contract_ref'])
                    or not same_resource_ref(accepted.get('solution_design'),payload['solution_design_ref'])):
                raise PermissionError('Operational baseline is not the actually accepted completed v1.2 Task')
            return {**payload,'schema_version':'operational-baseline.v1','task_id':str(task['run_id']),
                'design_approval':state.get('design_approval'),'requirements_approval':state.get('requirements_approval'),
                'v12_baseline_ref':baseline}
        if not isinstance(baseline, dict) or baseline.get("schema_version") != "operational-baseline.v1":
            raise PermissionError("Project has no published operational baseline")
        if not baseline.get("task_id") or not baseline.get("publication_run_id") or not baseline.get("publication_receipt"):
            raise PermissionError("operational baseline is missing its publication evidence")
        publication = await self.repository.get_run(str(baseline["publication_run_id"]))
        if (publication is None or str(publication["project_id"]) != project_id
                or publication.get("run_type") != "publish" or publication.get("status") != "succeeded"
                or str(publication.get("parent_run_id")) != str(baseline["task_id"])):
            raise PermissionError("operational baseline publication is not verified")
        return baseline

    async def _approval(self, project_id: str, approval: Any, ref: dict[str, Any]) -> None:
        if not isinstance(approval, dict) or approval.get("approved_document_sha256") != ref.get("sha256"):
            raise PermissionError("approved document does not match the effective revision")
        message_id = approval.get("message_id") or approval.get("approval_message_id")
        message = await self.repository.get_message(str(message_id or ""))
        if (message is None or str(message["project_id"]) != project_id or message.get("direction") != "inbound"
                or approval_decision(dict(message.get("content_json") or {})) != "approve"):
            raise PermissionError("actual user approval for this document is missing")

    async def _context(self, project_id: str, task_id: str, action_id: str,
                       inputs: dict[str, Any]) -> dict[str, Any]:
        task = await self.repository.get_run(task_id)
        if (task is None or str(task["project_id"]) != project_id
                or task.get("agent_key") != f"project:{project_id}:manager" or task.get("run_type") != "chat"):
            raise PermissionError("operation source is not the current Project PM Task")
        request = dict(task.get("request_json") or {})
        if (request.get("work_mode") or request.get("current_stage")) != "operate":
            raise PermissionError("runbook dispatch is restricted to Operation phase")
        if request.get("task_activation", "active") != "active" or task.get("cancel_requested_at"):
            raise PermissionError("operation source Task is suspended or cancelled")
        baseline = await self._baseline(project_id)
        baseline_task = await self.repository.get_run(str(baseline["task_id"]))
        if baseline_task is None or str(baseline_task["project_id"]) != project_id:
            raise PermissionError("baseline source Task is missing")
        baseline_request = dict(baseline_task.get("request_json") or {})
        accepted = dict(baseline_request.get("accepted_documents") or {})
        contract_ref = dict(baseline.get("task_contract_ref") or {})
        design_ref = dict(baseline.get("solution_design_ref") or {})
        if accepted.get("task_contract") != contract_ref or (accepted.get("solution_design") or accepted.get("design")) != design_ref:
            raise PermissionError("baseline documents differ from the source Task's accepted revisions")
        self.documents.read(project_id, contract_ref)
        design = self.documents.read(project_id, design_ref)
        if (design.get("payload") or {}).get("task_contract_sha256") != contract_ref.get("sha256"):
            raise PermissionError("approved design is bound to another requirements revision")
        await self._approval(project_id, baseline_request.get("requirements_approval"), contract_ref)
        if baseline.get("design_approval") != baseline_request.get("design_approval"):
            raise PermissionError("baseline design approval differs from the source Task")
        await self._approval(project_id, baseline.get("design_approval"), design_ref)
        original_matches = [item for item in (design.get("payload") or {}).get("runbook_actions") or []
                            if isinstance(item, dict) and item.get("action_id") == action_id]
        hydrated_matches = [item for item in baseline.get("runbook_actions") or []
                            if isinstance(item, dict) and item.get("action_id") == action_id]
        original = original_matches[0] if len(original_matches) == 1 else None
        hydrated = hydrated_matches[0] if len(hydrated_matches) == 1 else None
        if original is None or hydrated is None or _action_material(original) != _action_material(hydrated):
            raise PermissionError("action is absent from or differs from the approved runbook")
        spec = dict(hydrated)
        if spec.get("risk") not in {"low", "high"}:
            raise PermissionError("action has no explicit approved risk class")
        if baseline.get('v12_baseline_ref'):
            from assistant_kernel.services.project_python import ProjectPython
            from assistant_kernel.services.document_runtime import same_resource_ref
            service=ProjectPython(self.settings,self.repository);manifest_ref=spec['script_manifest_ref']
            manifest=service.fixed.read_manifest(project_id,manifest_ref)
            async with self.repository.database.connection() as c:
                units=await (await c.execute('SELECT effective_definition_ref FROM assistant.project_runtime_units WHERE project_id=%s',(project_id,))).fetchall()
            matches=[x['effective_definition_ref'] for x in units if same_resource_ref(
                service.units.read_definition(project_id,x['effective_definition_ref'])['implementation_manifest_ref'],manifest_ref)]
            if len(matches)!=1:raise PermissionError('Runbook manifest needs one exact registered RuntimeUnit')
            await service.units.finite_gate(project_id,matches[0],manifest_ref,manifest['purpose']['authorization_ref'])
            from assistant_kernel.services.fixed_python import data_schema
            data_schema(inputs,spec['input_schema'])
            data_schema(inputs,manifest['io']['input_schema'])
            if set(spec['expected_effects'])!=set(manifest['expected_effects']):raise PermissionError('Runbook effect scope differs from its fixed programme')
            entry=manifest['code_bundle']['entrypoint_ref'];service.fixed.source_path(project_id,entry,self.scopes.resolve(project_id,task_id,'operate'))
            spec.update(fixed_args=manifest['fixed_args'],timeout_seconds=manifest['resources']['timeout_seconds'],shared_write=manifest['shared_write'])
            return {'task':task,'baseline':baseline,'spec':spec,'source_uri':entry['uri'],'source_sha256':entry['sha256'],
                'action_id':action_id,'action_definition_sha256':canonical_sha256(spec),'baseline_sha256':canonical_sha256(baseline),
                'input_sha256':canonical_sha256(inputs),'manifest_ref':manifest_ref,'runtime_unit_ref':matches[0]}
        script_ref = dict(spec.get("script_ref") or {})
        uri = str(script_ref.get("uri") or "")
        prefix = f"vault://{project_id}/"
        if not uri.startswith(prefix) or not script_ref.get("sha256"):
            raise PermissionError("action script is not bound to this Project Vault and a published SHA")
        target = self.runtime.resolve_vault_file(project_id, uri[len(prefix):])
        if target.suffix.lower() != ".py" or not target.is_file():
            raise PermissionError("action script is not a published Python file")
        import hashlib
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        approved_source_sha = (original.get("script_ref") or {}).get("sha256")
        if actual_sha != script_ref["sha256"] or approved_source_sha and approved_source_sha != actual_sha:
            raise PermissionError("action script SHA differs from the published or approved source")
        roots = self.scopes.resolve(project_id, task_id, "operate")
        self.scopes.resolve_uri(roots, f"workspace://project/{uri[len(prefix):]}")
        schema = spec.get("input_schema") or {"type": "object", "properties": {}, "additionalProperties": False}
        validate_action_inputs(inputs, schema)
        if len(json.dumps(inputs, allow_nan=False).encode()) > 65536:
            raise ValueError("action inputs exceed 65536 bytes")
        fixed_args = spec.get("fixed_args") or []
        if not isinstance(fixed_args, list) or any(not isinstance(item, str) for item in fixed_args):
            raise ValueError("runbook fixed_args must be a fixed string list")
        shared = spec.get("shared_write", False)
        timeout = spec.get("timeout_seconds", 300)
        if not isinstance(shared, bool) or not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ValueError("runbook execution limits are invalid")
        return {"task": task, "baseline": baseline, "spec": spec, "source_uri": uri,
                "source_sha256": actual_sha, "action_id": action_id,
                "action_definition_sha256": canonical_sha256(spec),
                "baseline_sha256": canonical_sha256(baseline), "input_sha256": canonical_sha256(inputs)}

    async def prepare_approval(self, project_id: str, task_id: str, action_id: str,
                               inputs: dict[str, Any]) -> dict[str, Any]:
        context = await self._context(project_id, task_id, action_id, inputs)
        result = {key: context[key] for key in ("action_id", "action_definition_sha256", "baseline_sha256", "input_sha256")}
        result["document_markdown"] = "\n".join((
            f"# Approve runbook action: {action_id}", "", f"Project: {project_id}",
            f"Task: {task_id}", f"Risk: {context['spec']['risk']}",
            f"Description: {context['spec'].get('description') or context['spec'].get('purpose') or action_id}",
            f"Expected effects: {json.dumps(context['spec'].get('expected_effects') or [], ensure_ascii=False)}",
            f"Script: {context['source_uri']}", f"Source SHA256: {context['source_sha256']}",
            f"Fixed arguments: {json.dumps(context['spec'].get('fixed_args') or [], ensure_ascii=False)}",
            f"Timeout seconds: {context['spec'].get('timeout_seconds', 300)}",
            f"Shared write: {context['spec'].get('shared_write', False)}", "", "Inputs:",
            "```json", json.dumps(inputs, ensure_ascii=False, sort_keys=True, indent=2), "```", "",
            *[f"{key}: {result[key]}" for key in ("action_definition_sha256", "baseline_sha256", "input_sha256")],
            "", "Approval authorizes one dispatch of this action on this baseline with these exact inputs."))
        return result

    async def _action_approval(self, project_id: str, context: dict[str, Any]) -> None:
        if context["spec"]["risk"] == "low":
            return
        task = context["task"]
        approved = ((task.get("request_json") or {}).get("approved_actions") or {}).get(context["action_id"])
        hashes = ("action_definition_sha256", "baseline_sha256", "input_sha256")
        if (not isinstance(approved, dict) or approved.get("revoked_at")
                or approved.get("status") in {"revoked", "superseded", "cancelled"}
                or any(approved.get(key) != context[key] for key in hashes)):
            raise PermissionError("high-risk action requires approval of this exact action, baseline and inputs")
        message = await self.repository.get_message(str(approved.get("approval_message_id") or ""))
        if (message is None or str(message["project_id"]) != project_id or message.get("direction") != "inbound"
                or str(message.get("run_id")) != str(task["run_id"])
                or approval_decision(dict(message.get("content_json") or {})) != "approve"):
            raise PermissionError("high-risk action has no actual user approval")
        question = await self.repository.get_message(str(message.get("reply_to_message_id") or ""))
        prepared = ((question or {}).get("content_json") or {}).get("operation_approval_request") or {}
        if (question is None or str(question["project_id"]) != project_id or question.get("direction") != "outbound"
                or str(question.get("run_id")) != str(task["run_id"])
                or question.get("message_type") != "approval" or prepared.get("action_id") != context["action_id"]
                or any(prepared.get(key) != context[key] for key in hashes)):
            raise PermissionError("high-risk approval card does not match this dispatch")

    async def run_action(self, project_id: str, task_id: str, action_id: str,
                         inputs: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise ValueError("action idempotency key is required and must fit 256 characters")
        context = await self._context(project_id, task_id, action_id, inputs)
        await self._action_approval(project_id, context)
        binding = {key: context[key] for key in ("action_id", "action_definition_sha256", "baseline_sha256", "input_sha256")}
        binding["task_id"] = task_id
        dispatch_key = "episode:" + canonical_sha256({
            "task_id": task_id, "action_id": action_id,
            **{key: context[key] for key in ("action_definition_sha256", "baseline_sha256", "input_sha256")},
        })
        if context["spec"]["risk"] == "high":
            grant = (context["task"]["request_json"]["approved_actions"])[action_id]
            dispatch_key = f"approval:{grant['approval_message_id']}"
        if context.get('manifest_ref'):
            from assistant_kernel.services.project_python import ProjectPython
            receipt=await ProjectPython(self.settings,self.repository).submit(project_id,{
                'manifest_ref':context['manifest_ref'],'runtime_unit_ref':context['runtime_unit_ref'],'inputs':inputs,
                'execution_kind':'formal','logical_execution_id':None,'resume_owner':None,
                'idempotency_key':'operation-'+canonical_sha256([task_id,action_id,dispatch_key])},operation_binding=binding)
            return await self.repository.get_run(receipt['run_ref']['id'])
        return await self.repository.create_run(
            project_id=project_id, agent_key=f"project:{project_id}:runtime", run_type="python",
            stage="operate", parent_run_id=task_id,
            request={"source_uri": context["source_uri"], "source_sha256": context["source_sha256"],
                     "input": inputs, "args": context["spec"].get("fixed_args") or [],
                     "timeout_seconds": context["spec"].get("timeout_seconds", 300),
                     "shared_write": context["spec"].get("shared_write", False), "operation_action": binding},
            idempotency_key=f"project-operation:{project_id}:{task_id}:{action_id}:{dispatch_key}")

    async def validate_dispatch(self, run: dict[str, Any]) -> None:
        request = dict(run.get("request_json") or {})
        binding = dict(request.get("operation_action") or {})
        if not binding:
            return
        project_id = str(run["project_id"])
        task_id = str(binding.get("task_id") or "")
        if str(run.get("parent_run_id")) != task_id:
            raise PermissionError("operation dispatch parent differs")
        context = await self._context(project_id, task_id, str(binding.get("action_id") or ""), request.get("input") or {})
        await self._action_approval(project_id, context)
        if any(binding.get(key) != context[key] for key in ("action_definition_sha256", "baseline_sha256", "input_sha256")):
            raise PermissionError("operation dispatch binding is stale")
        if context.get('manifest_ref'):
            from assistant_kernel.services.document_runtime import same_resource_ref
            if (request.get('python_protocol')!='fixed-manifest.v1' or not same_resource_ref(request.get('manifest_ref'),context['manifest_ref'])
                    or not same_resource_ref(request.get('runtime_unit_ref'),context['runtime_unit_ref'])):
                raise PermissionError('Operating action is outside its fixed RuntimeUnit')
            return  # ProjectPython validates exact inputs/args/resources/control at the shared dispatch gate.
        expected = {"source_uri": context["source_uri"], "source_sha256": context["source_sha256"],
                    "args": context["spec"].get("fixed_args") or [],
                    "timeout_seconds": context["spec"].get("timeout_seconds", 300),
                    "shared_write": context["spec"].get("shared_write", False)}
        if any(request.get(key) != value for key, value in expected.items()):
            raise PermissionError("operation dispatch execution differs from the approved runbook")
        if context["spec"]["risk"] == "high":
            grant = (context["task"]["request_json"]["approved_actions"])[context["action_id"]]
            key = f"project-operation:{project_id}:{task_id}:{context['action_id']}:approval:{grant['approval_message_id']}"
            if run.get("idempotency_key") != key:
                raise PermissionError("high-risk dispatch must use the single real approval's execution identity")
        else:
            episode = canonical_sha256({"task_id": task_id, "action_id": context["action_id"],
                **{key: context[key] for key in ("action_definition_sha256", "baseline_sha256", "input_sha256")}})
            if run.get("idempotency_key") != f"project-operation:{project_id}:{task_id}:{context['action_id']}:episode:{episode}":
                raise PermissionError("operation dispatch must use its stable Task episode identity")

    async def request_report(self, project_id: str, report_window: dict[str, Any],
                             source_message_id: str) -> dict[str, Any]:
        await self._baseline(project_id)
        if not isinstance(report_window, dict) or set(report_window) - {"start", "end", "timezone"}:
            raise ValueError("report window accepts only start, end and timezone")
        try:
            start, end = datetime.fromisoformat(report_window["start"]), datetime.fromisoformat(report_window["end"])
            if start.tzinfo is None or end.tzinfo is None or start >= end:
                raise ValueError("report window requires ordered timezone-aware timestamps")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("report window requires ordered timezone-aware timestamps") from exc
        zone = str(report_window.get("timezone") or "Asia/Shanghai")
        try:
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("report window timezone must be an IANA timezone") from exc
        window = {"start": start.astimezone(timezone.utc).isoformat(),
                  "end": end.astimezone(timezone.utc).isoformat(), "timezone": zone}
        key = "operation-report:" + canonical_sha256(window)
        return await self.queue.queue(project_id, f"评估项目运营效果并生成报告：{window['start']} 至 {window['end']}",
                                      source_message_id, task_kind="operation", report_window=window,
                                      idempotency_key=key)
