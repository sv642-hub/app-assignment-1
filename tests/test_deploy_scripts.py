"""deploy/deploy.sh, backup.sh and restore.sh against a fake `docker` and `curl`.

The fakes record every call in a log file, so the tests check what the scripts
ask Docker to do, and in what order, without a Docker daemon or a network.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("flock") is None,
    reason="the deploy scripts need bash and flock",
)

FAKE_DOCKER = '''#!/usr/bin/env python3
"""A stand-in for docker. Behaviour comes from environment variables."""
import hashlib, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    tag = os.environ.get("IMAGE_TAG", "")
    log.write(" ".join(args) + " | IMAGE_TAG=" + tag + "\\n")
line = " ".join(args)
if args[0] == "inspect":
    print("true")
elif args[:2] == ["image", "inspect"]:
    ref = args[-1]
    print("ghcr.io/x/y@sha256:" + hashlib.sha256(ref.encode()).hexdigest()[:12])
elif args[0] == "compose":
    if " ps -q postgres" in line:
        if os.environ.get("FAKE_NO_POSTGRES"):
            sys.exit(0)
        print("pgid")
    elif " pull " in line + " ":
        if os.environ.get("FAKE_PULL_FAIL", "-") == os.environ.get("IMAGE_TAG"):
            sys.exit(1)
    elif " up " in line:
        if os.environ.get("FAKE_UP_FAIL", "-") == os.environ.get("IMAGE_TAG"):
            sys.exit(1)
    elif "pg_dump" in line:
        sys.stdout.write("" if os.environ.get("FAKE_EMPTY_DUMP") else "PGDMP-bytes")
    elif "pg_restore --list" in line:
        sys.stdin.read()
        print("; entry 1\\n; entry 2")
    elif "pg_restore" in line:
        sys.stdin.read()
        if os.environ.get("FAKE_RESTORE_FAIL"):
            sys.exit(1)
    elif "psql" in line:
        sys.stdin.read()
        print("requests\\t3\\nschema_migrations\\t8")
'''

FAKE_CURL = """#!/usr/bin/env bash
echo "curl $*" >>"$FAKE_LOG"
[ -z "${FAKE_NOT_READY:-}" ]
"""

FAKE_SCP = """#!/usr/bin/env bash
echo "scp $*" >>"$FAKE_LOG"
[ -z "${FAKE_SCP_FAIL:-}" ]
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """A PATH with the fakes first, and every state directory under tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in [
        ("docker", FAKE_DOCKER),
        ("curl", FAKE_CURL),
        ("scp", FAKE_SCP),
        ("sleep", "#!/usr/bin/env bash\nexit 0\n"),
    ]:
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "DEPLOY_LOG": str(tmp_path / "deploy.log"),
        "STATE_DIR": str(tmp_path / "state"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "DEPLOY_GOLDEN_CMD": "true",
    }


def run(script: str, env: dict[str, str], *args: str, **extra: str):
    return subprocess.run(
        ["bash", str(ROOT / "deploy" / script), *args],
        env={**env, **extra},
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def calls(env: dict[str, str]) -> list[str]:
    path = Path(env["FAKE_LOG"])
    return path.read_text().splitlines() if path.exists() else []


def backup_log(env: dict[str, str]) -> list[str]:
    """The backup lines of the deploy log."""
    path = Path(env["DEPLOY_LOG"])
    return [ln for ln in path.read_text().splitlines() if "action=backup" in ln]


def deploy_log(env: dict[str, str]) -> list[str]:
    """The deploy and rollback lines (backups write their own lines too)."""
    path = Path(env["DEPLOY_LOG"])
    lines = path.read_text().splitlines() if path.exists() else []
    return [ln for ln in lines if "action=backup" not in ln]


def state(env: dict[str, str], name: str) -> str:
    path = Path(env["STATE_DIR"]) / name
    return path.read_text().strip() if path.exists() else ""


# --- deploy ---------------------------------------------------------------


def test_deploy_records_digests_and_the_deploy_log(env):
    result = run("deploy.sh", env, "v1.0.0")
    assert result.returncode == 0, result.stderr
    (line,) = deploy_log(env)
    assert "action=deploy version=v1.0.0 previous=none" in line
    assert "gateway=sha256:" in line and "proxy=sha256:" in line
    assert line.endswith("result=ok")
    assert state(env, "current") == "v1.0.0"
    log = "\n".join(calls(env))
    # Pulled and started the published images, never built.
    assert "pull gateway proxy | IMAGE_TAG=v1.0.0" in log
    assert "--no-build --pull never | IMAGE_TAG=v1.0.0" in log
    assert "compose.release.yml" in log
    assert "/v1/ready" in log


def test_deploy_backs_up_before_it_pulls(env):
    run("deploy.sh", env, "v1.0.0")
    log = calls(env)
    dump = next(i for i, c in enumerate(log) if "pg_dump" in c)
    pull = next(i for i, c in enumerate(log) if " pull " in c)
    assert dump < pull
    (backup,) = Path(env["BACKUP_DIR"]).glob("gateway-*-pre-deploy.dump")
    assert backup.read_text() == "PGDMP-bytes"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backup.name in "\n".join(deploy_log(env))


def test_first_deploy_has_nothing_to_back_up(env):
    result = run("deploy.sh", env, "v1.0.0", FAKE_NO_POSTGRES="1")
    assert result.returncode == 0, result.stderr
    assert not any("pg_dump" in c for c in calls(env))
    assert "backup=none" in deploy_log(env)[0]


def test_a_failed_backup_stops_the_deploy_before_anything_changes(env):
    result = run("deploy.sh", env, "v1.0.0", FAKE_EMPTY_DUMP="1")
    assert result.returncode != 0
    assert not any(" pull " in c or " up " in c for c in calls(env))
    assert state(env, "current") == ""


def test_a_second_deploy_keeps_the_previous_version(env):
    run("deploy.sh", env, "v1.0.0")
    run("deploy.sh", env, "v1.1.0")
    assert state(env, "current") == "v1.1.0"
    assert state(env, "previous") == "v1.0.0"
    assert "previous=v1.0.0" in deploy_log(env)[-1]


def test_rolling_back_is_the_same_script_with_an_older_version(env):
    run("deploy.sh", env, "v1.0.0")
    run("deploy.sh", env, "v1.1.0")
    result = run("deploy.sh", env, "previous")
    assert result.returncode == 0, result.stderr
    assert state(env, "current") == "v1.0.0"
    assert "version=v1.0.0 previous=v1.1.0" in deploy_log(env)[-1]


def test_a_failed_start_rolls_back_to_the_previous_version(env):
    run("deploy.sh", env, "v1.0.0")
    result = run("deploy.sh", env, "v2.0.0", FAKE_UP_FAIL="v2.0.0")
    assert result.returncode != 0
    assert "v1.0.0 is running again" in result.stderr
    lines = deploy_log(env)
    assert "version=v2.0.0" in lines[-2] and "result=failed reason=start" in lines[-2]
    assert "action=rollback version=v1.0.0 from=v2.0.0" in lines[-1]
    assert lines[-1].endswith("result=ok")
    # The failed version was never recorded as current.
    assert state(env, "current") == "v1.0.0"


def test_a_failed_golden_set_rolls_back_too(env):
    run("deploy.sh", env, "v1.0.0")
    result = run("deploy.sh", env, "v2.0.0", DEPLOY_GOLDEN_CMD="false")
    assert result.returncode != 0
    assert "reason=golden" in deploy_log(env)[-2]
    assert state(env, "current") == "v1.0.0"


def test_not_ready_counts_as_a_failure(env):
    result = run("deploy.sh", env, "v1.0.0", FAKE_NOT_READY="1", READY_WAIT="3")
    assert result.returncode != 0
    assert "reason=start" in deploy_log(env)[0]
    assert state(env, "current") == ""


def test_no_rollback_leaves_the_failed_version_running(env):
    run("deploy.sh", env, "v1.0.0")
    result = run("deploy.sh", env, "v2.0.0", "--no-rollback", FAKE_UP_FAIL="v2.0.0")
    assert result.returncode != 0
    assert not any("action=rollback" in line for line in deploy_log(env))


def test_a_failed_pull_changes_nothing(env):
    run("deploy.sh", env, "v1.0.0")
    result = run("deploy.sh", env, "v9", FAKE_PULL_FAIL="v9")
    assert result.returncode != 0
    assert "nothing was changed" in result.stderr
    assert not any("IMAGE_TAG=v9" in c and " up " in c for c in calls(env))
    assert state(env, "current") == "v1.0.0"


@pytest.mark.parametrize("bad", ["../etc", "v1;rm", "-x", "a b", ""])
def test_a_bad_version_is_refused(env, bad):
    result = run("deploy.sh", env, bad)
    assert result.returncode != 0
    assert calls(env) == []


def test_previous_with_no_history_is_an_error(env):
    result = run("deploy.sh", env, "previous")
    assert result.returncode != 0
    assert "no previous version" in result.stderr


# --- backup ---------------------------------------------------------------


def test_backup_checks_the_file_and_warns_without_a_remote(env):
    result = run("backup.sh", env)
    assert result.returncode == 0, result.stderr
    assert "WARNING: BACKUP_REMOTE is not set" in result.stdout
    (backup,) = Path(env["BACKUP_DIR"]).glob("gateway-*.dump")
    assert result.stdout.strip().splitlines()[-1] == str(backup)
    assert "copied_off_instance=no" in backup_log(env)[0]
    assert not list(Path(env["BACKUP_DIR"]).glob("*.partial"))


def test_backup_copies_off_the_instance_with_scp(env):
    result = run("backup.sh", env, BACKUP_REMOTE="me@host:backups")
    assert result.returncode == 0, result.stderr
    scp = next(c for c in calls(env) if c.startswith("scp"))
    assert "BatchMode=yes" in scp and scp.endswith("me@host:backups/")
    assert "copied_off_instance=yes" in backup_log(env)[0]


def test_backup_fails_loudly_when_the_copy_fails(env):
    result = run("backup.sh", env, BACKUP_REMOTE="me@host:b", FAKE_SCP_FAIL="1")
    assert result.returncode != 0
    assert "the local backup is at" in result.stderr


def test_backup_rejects_an_empty_dump_and_keeps_no_file(env):
    result = run("backup.sh", env, FAKE_EMPTY_DUMP="1")
    assert result.returncode != 0
    assert list(Path(env["BACKUP_DIR"]).iterdir()) == []


def test_backup_keeps_only_the_newest_files(env):
    backups = Path(env["BACKUP_DIR"])
    backups.mkdir()
    for n in range(5):
        old = backups / f"gateway-2020010{n}T000000Z.dump"
        old.write_text("x")
        os.utime(old, (1_000_000 + n, 1_000_000 + n))
    result = run("backup.sh", env, BACKUP_KEEP="3")
    assert result.returncode == 0, result.stderr
    kept = sorted(p.name for p in backups.glob("gateway-*.dump"))
    assert len(kept) == 3
    assert "gateway-20200100T000000Z.dump" not in kept


def test_backup_label_is_limited_to_safe_characters(env):
    assert run("backup.sh", env, "--label", "a/b").returncode != 0
    assert run("backup.sh", env, "--label", "pre-deploy").returncode == 0


def test_backup_needs_a_running_database(env):
    result = run("backup.sh", env, FAKE_NO_POSTGRES="1")
    assert result.returncode != 0
    assert "not running" in result.stderr


# --- restore --------------------------------------------------------------


@pytest.fixture
def dump(tmp_path: Path) -> Path:
    path = tmp_path / "gateway-test.dump"
    path.write_text("PGDMP-bytes")
    return path


def test_restore_refuses_without_yes(env, dump):
    result = run("restore.sh", env, str(dump))
    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert not any("pg_restore" in c for c in calls(env))


def test_restore_verify_uses_a_scratch_database_and_drops_it(env, dump):
    result = run("restore.sh", env, "--verify", str(dump))
    assert result.returncode == 0, result.stderr
    log = "\n".join(calls(env))
    assert "createdb" in log and "restore_check_" in log and "dropdb" in log
    # The live database is never the target of a verify.
    assert "--clean" not in log
    assert "schema_migrations" in result.stdout
    assert "action=restore-verify" in deploy_log(env)[0]


def test_live_restore_takes_a_safety_backup_and_stops_the_gateway_first(env, dump):
    result = run("restore.sh", env, str(dump), "--yes")
    assert result.returncode == 0, result.stderr
    log = calls(env)
    dumped = next(i for i, c in enumerate(log) if "pg_dump" in c)
    stopped = next(i for i, c in enumerate(log) if " stop " in c + " ")
    restored = next(
        i
        for i, c in enumerate(log)
        if "pg_restore" in c and "--list" not in c and "--clean" in c
    )
    assert dumped < stopped < restored
    assert "--single-transaction" in log[restored]
    assert list(Path(env["BACKUP_DIR"]).glob("*-pre-restore.dump"))
    assert deploy_log(env)[-1].endswith("result=ok")


def test_a_failed_restore_starts_the_old_stack_and_names_the_safety_backup(env, dump):
    result = run("restore.sh", env, str(dump), "--yes", FAKE_RESTORE_FAIL="1")
    assert result.returncode != 0
    assert "the database is as it was" in result.stderr
    assert any(" start " in c + " " for c in calls(env))
    assert "result=failed" in deploy_log(env)[-1]


def test_restore_needs_an_existing_file(env, tmp_path):
    result = run("restore.sh", env, str(tmp_path / "nope.dump"), "--yes")
    assert result.returncode != 0
    assert "no such file" in result.stderr


# --- compose files --------------------------------------------------------


def _load(name: str) -> dict:
    # Compose's !override tag is not plain YAML; drop it to read the rest.
    return yaml.safe_load((ROOT / name).read_text().replace("!override", ""))


def test_https_override_publishes_only_80_and_443():
    text = (ROOT / "compose.https.yml").read_text()
    assert "ports: !override" in text
    proxy = _load("compose.https.yml")["services"]["proxy"]
    assert proxy["ports"] == ["80:80", "443:443"]
    # Certificates must survive a container recreate.
    assert "caddy-data:/data" in proxy["volumes"]
    assert "caddy-data" in _load("compose.https.yml")["volumes"]
    # Only the proxy is touched: no other service gains a port.
    assert set(_load("compose.https.yml")["services"]) == {"proxy"}


def test_caddy_site_address_comes_from_the_environment():
    caddyfile = (ROOT / "Caddyfile").read_text()
    # Unset it is plain :8080, so local runs and CI are unchanged.
    assert "{$SITE_ADDRESS::8080} {" in caddyfile


def test_release_override_names_the_gateway_and_proxy_images():
    services = _load("compose.release.yml")["services"]
    assert set(services) == {"gateway", "proxy"}
    for name, service in services.items():
        assert service["image"].startswith("${IMAGE_REPO:-ghcr.io/")
        assert f"/{name}:${{IMAGE_TAG:?" in service["image"]


def test_deploy_env_example_lists_every_setting_the_scripts_read():
    example = (ROOT / "env/deploy.env.example").read_text()
    for key in [
        "SITE_ADDRESS",
        "IMAGE_REPO",
        "COMPOSE_PROJECT_NAME",
        "DEPLOY_LOG",
        "BACKUP_DIR",
        "BACKUP_KEEP",
        "BACKUP_REMOTE",
        "BACKUP_SCP_OPTS",
    ]:
        assert f"{key}=" in example, key
    # No real secret or address: templates only.
    assert json.dumps(example).count("password") == 0
