"""Conservative recovery classification for reconciling Runs.

T1  structurally side-effect-free Runs (Supervisor consultations) are
    requeued automatically through bounded retries.
T3  everything else keeps the existing Discord reconciliation card.

The classifier never touches a Run whose recorded process is still alive
and never infers absence of external effects from an empty file/candidate
record. Network, account, database and trading effects need an authoritative
receipt ledger before any broader automatic replay can be enabled.
"""
from __future__ import annotations

import logging
from typing import Any

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.reconciliation import ReconciliationService


class RecoveryService:
    # Only side-effect-free consultations use T1; other active Runs use cards.
    T1_INTERACTION_KINDS = {"supervisor_consultation"}
    def __init__(self, settings: KernelSettings, repository: CoreRepository):
        self.settings = settings
        self.repository = repository
        self.diagnostics = ReconciliationService(settings, repository)

    def _tier(self, run: dict[str, Any], diagnostic: dict[str, Any]) -> int:
        request = dict(run.get("request_json") or {})
        # A v1.2 Supervisor has the shared file package and real MCP tools.
        # Its role name no longer proves that replay has no external effects.
        if ((run.get('executor_json') or {}).get('resolved_supervisor_configuration')
                or (run.get('executor_json') or {}).get('file_agreement_revision') == '1.2'):
            return 3
        if request.get("interaction_kind") in self.T1_INTERACTION_KINDS:
            return 1
        return 3

    async def classify_and_recover(self, run: dict[str, Any]) -> dict[str, Any]:
        """Return the tier decision and perform the auto-recovery when safe."""
        diagnostic = self.diagnostics.diagnose(run)
        tier = self._tier(run, diagnostic)
        decision: dict[str, Any] = {
            "run_id": str(run["run_id"]),
            "tier": tier,
            "status": "left_for_card",
        }
        if tier == 3:
            return decision
        run_type = str(run.get("run_type") or "")
        reason = "side-effect-free supervisor consultation; safe bounded retry"
        next_status = await self.repository.auto_requeue_reconciled(
            run_id=str(run["run_id"]), reason=reason,
        )
        decision["status"] = next_status
        if next_status == "dead_letter":
            decision["notify"] = True
        logging.info(
            "recovery tier %s run %s (%s) -> %s",
            tier, run["run_id"], run_type, next_status,
        )
        return decision

    async def sweep(self, limit: int = 20) -> list[dict[str, Any]]:
        """Classify every unresolved reconciling Run once per maintenance poll."""
        decisions: list[dict[str, Any]] = []
        for run in await self.repository.list_reconciling_runs(limit):
            try:
                decisions.append(await self.classify_and_recover(run))
            except Exception:
                logging.exception(
                    "recovery classification failed for run %s", run.get("run_id")
                )
        return decisions
