from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from assistant_kernel.config import KernelSettings, SharedVolcengineConfig
from assistant_kernel.model_providers import VolcengineGateway


class FakeUsage:
    def model_dump(self, **_: object) -> dict:
        return {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}


class FakeResponse:
    def __init__(self, *, model: str, text: str = "ok"):
        self.model = model
        self.id = "request-1"
        self.usage = FakeUsage()
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=text))]


class FakeVideoResponse:
    def __init__(self, task_id: str, status: str, model: str):
        self.id = task_id
        self.status = status
        self.model = model

    def model_dump(self, **_: object) -> dict:
        return {"id": self.id, "status": self.status, "model": self.model}


class FakeChatCompletions:
    def __init__(self, owner: "FakeArkClient"):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.chat_requests.append(kwargs)
        return FakeResponse(model=str(kwargs["model"]), text="recognized")


class FakeVideoTasks:
    def __init__(self, owner: "FakeArkClient"):
        self.owner = owner

    async def create(self, **kwargs):
        self.owner.video_requests.append(kwargs)
        return FakeVideoResponse("task-1", "submitted", str(kwargs["model"]))

    async def get(self, *, task_id: str):
        return FakeVideoResponse(task_id, "succeeded", "doubao-seedance-2-0-fast-260128")


class FakeArkClient:
    def __init__(self):
        self.chat_requests: list[dict] = []
        self.embedding_requests: list[dict] = []
        self.video_requests: list[dict] = []
        self.chat = SimpleNamespace(completions=FakeChatCompletions(self))
        self.content_generation = SimpleNamespace(tasks=FakeVideoTasks(self))

    async def post(self, path: str, *, body: dict, **_: object):
        self.embedding_requests.append({"path": path, "body": body})
        dimension = int(body["dimensions"])
        return SimpleNamespace(
            model=body["model"], data=SimpleNamespace(embedding=[0.25] * dimension),
            usage=FakeUsage(),
        )


def configured_shared_config() -> SharedVolcengineConfig:
    return SharedVolcengineConfig.model_validate({
        "ark": {"enabled": True, "api_key": "test-secret"},
    })


def test_shared_config_template_is_secret_safe(tmp_path) -> None:
    path = tmp_path / "volcengine.json"
    path.write_text(json.dumps({"ark": {"enabled": False, "api_key": ""}}), encoding="utf-8")
    config = SharedVolcengineConfig.load(path)
    assert config.ark.models.generation == "doubao-seed-2-0-mini-260428"
    assert config.ark.models.embedding == "doubao-embedding-vision-251215"
    assert config.public_status()["ark"]["configured"] is False
    assert config.public_status()["ark"]["implemented"] is True
    assert config.public_status()["las"]["implemented"] is False
    assert config.public_status()["rerank"]["implemented"] is False
    assert "api_key" not in config.public_status()["ark"]


def test_shared_key_is_injected_only_for_volcengine_workers() -> None:
    config = configured_shared_config()
    assert config.environment_for_provider("volcengine") == {
        "ARK_API_KEY": "test-secret"
    }
    assert config.environment_for_provider("ark") == {"ARK_API_KEY": "test-secret"}
    assert config.environment_for_provider("openai") == {}


async def test_gateway_requires_an_enabled_shared_key(tmp_path) -> None:
    settings = KernelSettings.from_environment().model_copy(
        update={"shared_volcengine_path": tmp_path / "missing.json"}
    )
    with pytest.raises(RuntimeError, match="disabled"):
        await VolcengineGateway(settings).generate("hello")


async def test_gateway_uses_native_ark_shapes_without_network() -> None:
    settings = KernelSettings.from_environment()
    client = FakeArkClient()
    gateway = VolcengineGateway(settings, config=configured_shared_config(), client=client)

    generated = await gateway.generate("hello")
    ocr = await gateway.ocr("data:image/png;base64,AAAA")
    embeddings = await gateway.embed_texts(
        ["document one", "document two"], input_type="document", dimensions=256,
    )
    submitted = await gateway.create_video("a calm ocean")
    completed = await gateway.get_video_task(submitted.task_id)

    assert generated.model == "doubao-seed-2-0-mini-260428"
    assert ocr.text == "recognized"
    assert embeddings.dimension == 256
    assert len(embeddings.embeddings) == 2
    assert all(len(vector) == 256 for vector in embeddings.embeddings)
    assert client.embedding_requests[0]["path"] == "/embeddings/multimodal"
    assert "Represent this document" in client.embedding_requests[0]["body"]["instructions"]
    assert submitted.status == "submitted"
    assert completed.status == "succeeded"
    assert "test-secret" not in json.dumps(gateway.status())
