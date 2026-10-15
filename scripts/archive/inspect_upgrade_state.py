"""Read-only, repeatable snapshot of state which a release must preserve.

Waiting interactions are protected state, not permission to cancel or consume them.
This does not assert that a live system is quiescent; deployment must stop its
writers and take another snapshot and a consistent backup before migration.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories.database import AssistantDatabase
from assistant_kernel.services.document_runtime import TaskDocumentService


TABLES = ("messages", "runs", "sessions", "model_invocations", "project_grants")


async def inspect(settings=None):
    settings = settings or KernelSettings.from_environment()
    documents = TaskDocumentService(settings)
    result = {"schema_version": "upgrade-state.v1", "tables": {},
              "protected_interactions": [], "accepted_documents": [], "blockers": [],
              "read_only": True,"runtime_units":[],"service_instances":[]}
    async with AssistantDatabase(settings).connection() as connection:
        await connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        for table in TABLES:
            # Names are the fixed allowlist above. Include every row and field,
            # without exporting user text, answers, credentials or approvals.
            row = await (await connection.execute(
                "SELECT count(*) AS count,md5(coalesce(string_agg(j::text,'' ORDER BY j::text),'')) AS digest "
                "FROM (SELECT to_jsonb(t) AS j FROM assistant." + table + " t) source"
            )).fetchone()
            result["tables"][table] = dict(row)
        version = await (await connection.execute(
            "SELECT value FROM assistant.kernel_meta WHERE key='schema_version'"
        )).fetchone()
        result["database_schema"] = version["value"]
        runs = await (await connection.execute(
            "SELECT run_id,project_id,status,waiting_question_message_id,request_json,result_json "
            "FROM assistant.runs ORDER BY run_id"
        )).fetchall()
        for row in runs:
            identity = str(row["run_id"])
            request = row["request_json"] or {}
            if row["status"] in {"running", "reconciling"}:
                result["blockers"].append({"run_id": identity, "reason": row["status"]})
            # Queued work is preserved and must remain unclaimed during cutover.
            if row["status"] in {"queued", "waiting_user", "waiting_supervisor", "blocked"}:
                question_id = row["waiting_question_message_id"]
                question = None
                if question_id:
                    question = await (await connection.execute(
                        "SELECT md5(to_jsonb(m)::text) AS digest FROM assistant.messages m WHERE message_id=%s",
                        (question_id,),
                    )).fetchone()
                    if not question:
                        result["blockers"].append({"run_id": identity, "reason": "question_missing"})
                result["protected_interactions"].append({
                    "run_id": identity, "project_id": row["project_id"], "status": row["status"],
                    "question_id": str(question_id) if question_id else None,
                    "question_digest": question["digest"] if question else None,
                    "control_sha256": hashlib.sha256(json.dumps(
                        {"request": request, "result": row["result_json"]},
                        sort_keys=True, separators=(",", ":"), default=str,
                    ).encode()).hexdigest(),
                    "has_approved_actions": bool(request.get("approved_actions")),
                    "approval_kind": request.get("approval_kind"),
                    "task_phase": request.get("task_phase"),
                })
            if request.get("task_phase") == "publishing":
                result["blockers"].append({"run_id": identity, "reason": "unfinished_publication"})
            for kind, ref in (request.get("accepted_documents") or {}).items():
                try:
                    documents.read(row["project_id"], ref)
                    readable = True
                except Exception:
                    readable = False
                    result["blockers"].append({"run_id": identity, "reason": "accepted_document_unreadable", "kind": kind})
                result["accepted_documents"].append({"run_id": identity, "kind": kind,
                    "ref": ref, "readable": readable})
        result["outbox_counts"] = [dict(r) for r in await (await connection.execute(
            "SELECT delivery_status,count(*) AS count FROM assistant.messages "
            "WHERE direction='outbound' GROUP BY delivery_status ORDER BY delivery_status"
        )).fetchall()]
        unknown = await (await connection.execute(
            "SELECT run_id FROM assistant.runs WHERE "
            "request_json->>'effect_status' IN ('unknown','unknown_effects') "
            "OR result_json->>'effect_status' IN ('unknown','unknown_effects')"
        )).fetchall()
        result["blockers"].extend({"run_id":str(r["run_id"]), "reason":"unknown_effects"} for r in unknown)
        for table,key in (('project_runtime_units','runtime_units'),('service_instances','service_instances')):
            exists=await (await connection.execute('SELECT to_regclass(%s) AS name',('assistant.'+table,))).fetchone()
            if not exists['name']:continue
            rows=await (await connection.execute('SELECT to_jsonb(t) AS body,md5(to_jsonb(t)::text) AS digest FROM assistant.'+table+' t ORDER BY project_id,unit_id')).fetchall()
            for row in rows:
                body=row['body'];item={'project_id':body['project_id'],'unit_id':body['unit_id'],'digest':row['digest']}
                if table=='project_runtime_units':
                    item.update(desired_state=body['desired_state'],control_revision=body['control_revision'])
                    if body['desired_state']=='running':result['blockers'].append({**item,'reason':'enabled_runtime_unit; drain or stop through its existing control before restart'})
                else:
                    item.update(instance_id=body['instance_id'],observed_state=body['observed_state'],cleanup_status=body['cleanup_status'],
                        work_items=len(body['state_json'].get('work_control',{}).get('items',{})))
                    if item['work_items'] or body['cleanup_status']!='verified':result['blockers'].append({**item,'reason':'service work, waiting input or cleanup remains'})
                result[key].append(item)
    return result


def preserved(before, after):
    """Health/meta receipts may change; existing business facts may not disappear."""
    return all(before[key] == after[key] for key in
               ("tables", "protected_interactions", "accepted_documents", "outbox_counts")) and all(
               before.get(key,[])==after.get(key,[]) for key in ('runtime_units','service_instances'))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    args = parser.parse_args()
    result = asyncio.run(inspect())
    if args.compare:
        result["preserved"] = preserved(json.loads(args.compare.read_text("utf-8-sig")), result)
    args.receipt.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps({"blockers": result["blockers"],
                      "protected_interactions": len(result["protected_interactions"]),
                      "accepted_documents": len(result["accepted_documents"]),
                      "preserved": result.get("preserved")}))
    raise SystemExit(2 if result["blockers"] or result.get("preserved") is False else 0)
