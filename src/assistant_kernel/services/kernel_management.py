"""Historical management identities are no longer executable."""
MANAGEMENT_PROJECT = "kernel"
MANAGEMENT_AGENT = "retired:management"
RETIRED_TOOLS = frozenset({"propose_project", "create_project", "validate_system_config",
    "update_system_config", "restore_system_config", "get_public_background_fields"})

def retired_identity(project, agent):
    return project in {"system", "butler-maintenance"} or agent in {
        "system-butler", "project:kernel:intake", "project:butler-maintenance:intake", MANAGEMENT_AGENT}

def require_active_identity(project, agent):
    if retired_identity(project, agent):
        raise PermissionError("Butler is retired; select an existing Project PM")

def management_route(data_root, route):
    return (MANAGEMENT_PROJECT, MANAGEMENT_AGENT) if retired_identity(*route) else route
