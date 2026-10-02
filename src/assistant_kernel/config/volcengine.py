from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class ArkModels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: str = "doubao-seed-2-0-mini-260428"
    ocr: str = "doubao-seed-2-0-mini-260428"
    embedding: str = "doubao-embedding-vision-251215"
    video: str = "doubao-seedance-2-0-fast-260128"
    embedding_dimensions: int = Field(default=1024, ge=256, le=4096)


class ArkCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    models: ArkModels = Field(default_factory=ArkModels)


class LasCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    api_key: SecretStr = SecretStr("")
    endpoint: str = "https://operator.las.cn-beijing.volces.com/api/v1"
    primary_resource: str = "seedasr"
    backup_resource: str = "bigasr"


class VikingRerankCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    access_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    endpoint: str = "https://api-knowledgebase.mlp.cn-beijing.volces.com"
    region: str = "cn-north-1"
    primary_model: str = "doubao-seed-rerank"
    backup_model: str = "base-multilingual-rerank"


class SharedVolcengineConfig(BaseModel):
    """Shared Kernel credential file; deliberately not Project-scoped."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "shared-volcengine-models.v1"
    provider: str = "volcengine"
    region: str = "cn-beijing"
    ark: ArkCredential = Field(default_factory=ArkCredential)
    las: LasCredential = Field(default_factory=LasCredential)
    rerank: VikingRerankCredential = Field(default_factory=VikingRerankCredential)

    @classmethod
    def load(cls, path: Path) -> "SharedVolcengineConfig":
        if not path.is_file():
            return cls()
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8-sig")))

    def public_status(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "region": self.region,
            "ark": {
                "enabled": self.ark.enabled,
                "configured": bool(self.ark.api_key.get_secret_value()),
                "implemented": True,
                "base_url": self.ark.base_url,
                "models": self.ark.models.model_dump(),
                "capabilities": {
                    "generation": True,
                    "ocr": True,
                    "embedding": True,
                    "video": True,
                },
            },
            "las": {
                "enabled": self.las.enabled,
                "configured": bool(self.las.api_key.get_secret_value()),
                "implemented": False,
                "primary_resource": self.las.primary_resource,
                "backup_resource": self.las.backup_resource,
            },
            "rerank": {
                "enabled": self.rerank.enabled,
                "configured": bool(
                    self.rerank.access_key.get_secret_value()
                    and self.rerank.secret_key.get_secret_value()
                ),
                "implemented": False,
                "primary_model": self.rerank.primary_model,
                "backup_model": self.rerank.backup_model,
            },
        }

    def environment_for_provider(self, provider: str | None) -> dict[str, str]:
        """Inject only the shared key required by a Volcengine-backed worker."""
        normalized = str(provider or "").strip().lower()
        if normalized not in {"volcengine", "ark", "doubao"}:
            return {}
        key = self.ark.api_key.get_secret_value()
        if not self.ark.enabled or not key:
            return {}
        return {"ARK_API_KEY": key}
