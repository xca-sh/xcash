"""运行真实升级脚本，以隔离的 CLI 替身验证版本切换和失败恢复顺序。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

UPGRADE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upgrade.sh"

FAKE_CLI = r"""
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
with Path(os.environ["COMMAND_LOG"]).open("a") as output:
    output.write(json.dumps([tool, *args]) + "\n")

if tool == "git":
    if args != ["status", "--porcelain"]:
        sys.exit("upgrade must not update code or rely on Git history")
    if os.environ.get("DIRTY_WORKTREE") == "true":
        print(" M local-change.py")
    sys.exit(0)
if tool == "flock":
    sys.exit(0)

args = args[1:]  # docker compose
while args and args[0] in ("--env-file", "-f", "--profile"):
    args = args[2:]
command = args[0]
failure = os.environ.get("FAIL_POINT", "")

if command == "ps":
    print(os.environ["RUNNING_SERVICES"])
elif command == "build" and failure == "build":
    sys.exit(23)
elif command == "stop" and args[1:] == ["django", "worker", "worker-scan"]:
    if failure == "stop_runtime":
        sys.exit(23)
elif command == "up" and "worker" in args and failure == "start_runtime":
    sys.exit(23)
elif command == "run":
    operation = args[args.index("manage.py") + 1:]
    rehearsal = "POSTGRES_HOST=migration-rehearsal-db" in args
    if operation == ["migrate", "--plan"]:
        if os.environ["PENDING_MIGRATIONS"] == "true":
            print("chains.0001_initial")
        else:
            print("Planned operations:")
            print("  No planned migration operations.")
        if not rehearsal:
            stopped = any(
                '"stop", "beat"' in line
                for line in Path(os.environ["COMMAND_LOG"]).read_text().splitlines()
            )
            if failure == ("production_plan" if stopped else "pending_plan"):
                sys.exit(23)
    elif operation == ["migrate", "--noinput"]:
        if failure == ("rehearsal" if rehearsal else "production_migrate"):
            sys.exit(23)
    elif not rehearsal and operation == ["ensure_default_reference_data"]:
        if failure == "bootstrap":
            sys.exit(23)
"""


@pytest.fixture
def run_upgrade(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("git", "docker", "flock"):
        executable = bin_dir / tool
        executable.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
        executable.chmod(0o755)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=upgrade-test\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    log_path = tmp_path / "commands.jsonl"

    def run(*, migrations=False, quiesced=False, failure="", running=None, dirty=False):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "COMMAND_LOG": str(log_path),
            "ENV_FILE": ".env",
            "COMPOSE_FILE": "docker-compose.yml",
            "BACKUP_DIR": str(tmp_path / "backups"),
            "UPGRADE_LOCK_FILE": str(tmp_path / "upgrade.lock"),
            "ALLOW_DIRTY_UPGRADE": "false",
            "DIRTY_WORKTREE": str(dirty).lower(),
            "STOP_BEFORE_REHEARSAL": str(quiesced).lower(),
            "PENDING_MIGRATIONS": str(migrations).lower(),
            "FAIL_POINT": failure,
            "RUNNING_SERVICES": running or "django\nworker\nworker-scan\nbeat",
        }
        result = subprocess.run(  # noqa: S603 - 实际升级命令全部被隔离的 CLI 替身接管
            ["/bin/bash", str(UPGRADE_SCRIPT)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        commands = []
        for line in log_path.read_text().splitlines():
            command = json.loads(line)
            if command[:2] != ["docker", "compose"]:
                continue
            command = command[2:]
            while command[0] in ("--env-file", "-f", "--profile"):
                command = command[2:]
            commands.append(command)
        return result, commands

    return run


def command_index(commands, prefix, *, contains=None):
    return next(
        index
        for index, command in enumerate(commands)
        if command[: len(prefix)] == prefix
        and (contains is None or contains in command)
    )


@pytest.mark.parametrize(
    ("migrations", "quiesced"), [(False, False), (True, False), (True, True)]
)
def test_upgrade_switches_old_processes_before_starting_new_beat(
    run_upgrade,
    migrations,
    quiesced,
):
    result, commands = run_upgrade(migrations=migrations, quiesced=quiesced)
    assert result.returncode == 0, result.stdout + result.stderr
    build = command_index(commands, ["build"])
    stop_beat = commands.index(["stop", "beat"])
    stop_runtime = commands.index(["stop", "django", "worker", "worker-scan"])
    migrate = next(
        index
        for index, command in enumerate(commands)
        if "POSTGRES_HOST=db" in command and command[-2:] == ["migrate", "--noinput"]
    )
    start_runtime = command_index(commands, ["up"], contains="worker")
    start_beat = commands.index(["up", "-d", "--no-deps", "beat"])
    assert build < stop_beat < stop_runtime < migrate < start_runtime < start_beat
    assert commands.count(["stop", "beat"]) == 1
    assert commands.count(["stop", "django", "worker", "worker-scan"]) == 1
    rehearsal = [c for c in commands if "POSTGRES_HOST=migration-rehearsal-db" in c]
    assert bool(rehearsal) is migrations
    if migrations:
        rehearsal_index = command_index(
            commands,
            ["run"],
            contains="POSTGRES_HOST=migration-rehearsal-db",
        )
        assert (stop_beat < rehearsal_index) is quiesced


def test_build_failure_leaves_existing_services_running(run_upgrade):
    result, commands = run_upgrade(failure="build")
    assert result.returncode != 0
    assert not any(c[0] in ("stop", "start") for c in commands)


def test_pending_plan_failure_aborts_before_stopping_services(run_upgrade):
    result, commands = run_upgrade(failure="pending_plan")
    assert result.returncode != 0
    assert not any(c[0] in ("stop", "start") for c in commands)
    assert not any(c[-2:] == ["migrate", "--noinput"] for c in commands)


def test_dirty_worktree_aborts_before_docker_commands(run_upgrade):
    result, commands = run_upgrade(dirty=True)
    assert result.returncode != 0
    assert "git worktree is dirty" in result.stderr
    assert commands == []


@pytest.mark.parametrize("failure", ["production_plan", "stop_runtime"])
def test_pre_migration_failure_restores_only_previously_running_containers(
    run_upgrade, failure
):
    result, commands = run_upgrade(
        migrations=True,
        failure=failure,
        running="worker\nbeat",
    )
    assert result.returncode != 0
    assert ["start", "worker", "beat"] in commands
    assert not any(c[0] == "up" and "worker" in c for c in commands)


def test_failed_production_migration_does_not_restart_apps(run_upgrade):
    result, commands = run_upgrade(failure="production_migrate")
    assert result.returncode != 0
    assert ["stop", "beat"] in commands
    assert not any(c[0] in ("start", "up") and "worker" in c for c in commands)
    assert ["up", "-d", "--no-deps", "beat"] not in commands


def test_post_migration_recovery_starts_new_workers_before_beat(run_upgrade):
    result, commands = run_upgrade(failure="bootstrap")
    assert result.returncode != 0
    runtime = command_index(commands, ["up"], contains="worker")
    beat = commands.index(["up", "-d", "--no-deps", "beat"])
    assert runtime < beat
    assert not any(c[0] == "start" for c in commands)


def test_worker_start_failure_cannot_start_beat_even_during_cleanup(run_upgrade):
    result, commands = run_upgrade(failure="start_runtime")
    assert result.returncode != 0
    assert ["up", "-d", "--no-deps", "beat"] not in commands
