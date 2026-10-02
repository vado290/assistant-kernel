from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class EmbeddingResult(BaseModel):
    model: str
    dimension: int
    embeddings: list[list[float]]
    provider_raw_data: dict = Field(default_factory=dict)


class RerankItem(BaseModel):
    index: int
    relevance_score: float
    document: str | None = None


class RerankResult(BaseModel):
    model: str
    results: list[RerankItem]
    provider_raw_data: dict = Field(default_factory=dict)


class RetrievalModelProvider(Protocol):
    async def embed(self, texts: list[str], dimensions: int = 1024) -> EmbeddingResult: ...

    async def rerank(
        self, query: str, documents: list[str], top_n: int | None = None
    ) -> RerankResult: ...

