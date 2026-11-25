"""The start-at-boot units in deploy/autostart/ (README, Start at boot)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy/autostart"
PREFLIGHT = UNITS / "gpu-preflight.sh"
GPU = (
    "/usr/bin/docker compose -f compose.gpu.yml --env-file env/gpu.env "
    "-p smart-gateway-gpu"
)
CPU = "/usr/bin/docker compose -p PROJECT_NAME"
UNIT_FILES = [("smart-gateway-gpu.service", GPU), ("smart-gateway-cpu.service", CPU)]
IP = "10.1.82.72"


def unit(name: str) -> dict[str, dict[str, list[str]]]:
    """Each section's keys, each with every value in order."""
    sections: dict[str, dict[str, list[str]]] = {}
    section = ""
    for line in (UNITS / name).read_text().splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line.strip("[]")
            sections[section] = {}
        elif "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            sections[section].setdefault(key, []).append(value)
    return sections


def minutes(value: str) -> float:
    """A systemd time span such as 30s, 10min or 2h, in minutes."""
    total = 0.0
    for number, suffix in re.findall(r"(\d+)\s*(h|min|s)", value):
        total += int(number) * {"h": 60, "min": 1, "s": 1 / 60}[suffix]
    assert total, value
    return total


@pytest.mark.parametrize(("name", "compose"), UNIT_FILES)
def test_units_stop_then_start_the_stack_in_order(name, compose):
    service = unit(name)["Service"]
    assert service["WorkingDirectory"] == ["CHECKOUT_DIR"]
    # `down` first, so containers Docker restarted on its own at boot are
    # replaced by an ordered start that waits for health.
    assert service["ExecStartPre"][-1] == f"{compose} down"
    (start,) = service["ExecStart"]
    assert start.startswith(f"{compose} up -d --wait --wait-timeout ")
    # A boot start uses the images of the last deploy: it never builds or pulls.
    assert "--no-build" in start.split() and "--pull never" in start
    assert "--build" not in start.split()
    assert service["ExecStop"] == [f"{compose} down"]


@pytest.mark.parametrize(("name", "compose"), UNIT_FILES)
def test_units_start_at_boot_and_stay_up(name, compose):
    sections = unit(name)
    service = sections["Service"]
    # A oneshot that stays "active (exited)": without RemainAfterExit, systemd
    # would run ExecStop (`down`) as soon as `up` returns.
    assert service["Type"] == ["oneshot"]
    assert service["RemainAfterExit"] == ["yes"]
    assert service["Restart"] == ["on-failure"]
    assert "docker.service" in sections["Unit"]["Requires"][0].split()
    assert "docker.service" in sections["Unit"]["After"][0].split()
    assert sections["Install"]["WantedBy"] == ["multi-user.target"]


@pytest.mark.parametrize(("name", "compose"), UNIT_FILES)
def test_units_give_up_after_a_few_tries(name, compose):
    # systemd counts starts inside StartLimitIntervalSec. If one try can take
    # longer than interval / burst, slow failures each start a new window and
    # it retries forever, reloading the models every time.
    sections = unit(name)
    burst = int(sections["Unit"]["StartLimitBurst"][0])
    interval = minutes(sections["Unit"]["StartLimitIntervalSec"][0])
    service = sections["Service"]
    one_try = minutes(service["TimeoutStartSec"][0]) + minutes(service["RestartSec"][0])
    assert 2 <= burst <= 5
    assert interval > burst * one_try
    # `up` gives up on its own before systemd kills it.
    wait = int(service["ExecStart"][0].split("--wait-timeout ")[1].split()[0])
    assert wait / 60 < minutes(service["TimeoutStartSec"][0])


def test_gpu_unit_matches_the_readme_commands():
    readme = (ROOT / "README.md").read_text()
    assert GPU.removeprefix("/usr/bin/") + " up -d" in readme
    assert "reset-failed" in readme
    # The CPU stack runs as app-assignment-1 (docs/DEPLOYMENT.md); a README
    # command with another name would start a second stack on port 8080.
    assert "-p smart-gateway-prototype" not in readme


def test_gpu_unit_checks_the_host_first():
    service = unit("smart-gateway-gpu.service")["Service"]
    assert service["ExecStartPre"][0] == f"CHECKOUT_DIR/{PREFLIGHT.relative_to(ROOT)}"
    assert PREFLIGHT.stat().st_mode & 0o111, "gpu-preflight.sh must be executable"
    # It reads only two settings, so the API key never reaches the log.
    assert "VLLM_API_KEY" not in PREFLIGHT.read_text()


# --- The preflight itself, run against stubbed `ip` and `nvidia-smi` ---

needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs sh")
MODELS = re.findall(
    r"--model=(\S+)\n\s*- --revision=(\S+)", (ROOT / "compose.gpu.yml").read_text()
)


def add_model(cache: Path, model: str, revision: str, shards: int = 2) -> Path:
    """A Hugging Face cache entry as vLLM leaves it: snapshot files linking
    into blobs/."""
    repo = cache / "hub" / f"models--{model.replace('/', '--')}"
    snapshot = repo / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (repo / "blobs").mkdir()
    names = [f"model-{i:05d}-of-{shards:05d}.safetensors" for i in range(1, shards + 1)]
    index = {"weight_map": {f"layer.{i}": name for i, name in enumerate(names)}}
    files = {
        "config.json": "{}",
        "tokenizer.json": "{}",
        "model.safetensors.index.json": json.dumps(index),
    } | {name: "weights" for name in names}
    for number, (name, body) in enumerate(files.items()):
        blob = repo / "blobs" / f"blob{number}"
        blob.write_text(body)
        (snapshot / name).symlink_to(Path("../../blobs") / blob.name)
    return snapshot


@pytest.fixture
def host(tmp_path):
    """A checkout and a healthy host; tests break one thing each."""
    checkout, cache, bin_dir = (
        tmp_path / "checkout",
        tmp_path / "cache",
        tmp_path / "bin",
    )
    (checkout / "env").mkdir(parents=True)
    shutil.copy(ROOT / "compose.gpu.yml", checkout)
    for model, revision in MODELS:
        add_model(cache, model, revision)
    (checkout / "env/gpu.env").write_text(
        f"VLLM_BIND_IP={IP}\nMODEL_CACHE_DIR={cache}\nVLLM_API_KEY=secret\n"
    )
    bin_dir.mkdir()
    stubs = {
        "ip": f'echo "2: ens3    inet {IP}/24 brd 10.1.82.255 scope global ens3"',
        "nvidia-smi": 'echo "GPU 0: GRID A100X-20C"',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)

    class Host:
        def __init__(self):
            self.checkout, self.cache, self.bin = checkout, cache, bin_dir

        def env(self, text):
            (checkout / "env/gpu.env").write_text(text)

        def stub(self, name, body):
            (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")

        def run(self):
            path = f"{bin_dir}:/usr/bin:/bin"
            return subprocess.run(
                ["sh", str(PREFLIGHT)],
                cwd=checkout,
                env={"PATH": path, "PREFLIGHT_WAIT": "0"},
                capture_output=True,
                text=True,
            )

    return Host()


def test_compose_names_both_pinned_models():
    assert [model for model, _ in MODELS] == ["Qwen/Qwen3-1.7B", "Qwen/Qwen3-4B"]
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in MODELS)


@needs_sh
def test_preflight_passes_on_a_ready_host(host):
    result = host.run()
    assert (result.returncode, result.stderr) == (0, "")


@needs_sh
@pytest.mark.parametrize(
    "line",
    [
        f"VLLM_BIND_IP={IP}\r",  # CRLF line endings
        f"export VLLM_BIND_IP={IP}",
        f"VLLM_BIND_IP='{IP}'",
        f'VLLM_BIND_IP="{IP}"  # the private IP',
        f"VLLM_BIND_IP={IP} # the private IP",
        f"VLLM_BIND_IP = {IP}",
    ],
)
def test_preflight_reads_the_env_file_as_compose_does(host, line):
    host.env(f"VLLM_BIND_IP=1.2.3.4\n{line}\nMODEL_CACHE_DIR={host.cache}\n")
    result = host.run()
    assert (result.returncode, result.stderr) == (0, "")


@needs_sh
@pytest.mark.parametrize(
    ("env", "message"),
    [
        ("MODEL_CACHE_DIR={cache}\n", "VLLM_BIND_IP is not set"),
        ("VLLM_BIND_IP=10.1.82\nMODEL_CACHE_DIR={cache}\n", "not an IPv4 address"),
        (
            "VLLM_BIND_IP=10.1.82.7\nMODEL_CACHE_DIR={cache}\n",
            "VLLM_BIND_IP=10.1.82.7 is not an address of this host",
        ),
        (f"VLLM_BIND_IP={IP}\n", "MODEL_CACHE_DIR is not set"),
    ],
)
def test_preflight_names_a_bad_setting(host, env, message):
    host.env(env.format(cache=host.cache))
    result = host.run()
    assert result.returncode == 1
    assert message in result.stderr
    assert "secret" not in result.stderr + result.stdout


@needs_sh
def test_preflight_needs_the_env_file(host):
    (host.checkout / "env/gpu.env").unlink()
    result = host.run()
    assert result.returncode == 1 and "env/gpu.env is missing" in result.stderr


def remove_cache(host):
    shutil.rmtree(host.cache)


def empty_hub(host):
    shutil.rmtree(host.cache / "hub")
    (host.cache / "hub").mkdir()


def one_model_missing(host):
    shutil.rmtree(host.cache / "hub/models--Qwen--Qwen3-4B")


def other_revision_only(host):
    snapshots = host.cache / "hub/models--Qwen--Qwen3-4B/snapshots"
    (snapshots / MODELS[1][1]).rename(snapshots / ("0" * 40))


def shard_missing(host):
    snapshot = host.cache / f"hub/models--Qwen--Qwen3-1.7B/snapshots/{MODELS[0][1]}"
    (snapshot / "model-00002-of-00002.safetensors").unlink()


def blob_missing(host):
    # A snapshot link whose blob is gone, as after a cleanup.
    snapshot = host.cache / f"hub/models--Qwen--Qwen3-4B/snapshots/{MODELS[1][1]}"
    (snapshot / "config.json").resolve().unlink()


def download_unfinished(host):
    blobs = host.cache / "hub/models--Qwen--Qwen3-4B/blobs"
    (blobs / "abc123.incomplete").write_text("part")


@needs_sh
@pytest.mark.parametrize(
    ("breakage", "message"),
    [
        (remove_cache, "has no hub/ folder: is the volume mounted?"),
        (empty_hub, "Qwen/Qwen3-1.7B at"),
        (one_model_missing, "Qwen/Qwen3-4B at"),
        (other_revision_only, "Qwen/Qwen3-4B at"),
        (shard_missing, "missing model-00002-of-00002.safetensors"),
        (blob_missing, "Qwen/Qwen3-4B at"),
        (download_unfinished, "Qwen/Qwen3-4B has an unfinished download"),
    ],
)
def test_preflight_needs_both_pinned_models_in_full(host, breakage, message):
    breakage(host)
    result = host.run()
    assert result.returncode == 1
    assert message in result.stderr


@needs_sh
def test_preflight_needs_the_gpu(host):
    host.stub("nvidia-smi", "exit 9")
    result = host.run()
    assert result.returncode == 1 and "cannot see the GPU" in result.stderr


@needs_sh
def test_preflight_waits_for_what_comes_up_after_boot(host):
    # The IP appears on the second check.
    flag = host.bin / "seen"
    host.stub(
        "ip",
        f'if [ -e {flag} ]; then echo "inet {IP}/24"; else touch {flag}; fi',
    )
    result = subprocess.run(
        ["sh", str(PREFLIGHT)],
        cwd=host.checkout,
        env={
            "PATH": f"{host.bin}:/usr/bin:/bin",
            "PREFLIGHT_WAIT": "5",
            "PREFLIGHT_STEP": "1",
        },
        capture_output=True,
        text=True,
    )
    assert (result.returncode, result.stderr) == (0, "")
