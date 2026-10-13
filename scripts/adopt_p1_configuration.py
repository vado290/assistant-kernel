"""Retired historical migration; never recreate management state."""

async def adopt(*args, **kwargs):
    raise PermissionError("Historical Butler/background migration is retired")

if __name__ == "__main__":
    raise SystemExit("Retired historical migration; no data was changed")
