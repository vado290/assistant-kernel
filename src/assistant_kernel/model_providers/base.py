from __future__ import annotations

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
