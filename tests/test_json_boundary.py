import uuid
from datetime import datetime, timezone

from assistant_kernel.repositories import json_compatible


def test_json_boundary_normalizes_database_values() -> None:
    identifier = uuid.uuid4()
    timestamp = datetime.now(timezone.utc)
    result = json_compatible({"id": identifier, "created_at": timestamp})
    assert result == {"id": str(identifier), "created_at": timestamp.isoformat()}
