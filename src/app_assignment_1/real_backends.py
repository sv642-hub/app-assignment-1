"""The real Qwen backends: one VLLMBackend per vLLM server, used when
APP_PROVIDER_MODE=real. Each server's --served-model-name matches `name` in
config/backends.json (compose.gpu.yml), so the config name is the model name.
"""

from contextlib import AsyncExitStack

import httpx
from pydantic import AnyHttpUrl, TypeAdapter

from app_assignment_1.config import Settings
from app_assignment_1.labels import load_labels
from app_assignment_1.schemas import BackendConfig, BackendName, PolicyCard
from app_assignment_1.vllm import VLLMBackend, build_system_prompt


def vllm_base_url(settings: Settings, name: BackendName) -> AnyHttpUrl:
    url = (
        settings.vllm_small_base_url
        if name == "qwen3-1.7b"
        else settings.vllm_large_base_url
    )
    if url is None:
        raise ValueError(f"No vLLM URL is set for {name}")
    return url


def create_vllm_http_client(
    settings: Settings,
    base_url: AnyHttpUrl,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """One client per vLLM server for the app's lifetime. The caller closes it.

    The gateway's per-attempt budget is the real limit; this timeout is a
    backstop. No retries, and no redirects, so the key only goes to the
    configured host.
    """
    if settings.vllm_api_key is None or not settings.vllm_api_key.get_secret_value():
        raise ValueError("The vLLM client needs APP_VLLM_API_KEY")
    return httpx.AsyncClient(
        base_url=str(base_url),
        headers={"Authorization": f"Bearer {settings.vllm_api_key.get_secret_value()}"},
        timeout=httpx.Timeout(settings.request_deadline_seconds),
        follow_redirects=False,
        transport=transport,
    )


async def open_vllm_backends(
    settings: Settings, stack: AsyncExitStack
) -> list[VLLMBackend]:
    """Build both backends. `stack` closes their HTTP clients on exit."""
    labels = load_labels(settings.labels_path)
    cards = TypeAdapter(dict[str, PolicyCard]).validate_json(
        settings.policy_cards_path.read_text()
    )
    configs = TypeAdapter(list[BackendConfig]).validate_json(
        settings.backends_path.read_text()
    )
    prompt = build_system_prompt(labels, cards)
    backends = []
    for config in configs:
        client = await stack.enter_async_context(
            create_vllm_http_client(settings, vllm_base_url(settings, config.name))
        )
        backends.append(VLLMBackend(config, client, config.name, prompt, labels))
    return backends
