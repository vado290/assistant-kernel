"""Project-scoped immutable Incident records for on-demand Operator analysis."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.storage import ProjectRuntimeManager


class IncidentService:
    def __init__(self, settings: KernelSettings):
        self.runtime = ProjectRuntimeManager(settings.data_root)

    def record(
        self, *, project_id: str, incident_key: str, severity: str,
        summary: str, evidence_refs: list[dict[str, Any]],
        source_run_id: str | None = None,
    ) -> dict[str, Any]:
        stable = hashlib.sha256(
            f"{project_id}:{incident_key}".encode("utf-8")
        ).hexdigest()[:20]
        root = self.runtime.ensure(project_id).vault_root / "operations" / "incidents"
        target = root / f"incident-{stable}.json"
        if target.is_file():
            return json.loads(target.read_text(encoding="utf-8"))
        payload = {
            "schema_version": "incident.v1",
            "incident_id": f"incident-{stable}",
            "incident_key": incident_key,
            "project_id": project_id,
            "severity": severity,
            "status": "open",
            "summary": summary,
            "observed_facts": [],
            "inferences": [],
            "executed_actions": [],
            "recommended_actions": [],
            "evidence_refs": evidence_refs,
            "source_run_id": source_run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return payload
