"""One desired configuration input, immutable documents and DB-owned adoption facts.

Accepting a candidate never restarts infrastructure or fabricates consumer receipts.
Consumers explicitly adopt their groups at their own verified safe boundaries.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from assistant_kernel.config.system_config import (
    PUBLIC_GROUPS, apply_mode, changed_config, leaf_values, parse_system_config,
    pointer_parts, pointer_value, validate_system_config,
)
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.services.contracts import canonical_json, contracts
from assistant_kernel.services.document_runtime import TaskDocumentService, resource_ref
from assistant_kernel.services.scheduling_scope import active_configuration, retired_config_path

STATE_KEY = "system-config:v1"


class SystemConfigService:
    def __init__(self, settings):
        self.settings = settings
        self.database = AssistantDatabase(settings)
        self.documents = TaskDocumentService(settings)
        self.path = Path(os.getenv("KERNEL_SYSTEM_CONFIG", str(settings.data_root / "config/kernel-system.json")))
        self._observed: tuple[str, float] | None = None

    async def state(self) -> dict[str, Any] | None:
        async with self.database.connection() as c:
            row = await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key=%s", (STATE_KEY,))).fetchone()
            return dict(row["value"]) if row else None

    def _config(self, ref: dict[str, Any]) -> dict[str, Any]:
        document = self.documents.read("kernel", ref)
        if document["schema_version"] != "kernel-system-config.v1":
            raise ValueError("configuration reference has the wrong Schema")
        return active_configuration(document["payload"])

    def _read_input(self) -> bytes | None:
        try:
            with self.path.open("rb") as stream:
                encoded = stream.read(2 * 1024 * 1024 + 1)
        except FileNotFoundError:
            return None
        if len(encoded) > 2 * 1024 * 1024:
            raise ValueError("configuration exceeds size limit")
        return encoded

    async def view(self, *, view: str, paths: list[str], management: bool) -> dict[str, Any]:
        if view not in {"desired", "effective", "status"}:
            raise ValueError("unknown configuration view")
        state = await self.state()
        if not state:
            return {"view":view,"desired_ref":None,"values":{},"group_results":[],"secret_values_included":False}
        desired = self._config(state["desired_ref"])
        requested = paths or [p for p in leaf_values(desired)
                              if management or pointer_parts(p)[0] in PUBLIC_GROUPS]
        if not management and any(pointer_parts(p)[0] not in PUBLIC_GROUPS for p in requested):
            raise PermissionError("configuration field is reserved for Kernel management")
        values = {}
        def effective_value(path):
            requested_value = pointer_value(desired, path)
            if isinstance(requested_value, dict):
                return {k:effective_value(path + "/" + k.replace("~", "~0").replace("/", "~1"))
                        for k in requested_value}
            if path in {"/$schema", "/schema_version"}:
                return requested_value
            group = self._group(path)
            actual = next((g for g in state["groups"] if g["group"] == group), None)
            return pointer_value(self._config(actual["effective_ref"]), path) if actual and actual["effective_ref"] else None
        for path in requested:
            if retired_config_path(path):raise ValueError('Legacy external scheduler configuration is retired; use /kernel/scheduler')
            if view != "effective":
                values[path] = pointer_value(desired, path)
                continue
            values[path] = effective_value(path)  # pending is separate from zero
        groups = state["groups"] if management else [g for g in state["groups"]
                 if g["group"].split(":", 1)[0].split('/',1)[0] in PUBLIC_GROUPS]
        groups=[g for g in groups if not retired_config_path('/'+g['group'].split(':',1)[0])]
        if management and view == "status":
            async with self.database.connection() as c:
                latest = await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key='system-config-input-status'")).fetchone()
            if latest and latest["value"].get("status") == "invalid":
                groups = [*groups,{"group":"configuration_input","apply_mode":"dynamic_admission",
                    "status":"failed","previous_ref":state["desired_ref"],"effective_ref":state["desired_ref"],
                    "receipt_refs":[],"reason":str(latest["value"].get("reason","Invalid configuration input"))[:1500]}]
        return {"view":view,"desired_ref":state["desired_ref"],"values":values,
                "group_results":groups,"secret_values_included":False}

    @staticmethod
    def _group(path: str) -> str:
        parts=pointer_parts(path)
        # These parameters have different real consumers. One broad map group
        # must not become applied merely because directory measurement ran.
        prefix='/'.join(parts[:2]) if parts[0]=='map' or parts[:2]==['kernel','scheduler'] else parts[0]
        return prefix + ":" + apply_mode(path)

    async def repartition_application_groups(self):
        """Explicit upgrade step; retain original values, receipts and effective refs."""
        prior=await self.state()
        if not prior:return None
        existing={g['group']:g for g in prior['groups']}
        paths=[p for p in leaf_values(self._config(prior['desired_ref'])) if p not in {'/$schema','/schema_version'}]
        wanted={self._group(p) for p in paths}
        if set(existing)==wanted:return prior
        groups=[]
        for name in sorted(wanted):
            legacy=name.split('/',1)[0]+':'+name.rsplit(':',1)[1] if '/' in name else name
            old=existing.get(name) or existing.get(legacy)
            if old is None:raise ValueError('Unknown configuration group migration source')
            groups.append({**deepcopy(old),'group':name})
        async with self.database.connection() as c:
            await c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(STATE_KEY,))
            row=await (await c.execute('SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE',(STATE_KEY,))).fetchone()
            state=dict(row['value'])
            if state!=prior:raise ValueError('Configuration state changed during group migration')
            state.update(groups=groups,group_layout_revision=2)
            receipt={**self.documents.read('kernel',state['receipt_ref'])['payload'],'groups':groups,
                     'recorded_at':datetime.now(timezone.utc).isoformat()}
            state['receipt_ref']=self.documents.write_scoped_revision(project_id='kernel',scope='project',object_id='system-config',
                document_kind='config-apply-receipt',document_schema='config-apply-receipt.v1',payload=receipt,
                status='submitted',actor_type='kernel',actor_id='system-config',reason='Separate actual map consumers without changing any parameter',
                source_refs=[state['receipt_ref']],supersedes_ref=state['receipt_ref'])
            await c.execute('UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key=%s',(Jsonb(state),STATE_KEY))
            await c.commit()
        return state

    async def _user_source(self, source_message_id: str) -> dict[str, Any]:
        async with self.database.connection() as c:
            row = await (await c.execute(
                "SELECT message_id,project_id,direction,user_id FROM assistant.messages WHERE message_id=%s",
                (source_message_id,),
            )).fetchone()
        if not row or row["project_id"] != "kernel" or row["direction"] != "inbound" or not row["user_id"]:
            raise PermissionError("configuration update requires a real Kernel user input")
        return resource_ref(kind="message", identity=str(row["message_id"]),
                            uri=f"db://assistant/messages/{row['message_id']}", purpose="explicit configuration input")

    async def update(self, *, changes: list[dict[str, Any]], expected_revision: str,
                     reason: str, source_message_id: str) -> dict[str, Any]:
        source = await self._user_source(source_message_id)
        current = await self.state()
        if not current:
            raise ValueError("configuration must first be imported from the actual deployment")
        candidate = changed_config(self._config(current["desired_ref"]), changes)
        return await self.accept(candidate=candidate, expected_revision=expected_revision,
                                 reason=reason, source_kind="kernel_skill", source_refs=[source],
                                 write_input=True)

    async def restore(self, *, revision: str, expected_revision: str,
                      source_message_id: str) -> dict[str, Any]:
        source = await self._user_source(source_message_id)
        async with self.database.connection() as c:
            row = await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key=%s",
                                        (f"system-config-revision:{revision}",))).fetchone()
        if not row:
            raise ValueError("unknown configuration revision")
        old = self._config(row["value"]["ref"])
        return await self.accept(candidate=old, expected_revision=expected_revision,
                                 reason="Restore prior configuration content as a new submission",
                                 source_kind="kernel_skill", source_refs=[source], write_input=True)

    async def accept(self, *, candidate: dict[str, Any], expected_revision: str,
                     reason: str, source_kind: str, source_refs: list[dict[str, Any]],
                     write_input: bool, input_sha256: str | None = None) -> dict[str, Any]:
        """Internal acceptance; a caller must bind the trusted source first."""
        validation = validate_system_config(candidate)
        if not validation["valid"]:
            raise ValueError("; ".join(validation["issues"]))
        if source_kind not in {"kernel_skill", "user_message", "external_file"}:
            raise ValueError("invalid configuration source kind")
        if source_kind != "external_file" and not source_refs:
            raise PermissionError("configuration source is missing")
        prior = await self.state()
        actual_revision = prior["revision"] if prior else "uninitialized"
        if actual_revision != expected_revision:
            raise ValueError("stale configuration revision")
        raw_input = self._read_input()
        observed = hashlib.sha256(raw_input).hexdigest() if raw_input is not None else None
        if input_sha256 is not None and observed != input_sha256:
            raise ValueError("configuration input changed before acceptance")
        previous_ref = prior["desired_ref"] if prior else None
        doc = self.documents.write_scoped_revision(
            project_id="kernel", scope="project", object_id="system-config",
            document_kind="kernel-system-config", document_schema="kernel-system-config.v1",
            payload=deepcopy(candidate), status="submitted", actor_type="kernel",
            actor_id="system-config", reason=reason, source_refs=source_refs,
            supersedes_ref=previous_ref,
        )
        prior_groups = {g["group"]:g for g in (prior or {}).get("groups", [])}
        old_values = leaf_values(self._config(previous_ref)) if previous_ref else {}
        groups = []
        flattened = {p:v for p,v in leaf_values(candidate).items() if p not in {"/$schema", "/schema_version"}}
        paths = set(flattened) | (set(old_values) - {"/$schema", "/schema_version"})
        missing = object()
        for group in sorted({self._group(p) for p in paths}):
            mode = group.split(":", 1)[1]
            before = prior_groups.get(group)
            changed = any(old_values.get(p, missing) != flattened.get(p, missing)
                          for p in paths if self._group(p) == group)
            if before and not changed:
                groups.append(deepcopy(before)); continue
            status = mode if mode in {"pending_restart", "pending_recreate", "requires_migration"} else "pending_boundary"
            groups.append({"group":group,"apply_mode":mode,"status":status,
                           "previous_ref":before.get("effective_ref") if before else None,
                           "effective_ref":before.get("effective_ref") if before else None,
                           "receipt_refs":[],"reason":"Awaiting a verified consumer application receipt"})
        receipt = {"change_id":"config-" + doc["revision_id"],"desired_ref":doc,
                   "source_kind":source_kind,"source_refs":source_refs,"expected_revision":expected_revision,
                   "status":"pending","groups":groups,"affected_refs":[],
                   "recorded_at":datetime.now(timezone.utc).isoformat(),"runtime_verified":False}
        contracts().validate("ConfigApplyReceipt", receipt)
        receipt_ref = self.documents.write_scoped_revision(
            project_id="kernel",scope="project",object_id="system-config",
            document_kind="config-apply-receipt",document_schema="config-apply-receipt.v1",
            payload=receipt,status="submitted",actor_type="kernel",actor_id="system-config",
            reason="Configuration acceptance is separate from actual application",source_refs=source_refs,
        )
        state = {"revision":doc["revision_id"],"desired_ref":doc,"groups":groups,
                 "receipt_ref":receipt_ref,"input_sha256":observed,
                 "input_projection_pending":write_input,"source_kind":source_kind}
        async with self.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (STATE_KEY,))
            current = await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE",(STATE_KEY,))).fetchone()
            if (current["value"]["revision"] if current else "uninitialized") != expected_revision:
                raise ValueError("stale configuration revision")
            fresh_bytes = self._read_input()
            now_input = hashlib.sha256(fresh_bytes).hexdigest() if fresh_bytes is not None else None
            if now_input != observed:
                raise ValueError("configuration input changed during preparation")
            await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()",(STATE_KEY,Jsonb(state)))
            await c.execute("INSERT INTO assistant.kernel_meta(key,value) VALUES (%s,%s) ON CONFLICT(key) DO NOTHING",(f"system-config-revision:{state['revision']}",Jsonb({"ref":doc})))
            await c.commit()
        if write_input:
            await self.reconcile_input_projection()
        return receipt

    async def reconcile_input_projection(self) -> bool:
        """Repair only the desired-file projection; never reapply business work."""
        current = await self.state()
        if not current or not current.get("input_projection_pending"):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The file lock coordinates managed writers. External file writes remain
        # detectable changes, not an OS identity or permission guarantee.
        import fcntl
        async with self.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(STATE_KEY,))
            # Acquire DB first: never block the asyncio loop on a file lock held
            # by another coroutine which is awaiting this same DB guard.
            with self.path.with_suffix(self.path.suffix + ".lock").open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                row = await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE",(STATE_KEY,))).fetchone()
                state = dict(row["value"])
                if state["revision"] != current["revision"] or not state.get("input_projection_pending"):
                    return False
                encoded = json.dumps(self._config(state["desired_ref"]),ensure_ascii=False,sort_keys=True,indent=2).encode()
                desired_hash = hashlib.sha256(encoded).hexdigest()
                actual_bytes = self._read_input()
                actual = hashlib.sha256(actual_bytes).hexdigest() if actual_bytes is not None else None
                if actual not in {state["input_sha256"], desired_hash}:
                    raise ValueError("configuration file was edited; do not overwrite the newer input")
                if actual != desired_hash:
                    temporary = self.path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                    try:
                        temporary.write_bytes(encoded); temporary.replace(self.path)
                    finally:
                        temporary.unlink(missing_ok=True)
                state.update(input_sha256=hashlib.sha256(encoded).hexdigest(),input_projection_pending=False)
                await c.execute("UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key=%s",(Jsonb(state),STATE_KEY))
                await c.commit()
        return True

    async def poll(self, *, settle_seconds: float = 2) -> dict[str, Any]:
        if not self.path.is_file():
            return {"status":"uninitialized"}
        try:
            encoded = self._read_input()
        except ValueError as exc:
            return {"status":"invalid","reason":str(exc)}
        if encoded is None:
            return {"status":"uninitialized"}
        digest = hashlib.sha256(encoded).hexdigest();now = time.monotonic()
        if self._observed is None or self._observed[0] != digest:
            self._observed = (digest, now);return {"status":"settling"}
        if now - self._observed[1] < settle_seconds:
            return {"status":"settling"}
        state = await self.state()
        if state and state.get("input_projection_pending"):
            intended = json.dumps(self._config(state["desired_ref"]),ensure_ascii=False,sort_keys=True,indent=2).encode()
            if digest in {state.get("input_sha256"), hashlib.sha256(intended).hexdigest()}:
                await self.reconcile_input_projection();return {"status":"projection_reconciled"}
            # A distinct stable external edit is a new input, not a projection
            # to overwrite. Validate it and CAS against the latest desired head.
        if state and state.get("input_sha256") == digest:
            return {"status":"unchanged"}
        try:
            candidate = parse_system_config(encoded)
        except ValueError as exc:
            return {"status":"invalid","reason":str(exc)}
        # Preserve exact accepted input bytes so source refs remain readable after edits.
        source_path = self.settings.data_root / "runtime/config-inputs" / (digest + ".json")
        source_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.exists() and hashlib.sha256(source_path.read_bytes()).hexdigest() != digest:
            raise ValueError("immutable configuration source was changed")
        if not source_path.exists():
            temporary = source_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(encoded);temporary.replace(source_path)
        source = resource_ref(kind="source",identity="config-file-"+digest,
                              uri=str(source_path),sha256=digest,purpose="external_file; writer identity not established")
        receipt = await self.accept(candidate=candidate,expected_revision=state["revision"] if state else "uninitialized",
                                    reason="Stable configuration file accepted",source_kind="external_file",
                                    source_refs=[source],write_input=False,input_sha256=digest)
        return {"status":"accepted","receipt":receipt}

    async def settle_group(self, *, expected_revision: str, group: str,
                           evidence_ref: dict[str, Any], applied: bool, reason: str) -> dict[str, Any]:
        """Internal consumer receipt; never exposed as an arbitrary model tool."""
        from assistant_kernel.services.document_runtime import validate_resource_ref
        evidence_ref = validate_resource_ref(evidence_ref)
        async with self.database.connection() as c:
            await c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(STATE_KEY,))
            row=await (await c.execute("SELECT value FROM assistant.kernel_meta WHERE key=%s FOR UPDATE",(STATE_KEY,))).fetchone()
            if not row or row["value"]["revision"] != expected_revision:
                raise ValueError("configuration adoption receipt is stale")
            state=dict(row["value"]);matched=False
            for item in state["groups"]:
                if item["group"] != group:continue
                matched=True;item.update(status="applied" if applied else "failed",reason=reason,
                                         receipt_refs=[evidence_ref])
                if applied:item["effective_ref"]=state["desired_ref"]
            if not matched:raise ValueError("unknown configuration group")
            previous_receipt = self.documents.read("kernel",state["receipt_ref"])["payload"]
            statuses = [g["status"] for g in state["groups"]]
            receipt = {**previous_receipt,"groups":state["groups"],
                       "status":"applied" if all(x=="applied" for x in statuses) else "partially_applied" if "applied" in statuses else "pending",
                       "runtime_verified":all(x=="applied" for x in statuses),
                       "recorded_at":datetime.now(timezone.utc).isoformat()}
            # A bounded immutable file is prepared before DB acceptance; if the
            # transaction fails it remains an unaccepted orphan, never an effect.
            state["receipt_ref"] = self.documents.write_scoped_revision(
                project_id="kernel",scope="project",object_id="system-config",
                document_kind="config-apply-receipt",document_schema="config-apply-receipt.v1",
                payload=receipt,status="submitted",actor_type="kernel",actor_id="system-config",
                reason="Recorded actual configuration consumer result",source_refs=[evidence_ref],
                supersedes_ref=state["receipt_ref"],
            )
            await c.execute("UPDATE assistant.kernel_meta SET value=%s,updated_at=now() WHERE key=%s",(Jsonb(state),STATE_KEY))
            await c.commit()
        return state
