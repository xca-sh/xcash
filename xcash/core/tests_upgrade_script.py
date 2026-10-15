"""运行真实升级脚本，以隔离的 CLI 替身验证版本切换和失败恢复顺序。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

UPGRADE_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upgrade.sh"
START_SCRIPT = UPGRADE_SCRIPT.parent.parent / "compose/production/django/start"

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
if tool == "timeout":
    # 去掉超时秒数，原样执行被包裹的命令（仍会走到这里的 docker 替身）。
    os.execvp(args[1], args[1:])

args = args[1:]  # docker compose
compose_files = []
while args and args[0] in ("--env-file", "-f", "--profile"):
    if args[0] == "-f":
        compose_files.append(args[1])
    args = args[2:]
command = args[0]
failure = os.environ.get("FAIL_POINT", "")

if command == "up" and "worker" in args:
    # 成功准备后的启动必须真正覆盖 /start，否则 Web 仍会重复迁移和初始化。
    assert len(compose_files) == 2
    assert 'command: ["/start", "--prepared"]' in Path(compose_files[-1]).read_text()

if command == "ps":
    print(os.environ["RUNNING_SERVICES"])
elif command == "pull" and failure == "pull":
    sys.exit(23)
elif command == "stop":
    if failure == "stop_workers" and args[1:] == ["worker", "worker-scan"]:
        sys.exit(23)
    if failure == "stop_django" and args[1:] == ["django"]:
        sys.exit(23)
elif command == "up" and "worker" in args and failure == "start_runtime":
    sys.exit(23)
elif command in ("run", "exec") and "manage.py" in args:
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
    elif not rehearsal and operation[:1] == ["bootstrap_runtime"]:
        if "--skip-migrations" not in operation and failure == "production_migrate":
            sys.exit(1)
        if failure == "bootstrap_interrupted":
            sys.exit(137)
        attempts = sum(
            '"bootstrap_runtime"' in line
            for line in Path(os.environ["COMMAND_LOG"]).read_text().splitlines()
        )
        if failure == "bootstrap" or (failure == "bootstrap_once" and attempts == 1):
            sys.exit(20)
    elif operation[:1] == ["wait_for_runtime"]:
        if failure == "ready_" + operation[operation.index("--phase") + 1]:
            sys.exit(23)
"""


@pytest.fixture
def run_upgrade(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("git", "docker", "flock", "timeout"):
        executable = bin_dir / tool
        executable.write_text(f"#!{sys.executable}\n{FAKE_CLI}")
        executable.chmod(0o755)
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=upgrade-test\n")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    log_path = tmp_path / "commands.jsonl"
    log_path.touch()

    def run(
        *,
        migrations=False,
        quiesced=False,
        failure="",
        running=None,
        dirty=False,
        ready_timeout="360",
    ):
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
            "APP_READY_TIMEOUT": ready_timeout,
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
    pull = command_index(commands, ["pull"])
    stop_beat = commands.index(["stop", "beat"])
    stop_workers = commands.index(["stop", "worker", "worker-scan"])
    stop_django = commands.index(["stop", "django"])
    start_runtime = command_index(commands, ["up"], contains="worker")
    start_beat = commands.index(["up", "-d", "--no-deps", "beat"])
    consumers_ready = command_index(commands, ["exec"], contains="consumers")
    scheduler_ready = command_index(commands, ["exec"], contains="scheduler")
    reload_caddy = command_index(commands, ["exec"], contains="reload")
    bootstrap = command_index(commands, ["run"], contains="bootstrap_runtime")
    assert (
        pull
        < stop_beat
        < stop_workers
        < stop_django
        < bootstrap
        < start_runtime
        < start_beat
    )
    assert start_runtime < consumers_ready < start_beat < scheduler_ready
    # Caddyfile 有误时 reload 会失败，放在最后才不会拖住 Beat 等业务进程的启动。
    assert scheduler_ready < reload_caddy
    assert commands.count(["stop", "beat"]) == 1
    assert commands.count(["stop", "worker", "worker-scan"]) == 1
    assert commands.count(["stop", "django"]) == 1
    assert sum("bootstrap_runtime" in c for c in commands) == 1
    assert commands[bootstrap][-1] == "bootstrap_runtime"
    assert not any(
        "POSTGRES_HOST=db" in c and c[-2:] == ["migrate", "--noinput"] for c in commands
    )
    assert not any(c[0] == "run" and "wait_for_runtime" in c for c in commands)
    rehearsal = [c for c in commands if "POSTGRES_HOST=migration-rehearsal-db" in c]
    assert bool(rehearsal) is migrations
    if migrations:
        rehearsal_index = command_index(
            commands,
            ["run"],
            contains="POSTGRES_HOST=migration-rehearsal-db",
        )
        assert (stop_beat < rehearsal_index) is quiesced


def test_pull_failure_leaves_existing_services_running(run_upgrade):
    result, commands = run_upgrade(failure="pull")
    assert result.returncode != 0
    assert not any(c[0] in ("stop", "start") for c in commands)


def test_pending_plan_failure_aborts_before_stopping_services(run_upgrade):
    result, commands = run_upgrade(failure="pending_plan")
    assert result.returncode != 0
    assert not any(c[0] in ("stop", "start") for c in commands)
    assert not any("bootstrap_runtime" in c for c in commands)
    assert not any(c[-2:] == ["migrate", "--noinput"] for c in commands)


def test_dirty_worktree_aborts_before_docker_commands(run_upgrade):
    result, commands = run_upgrade(dirty=True)
    assert result.returncode != 0
    assert "git worktree is dirty" in result.stderr
    assert commands == []


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_readiness_timeout_aborts_before_touching_services(run_upgrade, value):
    result, commands = run_upgrade(ready_timeout=value)
    assert result.returncode != 0
    assert commands == []


@pytest.mark.parametrize("failure", ["production_plan", "stop_workers", "stop_django"])
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


@pytest.mark.parametrize("failure", ["production_migrate", "bootstrap_interrupted"])
def test_failed_or_unconfirmed_migration_does_not_restart_apps(run_upgrade, failure):
    result, commands = run_upgrade(failure=failure)
    assert result.returncode != 0
    assert ["stop", "beat"] in commands
    assert not any(c[0] in ("start", "up") and "worker" in c for c in commands)
    assert ["up", "-d", "--no-deps", "beat"] not in commands


def test_post_migration_recovery_retries_setup_before_starting_services(run_upgrade):
    result, commands = run_upgrade(failure="bootstrap_once")
    assert result.returncode != 0
    runtime = command_index(commands, ["up"], contains="worker")
    beat = commands.index(["up", "-d", "--no-deps", "beat"])
    bootstrap = [i for i, c in enumerate(commands) if "bootstrap_runtime" in c]
    assert len(bootstrap) == 2
    assert commands[bootstrap[0]][-1] == "bootstrap_runtime"
    assert commands[bootstrap[-1]][-1] == "--skip-migrations"
    assert bootstrap[-1] < runtime < beat
    assert not any(c[0] == "start" for c in commands)


def test_worker_start_failure_cannot_start_beat_even_during_cleanup(run_upgrade):
    result, commands = run_upgrade(failure="start_runtime")
    assert result.returncode != 0
    assert ["up", "-d", "--no-deps", "beat"] not in commands


def test_persistent_setup_failure_cannot_start_unprepared_services(run_upgrade):
    result, commands = run_upgrade(failure="bootstrap")
    assert result.returncode != 0
    assert sum("bootstrap_runtime" in c for c in commands) == 2
    assert not any(c[0] == "up" and "worker" in c for c in commands)
    assert ["up", "-d", "--no-deps", "beat"] not in commands


def test_worker_stop_failure_does_not_take_http_down(run_upgrade):
    result, commands = run_upgrade(failure="stop_workers")
    assert result.returncode != 0
    assert ["stop", "django"] not in commands
    assert not any("bootstrap_runtime" in c for c in commands)
    assert not any(c[-2:] == ["migrate", "--noinput"] for c in commands)


def test_consumer_readiness_failure_never_starts_beat(run_upgrade):
    result, commands = run_upgrade(failure="ready_consumers")
    assert result.returncode != 0
    assert ["up", "-d", "--no-deps", "beat"] not in commands
    assert "upgrade completed" not in result.stdout


def test_scheduler_readiness_failure_stops_beat_without_cleanup_restart(run_upgrade):
    result, commands = run_upgrade(failure="ready_scheduler")
    assert result.returncode != 0
    start = commands.index(["up", "-d", "--no-deps", "beat"])
    assert ["stop", "beat"] in commands[start + 1 :]
    assert commands.count(["up", "-d", "--no-deps", "beat"]) == 1
    assert "upgrade completed" not in result.stdout


@pytest.mark.parametrize(
    ("prepared", "fail_bootstrap"), [(False, False), (True, False), (False, True)]
)
def test_web_start_requires_bootstrap_unless_upgrade_prepared_it(
    tmp_path, prepared, fail_bootstrap
):
    """执行真实 /start，验证首次部署会初始化，失败不会开放 HTTP。"""
    command_log = tmp_path / "commands.jsonl"
    fake = r"""
import json
import os
import sys
from pathlib import Path

tool = Path(sys.argv[0]).name
with Path(os.environ["COMMAND_LOG"]).open("a") as output:
    output.write(json.dumps([tool, *sys.argv[1:]]) + "\n")
if "shell-env" in sys.argv:
    print("GUNICORN_WORKERS=1; GUNICORN_THREADS=1")
if "bootstrap_runtime" in sys.argv and os.environ["FAIL_BOOTSTRAP"] == "true":
    sys.exit(23)
"""
    for name in ("python", "gunicorn"):
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\n{fake}")
        executable.chmod(0o755)
    result = subprocess.run(
        ["/bin/bash", str(START_SCRIPT), *(["--prepared"] if prepared else [])],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "COMMAND_LOG": str(command_log),
            "FAIL_BOOTSTRAP": str(fail_bootstrap).lower(),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    commands = [json.loads(line) for line in command_log.read_text().splitlines()]
    assert any("bootstrap_runtime" in c for c in commands) is not prepared
    assert any(c[0] == "gunicorn" for c in commands) is not fail_bootstrap
    assert (result.returncode == 0) is not fail_bootstrap
