"""Retired historical migration; never recreate management state."""

async def migrate(*args, **kwargs):
    raise PermissionError("Historical Butler/background migration is retired")

async def rollback(*args, **kwargs):
    raise PermissionError("Historical Butler/background migration is retired")

def planned_files(*args, **kwargs):
    raise PermissionError("Historical Butler/background generation is retired")

if __name__ == "__main__":
    raise SystemExit("Retired historical migration; no data was changed")
