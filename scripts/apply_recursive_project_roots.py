"""Apply user-approved recursive parent-folder grants to an existing Project."""
from __future__ import annotations

import argparse
import asyncio
import json

from assistant_kernel.config import KernelSettings
from assistant_kernel.repositories import CoreRepository
from assistant_kernel.services.source_scope import SourceScopeService


async def run(args: argparse.Namespace) -> None:
    settings = KernelSettings.from_environment()
    repository = CoreRepository(settings)
    service = SourceScopeService(settings, repository)
    bindings = [json.loads(item) for item in args.binding_json]
    normalized = service.normalize_bindings(bindings)
    if not args.apply:
        print(json.dumps({
            "project_id": args.project_id,
            "apply": False,
            "bindings": normalized,
        }, ensure_ascii=False, indent=2))
        return
    async with repository.database.connection() as connection:
        await connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("workspace-scope-config",),
        )
        result = await service.apply_full_directory_grants(
            connection=connection,
            target_project_id=args.project_id,
            bindings=bindings,
            source_binding={
                "authorization_mode": "administrative_user_request",
                "reason": args.reason,
            },
            source_message_id=None,
            approval_message_id=None,
        )
        await connection.commit()
    print(json.dumps({
        "project_id": args.project_id,
        "apply": True,
        "bindings": result["bindings"],
        "grant_ids": result["grant_ids"],
        "superseded_grant_ids": result["superseded_grant_ids"],
    }, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--binding-json", action="append", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
