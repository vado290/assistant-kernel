"""Retired historical entrypoint. Kernel PM now owns management directly."""

def planned_files(*args, **kwargs):
    raise PermissionError("Butler/intake is retired; no legacy files may be generated")

async def migrate(*args, **kwargs):
    raise PermissionError("Butler/intake is retired; this historical migration cannot run")

if __name__ == "__main__":
    raise SystemExit("Retired: use the current Kernel PM, not the old Butler migration")
