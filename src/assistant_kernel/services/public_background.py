"""Read historical revocations; public background loading and writes are retired."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from assistant_kernel.storage import ProjectRuntimeManager


BACKGROUND_DOCUMENTS = {
    "user": ("user-background.json", "user-background.v1"),
    "environment": ("development-environment.json", "development-environment.v1"),
}


class PublicBackgroundService:
    """Historical visibility adapter; never publish or inject shared profile facts."""

    def __init__(self, data_root: Path, settings=None):
        self.settings=settings
        self.runtime = ProjectRuntimeManager(data_root)
        canonical = self.runtime.describe("kernel").vault_root / "background"
        legacy = self.runtime.describe("butler-maintenance").vault_root / "background"
        # Exactly one active source. Legacy is a read fallback until migration.
        self.owner_project_id = "kernel" if canonical.exists() or not legacy.exists() else "butler-maintenance"
        self.root = canonical if self.owner_project_id == "kernel" else legacy

    def accepted(self):
        """Active Kernel callers supply settings; standalone legacy readers do not."""
        if self.settings is None:return None
        import psycopg
        from psycopg.rows import dict_row
        from assistant_kernel.services.document_runtime import TaskDocumentService
        s=self.settings
        with psycopg.connect(host=s.postgres_host,port=s.postgres_port,dbname=s.postgres_database,
                user=s.postgres_user,password=s.postgres_password.get_secret_value(),row_factory=dict_row,
                connect_timeout=10) as c:
            row=c.execute("SELECT value FROM assistant.kernel_meta WHERE key='public-background:v1'").fetchone()
        if not row:return None
        head=row['value']
        document=TaskDocumentService(s).read('kernel',head['accepted_ref'])
        return head,document['payload']

    async def accept(self,payload,*,expected_ref,approval_refs,reason):
        raise PermissionError("Public background is retired")

    def project_accepted(self):
        raise PermissionError("Public background is retired")

    def document_path(self, kind: str) -> Path:
        if kind not in BACKGROUND_DOCUMENTS:
            raise ValueError("unknown public background document")
        return self.runtime.resolve_below(self.root, BACKGROUND_DOCUMENTS[kind][0])

    def ensure(self) -> list[Path]:
        raise PermissionError("Public background is retired")


    @staticmethod
    def message_source_ids(refs: Any) -> set[str]:
        """Only typed, valid Message identifiers participate in source suppression."""
        identities: set[str] = set()
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict) or ref.get("kind") != "message":
                continue
            identity = ref.get("id") or ref.get("message_id") or ref.get("identity")
            try:
                identities.add(str(uuid.UUID(str(identity))))
            except (ValueError, TypeError, AttributeError):
                continue
        return identities

    def revoked_source_message_ids(self) -> set[str]:
        """Read two fixed documents; retain originals for deliberate audit retrieval."""
        if self.accepted() is not None:
            # Withdrawing a public field/purpose does not revoke an entire user
            # message, its other facts, or the approvals it also contains.
            return set()
        identities: set[str] = set()
        for kind, (_, schema) in BACKGROUND_DOCUMENTS.items():
            path = self.document_path(kind)
            if not path.is_file():
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or document.get("schema_version") != schema:
                raise ValueError(f"invalid public background schema: {kind}")
            facts = document.get("facts") or []
            if not isinstance(facts, list):
                raise ValueError("public background facts must be a list")
            for fact in facts:
                if isinstance(fact, dict) and (
                    fact.get("status") in {"withdrawn", "revoked"} or fact.get("revoked_at")
                ):
                    identities.update(self.message_source_ids(fact.get("source_refs")))
        return identities

    def read_project_context(
        self, project_id: str, *, fields: list[str] | None = None,
    ) -> dict[str, Any]:
        return {"schema_version":"public-background-context.v1", "project_id":project_id,
                "documents":[], "selected_field_ids":[]}
