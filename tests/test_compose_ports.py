"""Only Caddy publishes a port; vLLM binds only to the GPU host's private IP."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _services(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())["services"]


def test_only_proxy_publishes_a_port():
    published = {
        name for name, svc in _services("compose.yml").items() if svc.get("ports")
    }
    assert published == {"proxy"}


def test_vllm_ports_bind_only_to_the_private_ip():
    services = _services("compose.gpu.yml")
    assert set(services) == {"vllm-small", "vllm-large"}
    for name, svc in services.items():
        assert svc["ports"], name
        for port in svc["ports"]:
            # A bare "8001:8000" would bind to every interface, public included.
            assert isinstance(port, str) and port.startswith("${VLLM_BIND_IP:?"), (
                name,
                port,
            )


def test_vllm_requires_an_api_key():
    for name, svc in _services("compose.gpu.yml").items():
        key = svc["environment"]["VLLM_API_KEY"]
        # Required: an unset or empty key stops Compose instead of serving open.
        assert key.startswith("${VLLM_API_KEY:?"), (name, key)
        assert not any(arg.startswith("--api-key") for arg in svc["command"]), name


def test_vllm_names_match_backend_config():
    names = {b["name"] for b in json.loads((ROOT / "config/backends.json").read_text())}
    served = {
        arg.split("=", 1)[1]
        for svc in _services("compose.gpu.yml").values()
        for arg in svc["command"]
        if arg.startswith("--served-model-name=")
    }
    assert served == names


def test_model_cache_is_a_required_host_directory():
    for name, svc in _services("compose.gpu.yml").items():
        mounts = [v for v in svc["volumes"] if v.endswith(":/root/.cache/huggingface")]
        assert len(mounts) == 1, name
        assert mounts[0].startswith("${MODEL_CACHE_DIR:?"), (name, mounts[0])
