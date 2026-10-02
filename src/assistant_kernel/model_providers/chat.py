from __future__ import annotations

from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from assistant_kernel.config.settings import ProviderCredential


class ChatResult(BaseModel):
    provider: str
    model: str
    text: str
    usage: dict = Field(default_factory=dict)
    request_id: str = ""


class CatalogChatProvider:
    """Unified async chat caller migrated from the legacy callModels.py catalog."""

    def __init__(self, provider_name: str, config: ProviderCredential, timeout_seconds: float = 180):
        self.provider_name = provider_name
        self.config = config
        self.timeout_seconds = timeout_seconds

    def _api_key(self) -> str:
        key = self.config.api_key.get_secret_value()
        if not self.config.enabled or not key:
            raise RuntimeError(f"provider {self.provider_name} is not configured")
        return key

    def _model_id(self, model: str) -> tuple[str, dict]:
        model_config = self.config.chat_models.get(model)
        if model_config is None:
            raise ValueError(f"unknown model {model!r} for provider {self.provider_name}")
        return str(model_config.get("endpoint_id") or model_config.get("model_id") or model), model_config

    def _base_url(self) -> str:
        base = self.config.openai_compatible_base_url or self.config.native_base_url
        if not base:
            raise RuntimeError(f"provider {self.provider_name} has no base URL")
        return base.rstrip("/")

    async def chat(
        self,
        prompt: str,
        model: str,
        system_prompt: str = "",
        max_output_tokens: int = 2048,
        temperature: float | None = None,
    ) -> ChatResult:
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        if not 1 <= max_output_tokens <= 131072:
            raise ValueError("max_output_tokens must be between 1 and 131072")
        model_id, model_config = self._model_id(model)
        provider_type = self.config.provider_type
        if provider_type == "gemini_native":
            return await self._gemini(prompt, model_id, system_prompt, max_output_tokens, temperature)
        if provider_type == "anthropic_native":
            return await self._anthropic(prompt, model_id, system_prompt, max_output_tokens, temperature)
        api_style = str(model_config.get("api_style") or "")
        if provider_type == "openai_responses" or api_style == "responses":
            return await self._responses(prompt, model_id, system_prompt, max_output_tokens, temperature)
        return await self._chat_completions(prompt, model_id, system_prompt, max_output_tokens, temperature)

    async def _responses(self, prompt: str, model: str, system: str, max_tokens: int, temperature: float | None) -> ChatResult:
        payload: dict = {"model": model, "input": prompt, "max_output_tokens": max_tokens}
        if system:
            payload["instructions"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._post_openai("/responses", payload, model, responses=True)

    async def _chat_completions(self, prompt: str, model: str, system: str, max_tokens: int, temperature: float | None) -> ChatResult:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        return await self._post_openai("/chat/completions", payload, model, responses=False)

    async def _post_openai(self, path: str, payload: dict, model: str, *, responses: bool) -> ChatResult:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url()}{path}",
                headers={"Authorization": f"Bearer {self._api_key()}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()
        if responses:
            text = str(raw.get("output_text") or "")
            if not text:
                pieces = []
                for output in raw.get("output", []):
                    for content in output.get("content", []):
                        if content.get("type") in {"output_text", "text"}:
                            pieces.append(str(content.get("text", "")))
                text = "".join(pieces)
        else:
            text = str(raw.get("choices", [{}])[0].get("message", {}).get("content") or "")
        return ChatResult(
            provider=self.provider_name,
            model=model,
            text=text,
            usage=raw.get("usage") or {},
            request_id=response.headers.get("x-request-id", ""),
        )

    async def _anthropic(self, prompt: str, model: str, system: str, max_tokens: int, temperature: float | None) -> ChatResult:
        payload: dict = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url()}/messages",
                headers={"x-api-key": self._api_key(), "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            raw = response.json()
        text = "".join(str(block.get("text", "")) for block in raw.get("content", []) if block.get("type") == "text")
        return ChatResult(provider=self.provider_name, model=model, text=text, usage=raw.get("usage") or {}, request_id=response.headers.get("request-id", ""))

    async def _gemini(self, prompt: str, model: str, system: str, max_tokens: int, temperature: float | None) -> ChatResult:
        generation: dict = {"maxOutputTokens": max_tokens}
        if temperature is not None:
            generation["temperature"] = temperature
        payload: dict = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        url = f"{self._base_url()}/models/{quote(model, safe='')}:generateContent"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(url, params={"key": self._api_key()}, json=payload)
            response.raise_for_status()
            raw = response.json()
        text = "".join(
            str(part.get("text", ""))
            for candidate in raw.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
        )
        return ChatResult(provider=self.provider_name, model=model, text=text, usage=raw.get("usageMetadata") or {}, request_id=response.headers.get("x-request-id", ""))
