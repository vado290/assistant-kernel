from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ModelNames(BaseModel):
    embedding: str = "text-embedding-v4"
    reranker: str = "qwen3-rerank"
    multimodal_embedding: str = "qwen3-vl-embedding"
    multimodal_reranker: str = "qwen3-vl-rerank"
    vision: str = "qwen3.7-plus"
    document_ocr: str = "qwen-vl-ocr"


class ProviderCredential(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    provider_type: str = "openai_compatible"
    api_key: SecretStr = SecretStr("")
    workspace_id: str = ""
    region: str = "cn-beijing"
    openai_compatible_base_url: str = ""
    native_base_url: str = ""
    rerank_base_url: str = ""
    models: ModelNames = Field(default_factory=ModelNames)
    chat_models: dict[str, dict[str, Any]] = Field(default_factory=dict)


class InternalCredentials(BaseModel):
    kernel_api_token: SecretStr = SecretStr("")
    mcp_bearer_token: SecretStr = SecretStr("")


class ProviderSecrets(BaseModel):
    schema_version: str = "1.0"
    default_provider: str = "dashscope"
    providers: dict[str, ProviderCredential] = Field(default_factory=dict)
    internal: InternalCredentials = Field(default_factory=InternalCredentials)

    @classmethod
    def load(cls, path: Path) -> "ProviderSecrets":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.model_validate(data)


class KernelSettings(BaseModel):
    workspace_root: Path
    data_root: Path
    provider_secrets_path: Path
    shared_volcengine_path: Path
    service_credentials_path: Path
    postgres_host: str
    postgres_port: int
    postgres_database: str
    postgres_user: str
    postgres_password: SecretStr
    api_host: str = "0.0.0.0"
    api_port: int = 8001
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8002
    python_executable: Path = Path("/home/v/miniconda3/bin/python")
    python_timeout_seconds: int = 300
    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 60
    health_heartbeat_seconds: float = 30
    health_stale_after_seconds: float = 90
    agent_timeout_seconds: int = 600
    default_codex_provider: str = "openai"
    default_codex_model: str | None = "gpt-5.6-sol"
    default_codex_reasoning_effort: str = "medium"
    default_codex_context_policy: str = "max"
    context_soft_ratio: float = 0.70
    context_hard_ratio: float = 0.85
    default_context_window_tokens: int = 258_400
    role_soft_limit_tokens: int = 16000
    role_hard_limit_tokens: int = 20000

    @classmethod
    def from_environment(cls) -> "KernelSettings":
        workspace = Path(os.getenv("AI_HOME", "/home/v/AI"))
        rag_env_path = Path(os.getenv("RAG_ENV_FILE", str(workspace / "rag-stack/.env")))
        rag_env: dict[str, Any] = {
            key: value for key, value in dotenv_values(rag_env_path).items() if value is not None
        }

        def value(name: str, default: str = "") -> str:
            return os.getenv(name, str(rag_env.get(name, default)))

        data_root = Path(os.getenv("ASSISTANT_DATA_ROOT", str(workspace / "assistant-data")))
        return cls(
            workspace_root=workspace,
            data_root=data_root,
            provider_secrets_path=Path(
                os.getenv(
                    "ASSISTANT_PROVIDER_SECRETS",
                    str(data_root / "config/provider-secrets.json"),
                )
            ),
            shared_volcengine_path=Path(
                os.getenv(
                    "ASSISTANT_SHARED_VOLCENGINE_CONFIG",
                    str(data_root / "shared-models/volcengine.json"),
                )
            ),
            service_credentials_path=Path(
                os.getenv(
                    "ASSISTANT_SERVICE_CREDENTIALS",
                    str(data_root / "config/service-credentials.json"),
                )
            ),
            postgres_host=os.getenv("ASSISTANT_POSTGRES_HOST", "rag-postgres"),
            postgres_port=int(os.getenv("ASSISTANT_POSTGRES_PORT", "5432")),
            postgres_database=value("POSTGRES_DB", "rag"),
            postgres_user=value("POSTGRES_USER", "rag_service"),
            postgres_password=SecretStr(value("POSTGRES_PASSWORD")),
            api_host=os.getenv("ASSISTANT_API_HOST", "0.0.0.0"),
            api_port=int(os.getenv("ASSISTANT_API_PORT", "8001")),
            mcp_host=os.getenv("ASSISTANT_MCP_HOST", "0.0.0.0"),
            mcp_port=int(os.getenv("ASSISTANT_MCP_PORT", "8002")),
            python_executable=Path(
                os.getenv("ASSISTANT_PYTHON_EXECUTABLE", "/home/v/miniconda3/bin/python")
            ),
            python_timeout_seconds=int(os.getenv("ASSISTANT_PYTHON_TIMEOUT_SECONDS", "300")),
            worker_poll_seconds=float(os.getenv("ASSISTANT_WORKER_POLL_SECONDS", "1")),
            worker_lease_seconds=int(os.getenv("ASSISTANT_WORKER_LEASE_SECONDS", "60")),
            agent_timeout_seconds=int(os.getenv("ASSISTANT_AGENT_TIMEOUT_SECONDS", "600")),
            default_codex_provider=os.getenv("ASSISTANT_DEFAULT_CODEX_PROVIDER", "openai"),
            default_codex_model=(
                os.getenv("ASSISTANT_DEFAULT_CODEX_MODEL", "gpt-5.6-sol").strip()
                or "gpt-5.6-sol"
            ),
            default_codex_reasoning_effort=os.getenv(
                "ASSISTANT_DEFAULT_CODEX_REASONING_EFFORT", "medium"
            ),
            default_codex_context_policy=os.getenv(
                "ASSISTANT_DEFAULT_CODEX_CONTEXT_POLICY", "max"
            ),
            context_soft_ratio=float(os.getenv("ASSISTANT_CONTEXT_SOFT_RATIO", "0.70")),
            context_hard_ratio=float(os.getenv("ASSISTANT_CONTEXT_HARD_RATIO", "0.85")),
            default_context_window_tokens=int(
                os.getenv("ASSISTANT_DEFAULT_CONTEXT_WINDOW_TOKENS", "258400")
            ),
            role_soft_limit_tokens=int(os.getenv("ASSISTANT_ROLE_SOFT_LIMIT_TOKENS", "16000")),
            role_hard_limit_tokens=int(os.getenv("ASSISTANT_ROLE_HARD_LIMIT_TOKENS", "20000")),
        )

    def load_provider_secrets(self) -> ProviderSecrets:
        secrets = ProviderSecrets.load(self.provider_secrets_path)
        dashscope = secrets.providers.get("dashscope")
        env_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if dashscope and not dashscope.api_key.get_secret_value() and env_key:
            dashscope.api_key = SecretStr(env_key)
        return secrets
