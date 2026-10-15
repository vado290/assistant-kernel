"""kernel-mcp: shared services over streamable HTTP for the user's own MCP clients (bearer token).

Project Vault reads, embeddings, rerank, chat, the model list and service credentials. It holds no
task or run state; the rebuilt core's tools for PM / Supervisor turns live in ``core/tools.py``.
"""
from __future__ import annotations

import argparse
import json
import secrets as secret_compare
from pathlib import Path

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl

from assistant_kernel.config import KernelSettings
from assistant_kernel.mcp_server.error_protocol import install_error_protocol
from assistant_kernel.model_providers import CatalogChatProvider, QwenProvider
from assistant_kernel.storage import ProjectRuntimeManager


settings = KernelSettings.from_environment()
runtime_manager = ProjectRuntimeManager(settings.data_root)

class JsonTokenVerifier:
    """Reload the bearer token from JSON on every request so key rotation needs no restart."""

    def __init__(self, settings: KernelSettings):
        self.settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        expected = (
            self.settings.load_provider_secrets().internal.mcp_bearer_token.get_secret_value()
        )
        if not expected or not secret_compare.compare_digest(token, expected):
            return None
        return AccessToken(
            token=token,
            client_id="assistant-kernel-client",
            scopes=["assistant:tools"],
        )


mcp = MCPServer(
    name="assistant-kernel",
    instructions=(
        "Use these tools as the durable Assistant Kernel boundary. Always pass project_id. "
        "Read only inside that project's Vault. Embedding, reranking and chat are model calls."
    ),
    token_verifier=JsonTokenVerifier(settings),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("http://localhost"),
        resource_server_url=AnyHttpUrl(
            f"http://127.0.0.1:{settings.mcp_port}/mcp"
        ),
        required_scopes=["assistant:tools"],
    ),
)

install_error_protocol(mcp)


def _safe_project_file(project_id: str, relative_path: str) -> Path:
    runtime = runtime_manager.ensure(project_id)
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("relative_path must stay inside the project Vault")
    target = (runtime.vault_root / relative).resolve()
    root = runtime.vault_root.resolve()
    if target != root and root not in target.parents:
        raise ValueError("resolved path escaped the project Vault")
    return target


@mcp.tool()
async def assistant_project_runtime(project_id: str) -> dict:
    """Create or inspect a project's durable Vault and isolated Codex home."""
    runtime = runtime_manager.ensure(project_id)
    return {
        "project_id": project_id,
        "vault_uri": f"vault://{project_id}/",
        "codex_home": str(runtime.codex_home),
    }


@mcp.tool()
def assistant_vault_list(project_id: str, relative_path: str = "", limit: int = 200) -> dict:
    """List files below one project Vault without crossing project boundaries."""
    root = _safe_project_file(project_id, relative_path)
    if not root.exists():
        return {"files": []}
    if not root.is_dir():
        raise ValueError("relative_path must identify a directory")
    base = runtime_manager.ensure(project_id).vault_root
    files = []
    for item in sorted(root.rglob("*")):
        if item.is_file():
            files.append(str(item.relative_to(base)).replace("\\", "/"))
            if len(files) >= min(max(limit, 1), 1000):
                break
    return {"files": files}


@mcp.tool()
def assistant_vault_read_text(
    project_id: str, relative_path: str, max_bytes: int = 262144
) -> dict:
    """Read a UTF-8 text artifact from one project Vault with a size limit."""
    target = _safe_project_file(project_id, relative_path)
    if not target.is_file():
        raise FileNotFoundError(relative_path)
    limit = min(max(max_bytes, 1), 2 * 1024 * 1024)
    data = target.read_bytes()
    if len(data) > limit:
        raise ValueError(f"file exceeds max_bytes={limit}")
    return {"uri": f"vault://{project_id}/{relative_path}", "text": data.decode("utf-8")}


@mcp.tool()
async def assistant_embed_texts(
    project_id: str, texts: list[str], dimensions: int = 1024, provider: str = "dashscope"
) -> dict:
    """Create retrieval embeddings through the configured provider adapter."""
    runtime_manager.ensure(project_id)
    if provider != "dashscope":
        raise NotImplementedError("provider adapter is not implemented")
    config = settings.load_provider_secrets().providers.get("dashscope")
    if config is None:
        raise RuntimeError("dashscope provider is not configured")
    result = await QwenProvider(config).embed(texts, dimensions)
    return result.model_dump(exclude={"provider_raw_data"})


@mcp.tool()
async def assistant_rerank(
    project_id: str,
    query: str,
    documents: list[str],
    top_n: int | None = None,
    provider: str = "dashscope",
) -> dict:
    """Rerank recalled documents; callers must retain their pre-rerank fallback order."""
    runtime_manager.ensure(project_id)
    if provider != "dashscope":
        raise NotImplementedError("provider adapter is not implemented")
    config = settings.load_provider_secrets().providers.get("dashscope")
    if config is None:
        raise RuntimeError("dashscope provider is not configured")
    result = await QwenProvider(config).rerank(query, documents, top_n)
    return result.model_dump(exclude={"provider_raw_data"})


@mcp.tool()
def assistant_model_catalog() -> dict:
    """List configured chat providers and their model aliases without returning secrets."""
    providers = settings.load_provider_secrets().providers
    return {
        name: {
            "enabled": provider.enabled,
            "provider_type": provider.provider_type,
            "models": list(provider.chat_models),
        }
        for name, provider in providers.items()
    }


@mcp.tool()
async def assistant_chat(
    project_id: str,
    provider: str,
    model: str,
    prompt: str,
    system_prompt: str = "",
    max_output_tokens: int = 2048,
    temperature: float | None = None,
) -> dict:
    """Call one model from the merged callModels catalog and return text plus usage."""
    runtime_manager.ensure(project_id)
    normalized = {"qwen": "dashscope", "doubao": "volcengine"}.get(provider, provider)
    config = settings.load_provider_secrets().providers.get(normalized)
    if config is None:
        raise ValueError("provider is not configured")
    result = await CatalogChatProvider(normalized, config).chat(
        prompt, model, system_prompt, max_output_tokens, temperature
    )
    return result.model_dump()


@mcp.resource("assistant://configuration")
def assistant_configuration() -> str:
    """Return non-secret runtime configuration and provider readiness."""
    secrets = settings.load_provider_secrets()
    qwen = secrets.providers.get("dashscope")
    return json.dumps(
        {
            "data_root": str(settings.data_root),
            "database": {
                "host": settings.postgres_host,
                "port": settings.postgres_port,
                "database": settings.postgres_database,
                "user": settings.postgres_user,
            },
            "providers": {"dashscope": bool(qwen and qwen.api_key.get_secret_value())},
        },
        ensure_ascii=False,
    )


@mcp.tool()
def assistant_credential_get(category: str, name: str) -> dict:
    """Read one credential/service record from the Assistant-owned credential JSON."""
    allowed = {
        "remote_hosts",
        "tunnels",
        "external_mappings",
        "databases",
        "websites",
        "services",
    }
    if category not in allowed:
        raise ValueError(f"category must be one of {sorted(allowed)}")
    document = json.loads(settings.service_credentials_path.read_text(encoding="utf-8-sig"))
    record = (document.get(category) or {}).get(name)
    if record is None:
        raise KeyError(f"credential record not found: {category}/{name}")
    return {"category": category, "name": name, "record": record}


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
    args = parser.parse_args()
    if args.transport == "streamable-http":
        mcp.run(
            transport=args.transport,
            host=settings.mcp_host,
            port=settings.mcp_port,
            streamable_http_path="/mcp",
        )
    else:
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    run()
