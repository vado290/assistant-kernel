from __future__ import annotations

from urllib.parse import urlparse

import httpx

from assistant_kernel.config.settings import ProviderCredential
from assistant_kernel.model_providers.base import (
    EmbeddingResult,
    RerankItem,
    RerankResult,
)


REGION_HOSTS = {
    "cn-beijing": "dashscope.aliyuncs.com",
    "ap-southeast-1": "dashscope-intl.aliyuncs.com",
    "us-east-1": "dashscope-us.aliyuncs.com",
}


class QwenProvider:
    def __init__(self, config: ProviderCredential, timeout_seconds: float = 60):
        self.config = config
        self.timeout_seconds = timeout_seconds

    def configured(self) -> bool:
        return bool(self.config.api_key.get_secret_value())

    def rerank_configured(self) -> bool:
        return self.configured() and bool(
            self.config.workspace_id or self.config.rerank_base_url
        )

    def _headers(self) -> dict[str, str]:
        key = self.config.api_key.get_secret_value()
        if not key:
            raise RuntimeError("DashScope API key is not configured")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def _compatible_base_url(self) -> str:
        if self.config.openai_compatible_base_url:
            return self.config.openai_compatible_base_url.rstrip("/")
        if self.config.workspace_id:
            return (
                f"https://{self.config.workspace_id}.{self.config.region}.maas.aliyuncs.com"
                "/compatible-mode/v1"
            )
        host = REGION_HOSTS.get(self.config.region)
        if not host:
            raise ValueError(f"unsupported DashScope region: {self.config.region}")
        return f"https://{host}/compatible-mode/v1"

    def _rerank_url(self) -> str:
        if self.config.rerank_base_url:
            url = self.config.rerank_base_url
        elif self.config.workspace_id:
            url = (
                f"https://{self.config.workspace_id}.{self.config.region}.maas.aliyuncs.com"
                "/compatible-api/v1/reranks"
            )
        else:
            raise RuntimeError("qwen3-rerank requires workspace_id or rerank_base_url")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("rerank_base_url must be an HTTPS URL")
        return url

    async def embed(self, texts: list[str], dimensions: int = 1024) -> EmbeddingResult:
        if not texts or not all(isinstance(item, str) and item.strip() for item in texts):
            raise ValueError("texts must contain one or more non-empty strings")
        if not 64 <= dimensions <= 2048:
            raise ValueError("text-embedding-v4 dimensions must be between 64 and 2048")
        payload = {
            "model": self.config.models.embedding,
            "input": texts,
            "dimensions": dimensions,
            "encoding_format": "float",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self._compatible_base_url()}/embeddings",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()
        ordered = sorted(raw.get("data", []), key=lambda item: item.get("index", 0))
        vectors = [item["embedding"] for item in ordered]
        if len(vectors) != len(texts) or any(len(vector) != dimensions for vector in vectors):
            raise RuntimeError("DashScope returned an unexpected embedding shape")
        return EmbeddingResult(
            model=raw.get("model", self.config.models.embedding),
            dimension=dimensions,
            embeddings=vectors,
            provider_raw_data=raw,
        )

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> RerankResult:
        if not query.strip():
            raise ValueError("query must be non-empty")
        if not 1 <= len(documents) <= 500 or not all(item.strip() for item in documents):
            raise ValueError("documents must contain 1 to 500 non-empty strings")
        if top_n is None:
            top_n = len(documents)
        if not 1 <= top_n <= len(documents):
            raise ValueError("top_n must be between 1 and the document count")
        payload = {
            "model": self.config.models.reranker,
            "query": query,
            "documents": documents,
            "top_n": top_n,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self._rerank_url(), headers=self._headers(), json=payload)
            response.raise_for_status()
            raw = response.json()
        items = []
        for item in raw.get("results", []):
            index = int(item["index"])
            items.append(
                RerankItem(
                    index=index,
                    relevance_score=float(item.get("relevance_score", item.get("score", 0))),
                    document=item.get("document") or documents[index],
                )
            )
        return RerankResult(model=self.config.models.reranker, results=items, provider_raw_data=raw)
