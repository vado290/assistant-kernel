from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from assistant_kernel.config import KernelSettings
from assistant_kernel.model_providers import VolcengineGateway


class GenerateRequest(BaseModel):
    prompt: str
    system_prompt: str = ""
    max_tokens: int = Field(default=4096, ge=1, le=131072)
    thinking: str = Field(default="disabled", pattern="^(enabled|disabled|auto)$")


class OcrRequest(BaseModel):
    image_url: str
    prompt: str | None = None
    max_tokens: int = Field(default=8192, ge=1, le=131072)


class EmbeddingRequest(BaseModel):
    texts: list[str] = Field(min_length=1, max_length=100)
    input_type: str = Field(pattern="^(document|query)$")
    dimensions: int | None = Field(default=None, ge=256, le=4096)


class VideoRequest(BaseModel):
    prompt: str
    duration: int = Field(default=5, ge=2, le=30)
    ratio: str = Field(default="16:9", max_length=16)
    generate_audio: bool = True


def build_volcengine_router(
    settings: KernelSettings,
    require_kernel_token: Callable,
) -> APIRouter:
    router = APIRouter(
        prefix="/v1/volcengine",
        tags=["volcengine"],
        dependencies=[Depends(require_kernel_token)],
    )

    def gateway() -> VolcengineGateway:
        return VolcengineGateway(settings)

    @router.get("/status")
    async def status() -> dict:
        return gateway().status()

    @router.post("/generate")
    async def generate(request: GenerateRequest) -> dict:
        try:
            result = await gateway().generate(
                request.prompt,
                system_prompt=request.system_prompt,
                max_tokens=request.max_tokens,
                thinking=request.thinking,
            )
            return result.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Volcengine generation failed") from exc

    @router.post("/ocr")
    async def ocr(request: OcrRequest) -> dict:
        try:
            kwargs = {"max_tokens": request.max_tokens}
            if request.prompt is not None:
                kwargs["prompt"] = request.prompt
            result = await gateway().ocr(request.image_url, **kwargs)
            return result.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Volcengine OCR failed") from exc

    @router.post("/embeddings")
    async def embeddings(request: EmbeddingRequest) -> dict:
        try:
            result = await gateway().embed_texts(
                request.texts,
                input_type=request.input_type,
                dimensions=request.dimensions,
            )
            return result.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Volcengine embedding failed") from exc

    @router.post("/video/tasks")
    async def create_video(request: VideoRequest) -> dict:
        try:
            result = await gateway().create_video(
                request.prompt,
                duration=request.duration,
                ratio=request.ratio,
                generate_audio=request.generate_audio,
            )
            return result.model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Volcengine video submission failed") from exc

    @router.get("/video/tasks/{task_id}")
    async def get_video(task_id: str) -> dict:
        try:
            return (await gateway().get_video_task(task_id)).model_dump()
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Volcengine video status failed") from exc

    return router
