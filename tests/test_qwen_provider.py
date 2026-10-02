from pydantic import SecretStr

from assistant_kernel.config.settings import ProviderCredential
from assistant_kernel.model_providers.qwen import QwenProvider


def test_qwen_workspace_urls() -> None:
    provider = QwenProvider(
        ProviderCredential(
            enabled=True,
            api_key=SecretStr("test-key"),
            workspace_id="workspace-1",
            region="cn-beijing",
        )
    )
    assert provider._compatible_base_url() == (
        "https://workspace-1.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert provider._rerank_url() == (
        "https://workspace-1.cn-beijing.maas.aliyuncs.com/compatible-api/v1/reranks"
    )

