from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from assistant_kernel.config import KernelSettings, SharedVolcengineConfig


DEFAULT_OCR_PROMPT = (
    "Extract all visible text faithfully. Preserve headings, paragraphs, lists, tables, "
    "formulas, and reading order. Return Markdown only. Do not explain, summarize, or "
    "invent missing content."
)


class VolcengineTextResult(BaseModel):
    provider: str = "volcengine"
    model: str
    text: str
    usage: dict[str, Any] = Field(default_factory=dict)
    request_id: str = ""


class VolcengineEmbeddingResult(BaseModel):
    provider: str = "volcengine"
    model: str
    dimension: int
    embeddings: list[list[float]]
    usage: list[dict[str, Any]] = Field(default_factory=list)


class VolcengineVideoTaskResult(BaseModel):
    provider: str = "volcengine"
    model: str
    task_id: str
    status: str
    raw: dict[str, Any] = Field(default_factory=dict)


class VolcengineGateway:
    """One Kernel-facing facade over Volcengine's native Ark SDK."""

    def __init__(
        self,
        settings: KernelSettings,
        *,
        config: SharedVolcengineConfig | None = None,
        client: Any | None = None,
    ):
        self.settings = settings
        self.config = config or SharedVolcengineConfig.load(settings.shared_volcengine_path)
        self._injected_client = client

    def status(self) -> dict:
        return self.config.public_status()

    def _require_ark(self) -> None:
        if not self.config.ark.enabled:
            raise RuntimeError("shared Volcengine Ark access is disabled")
        if not self.config.ark.api_key.get_secret_value():
            raise RuntimeError("shared Volcengine ARK_API_KEY is not configured")

    @asynccontextmanager
    async def _client(self) -> AsyncIterator[Any]:
        self._require_ark()
        if self._injected_client is not None:
            yield self._injected_client
            return
        from arkruntime import AsyncArk

        async with AsyncArk(
            api_key=self.config.ark.api_key.get_secret_value(),
            base_url=self.config.ark.base_url,
            region=self.config.region,
            timeout=180,
            max_retries=2,
        ) as client:
            yield client

    @staticmethod
    def _usage(value: Any) -> dict[str, Any]:
        usage = getattr(value, "usage", None)
        if usage is None:
            return {}
        if hasattr(usage, "model_dump"):
            return usage.model_dump(mode="json", exclude_none=True)
        return dict(usage) if isinstance(usage, dict) else {}

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        model: str | None = None,
        max_tokens: int = 4096,
        thinking: str = "disabled",
    ) -> VolcengineTextResult:
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        if thinking not in {"enabled", "disabled", "auto"}:
            raise ValueError("thinking must be enabled, disabled, or auto")
        selected = model or self.config.ark.models.generation
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        async with self._client() as client:
            response = await client.chat.completions.create(
                model=selected,
                messages=messages,
                max_tokens=max_tokens,
                extra_body={"thinking": {"type": thinking}},
            )
        return VolcengineTextResult(
            model=str(getattr(response, "model", None) or selected),
            text=str(response.choices[0].message.content or ""),
            usage=self._usage(response),
            request_id=str(getattr(response, "id", "") or ""),
        )

    async def ocr(
        self,
        image_url: str,
        *,
        prompt: str = DEFAULT_OCR_PROMPT,
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> VolcengineTextResult:
        if not (image_url.startswith("https://") or image_url.startswith("data:image/")):
            raise ValueError("image_url must be HTTPS or a data:image URI")
        selected = model or self.config.ark.models.ocr
        messages = [
            {"role": "system", "content": DEFAULT_OCR_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            },
        ]
        async with self._client() as client:
            response = await client.chat.completions.create(
                model=selected,
                messages=messages,
                max_tokens=max_tokens,
                extra_body={"thinking": {"type": "disabled"}},
            )
        return VolcengineTextResult(
            model=str(getattr(response, "model", None) or selected),
            text=str(response.choices[0].message.content or ""),
            usage=self._usage(response),
            request_id=str(getattr(response, "id", "") or ""),
        )

    async def embed_texts(
        self,
        texts: list[str],
        *,
        input_type: str,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> VolcengineEmbeddingResult:
        if input_type not in {"document", "query"}:
            raise ValueError("input_type must be document or query")
        if not texts or len(texts) > 100 or not all(item.strip() for item in texts):
            raise ValueError("texts must contain 1 to 100 non-empty strings")
        selected = model or self.config.ark.models.embedding
        dimension = dimensions or self.config.ark.models.embedding_dimensions
        instruction = (
            "Target_modality: text.\nInstruction:Represent this document for semantic retrieval.\nDocument:"
            if input_type == "document"
            else "Target_modality: text.\nInstruction:Retrieve relevant documents for this query.\nQuery:"
        )
        from arkruntime.types.multimodal_embedding import MultiModalEmbeddingResponse

        vectors: list[list[float]] = []
        usage: list[dict[str, Any]] = []
        async with self._client() as client:
            for item in texts:
                response = await client.post(
                    "/embeddings/multimodal",
                    cast_to=MultiModalEmbeddingResponse,
                    body={
                        "model": selected,
                        "instructions": instruction,
                        "encoding_format": "float",
                        "dimensions": dimension,
                        "input": [{"type": "text", "text": item}],
                    },
                )
                vector = list(response.data.embedding)
                if len(vector) != dimension:
                    raise RuntimeError("Volcengine returned an unexpected embedding dimension")
                vectors.append(vector)
                usage.append(self._usage(response))
        return VolcengineEmbeddingResult(
            model=selected, dimension=dimension, embeddings=vectors, usage=usage,
        )

    async def create_video(
        self,
        prompt: str,
        *,
        model: str | None = None,
        duration: int = 5,
        ratio: str = "16:9",
        generate_audio: bool = True,
    ) -> VolcengineVideoTaskResult:
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        selected = model or self.config.ark.models.video
        async with self._client() as client:
            response = await client.content_generation.tasks.create(
                model=selected,
                content=[{"type": "text", "text": prompt}],
                duration=duration,
                ratio=ratio,
                generate_audio=generate_audio,
            )
        raw = response.model_dump(mode="json", exclude_none=True)
        return VolcengineVideoTaskResult(
            model=selected,
            task_id=str(response.id),
            status=str(getattr(response, "status", "submitted") or "submitted"),
            raw=raw,
        )

    async def get_video_task(self, task_id: str) -> VolcengineVideoTaskResult:
        if not task_id.strip():
            raise ValueError("task_id must be non-empty")
        async with self._client() as client:
            response = await client.content_generation.tasks.get(task_id=task_id)
        raw = response.model_dump(mode="json", exclude_none=True)
        return VolcengineVideoTaskResult(
            model=str(getattr(response, "model", None) or self.config.ark.models.video),
            task_id=task_id,
            status=str(getattr(response, "status", "unknown") or "unknown"),
            raw=raw,
        )
