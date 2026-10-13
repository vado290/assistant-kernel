"""Immutable change facts; all new writes move together after a verified backfill."""
from __future__ import annotations
import json
from typing import Any
from psycopg.types.json import Jsonb

LEDGER_KEY = "project-file-ledger:v2"


async def enabled(connection) -> bool:
    row = await (await connection.execute(
        "SELECT value FROM assistant.kernel_meta WHERE key=%s", (LEDGER_KEY,),
    )).fetchone()
    return bool(row and row["value"].get("write_enabled") is True
                and row["value"].get("backfill_complete") is True)


def semantic_record(record: dict[str, Any]) -> dict[str, Any]:
    # Capturing the same already-settled batch again must retain the FIRST real
    # timestamps, while a changed source/effect/reason under the same ID conflicts.
    return {k:v for k,v in record.items() if k not in {"recorded_at","observed_at","occurred_at"}}


async def append(connection, record: dict[str, Any], *, legacy_message_id=None,
                 legacy_created_at=None) -> dict[str, Any]:
    required = {"change_id","project_id","batch_id","target_uri","path","scope_kind",
                "operation","observed_at","recorded_at","cause_status","effect_status"}
    if not required <= set(record):
        raise ValueError("change fact is missing required identity or observation fields")
    row = await (await connection.execute(
        """INSERT INTO assistant.project_file_changes(
          change_id,project_id,batch_id,file_id,target_uri,path,scope_kind,operation,
          before_sha256,after_sha256,occurred_at,observed_at,recorded_at,reason,cause_status,
          reason_source_refs,diff_ref,actor,task_ref,run_ref,lines_added,lines_deleted,
          file_lines_added,file_lines_deleted,diff_basis,effect_status,record_json,
          legacy_message_id,legacy_message_created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(change_id) DO UPDATE SET change_id=EXCLUDED.change_id
        RETURNING record_json,legacy_message_id""",
        (record["change_id"],record["project_id"],record["batch_id"],record.get("file_id"),
         record["target_uri"],record["path"],record["scope_kind"],record["operation"],
         record.get("before_sha256"),record.get("after_sha256"),record.get("occurred_at"),
         record["observed_at"],record["recorded_at"],record.get("reason_text", ""),record["cause_status"],
         Jsonb(record.get("reason_source_refs",[])),Jsonb(record.get("diff_ref")),
         Jsonb(record.get("actor","kernel-observer")),Jsonb(record.get("task_ref",record.get("task_id"))),
         Jsonb(record.get("run_ref",record.get("run_id"))),record.get("lines_added"),record.get("lines_deleted"),
         record.get("file_lines_added"),record.get("file_lines_deleted"),record.get("diff_basis"),
         record["effect_status"],Jsonb(record),legacy_message_id,legacy_created_at),
    )).fetchone()
    if semantic_record(row["record_json"]) != semantic_record(record):
        raise ValueError("change ID was reused with different immutable facts")
    if legacy_message_id and str(row["legacy_message_id"]) != str(legacy_message_id):
        raise ValueError("change migration has a conflicting historical message identity")
    return dict(row["record_json"])


def union_query() -> str:
    return """WITH changes AS (
      SELECT project_id,COALESCE(legacy_message_id::text,change_id) AS row_id,
             COALESCE(legacy_message_created_at,recorded_at) AS created_at,record_json AS content_json
      FROM assistant.project_file_changes
      UNION ALL
      SELECT m.project_id,m.message_id::text,m.created_at,m.content_json FROM assistant.messages m
      WHERE m.direction='system' AND m.message_type='project_change'
        AND m.content_json->>'schema_version'='project-change.v1'
        AND NOT EXISTS(SELECT 1 FROM assistant.project_file_changes c
                       WHERE c.project_id=m.project_id AND c.change_id=m.content_json->>'change_id')
    ) """
