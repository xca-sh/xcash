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
if args[0] != "compose":
    # 非 compose 的 docker 命令：按标签查询退役服务的容器，其余（stop/start/image）直接成功。
    if args[0] == "ps":
        service = args[-1].removeprefix("label=com.docker.compose.service=")
        print("\n".join(json.loads(os.environ["RETIRED_CONTAINERS"]).get(service, [])))
    sys.exit(0)

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
    if failure == "stop_workers" and args[1:] == ["worker", "chain-runner"]:
        sys.exit(23)
    if failure == "stop_django" and args[1:] == ["django"]:
        sys.exit(23)
elif command == "up" and "worker" in args and failure == "start_runtime":
    sys.exit(23)
elif command in ("run", "exec") and "manage.py" in args:
    operation = args[args.index("manage.py") + 1:]
    rehearsal = "POSTGRES_HOST=migration-rehearsal-db" in args
    logged = Path(os.environ["COMMAND_LOG"]).read_text().splitlines()
    if operation == ["migrate", "--plan"]:
        if os.environ["PENDING_MIGRATIONS"] == "true":
            print("chains.0001_initial")
        else:
            print("Planned operations:")
            print("  No planned migration operations.")
        # 第一次查询是升级开头的 pending plan，之后是迁移前的复核。
        plans = sum('"migrate", "--plan"' in line for line in logged)
        if failure == ("pending_plan" if plans == 1 else "production_plan"):
            sys.exit(23)
    elif rehearsal and operation == ["bootstrap_runtime"]:
        if failure == "rehearsal":
            sys.exit(23)
    elif not rehearsal and operation[:1] == ["bootstrap_runtime"]:
        if "--skip-migrations" not in operation and failure == "production_migrate":
            sys.exit(1)
        if failure == "bootstrap_interrupted":
            sys.exit(137)
        attempts = sum(
            '"bootstrap_runtime"' in line and "migration-rehearsal-db" not in line
            for line in logged
        )
        if failure == "bootstrap" or (failure == "bootstrap_once" and attempts == 1):
            sys.exit(20)
    elif operation[:1] == ["wait_for_runtime"]:
        if failure == "ready":
            sys.exit(23)
    elif operation == ["migrate_celery_messages", "--dry-run"]:
        if failure == "celery_messages_preview":
            sys.exit(23)
    elif operation == ["migrate_celery_messages"]:
        if failure == "celery_messages":
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
        retired=None,
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
            "RUNNING_SERVICES": running or "django\nworker\nchain-runner",
            "APP_READY_TIMEOUT": ready_timeout,
            # 按退役服务名给出仍在运行的旧容器，默认有一个旧版本的 beat。
            "RETIRED_CONTAINERS": json.dumps(
                {"beat": ["old-beat-1"]} if retired is None else retired
            ),
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
            if command[0] == "docker" and command[1] != "compose":
                # 非 compose 的 docker 命令原样保留（去掉 docker 前缀），便于断言先后顺序。
                commands.append(["docker:", *command[1:]])
                continue
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


def production_bootstraps(commands):
    return [
        index
        for index, command in enumerate(commands)
        if "bootstrap_runtime" in command and "POSTGRES_HOST=db" in command
    ]


def retired_lookup(service):
    return [
        "docker:",
        "ps",
        "-q",
        "--filter",
        "label=com.docker.compose.project=xcash",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ]


def manage_command(commands, *operation):
    return next(
        index
        for index, command in enumerate(commands)
        if command[0] == "run"
        and command[command.index("manage.py") + 1 :] == list(operation)
    )


@pytest.mark.parametrize(
    ("migrations", "quiesced"), [(False, False), (True, False), (True, True)]
)
def test_upgrade_switches_old_processes_before_starting_new_runtime(
    run_upgrade,
    migrations,
    quiesced,
):
    result, commands = run_upgrade(migrations=migrations, quiesced=quiesced)
    assert result.returncode == 0, result.stdout + result.stderr
    pull = command_index(commands, ["pull"])
    preview = manage_command(commands, "migrate_celery_messages", "--dry-run")
    stop_beat = commands.index(["docker:", "stop", "old-beat-1"])
    stop_workers = commands.index(["stop", "worker", "chain-runner"])
    stop_django = commands.index(["stop", "django"])
    migrate_messages = manage_command(commands, "migrate_celery_messages")
    start_runtime = command_index(commands, ["up"], contains="worker")
    ready = command_index(commands, ["exec"], contains="wait_for_runtime")
    reload_caddy = command_index(commands, ["exec"], contains="reload")
    [bootstrap] = production_bootstraps(commands)
    # 旧 beat 是生产者，先于 worker 排空停止；在途消息在生产迁移之后、新进程启动之前迁移。
    assert (
        pull
        < preview
        < stop_beat
        < stop_workers
        < stop_django
        < bootstrap
        < migrate_messages
        < start_runtime
        < ready
        < reload_caddy
    )
    # 新进程一启动就可能写心跳，就绪下限必须是发起启动的时刻，而不是等待命令自身开始时刻。
    assert "--started-after" in commands[ready]
    # --remove-orphans 删除退役服务（旧 beat）的容器。
    assert "--remove-orphans" in commands[start_runtime]
    assert not any("beat" in command for command in commands if command[0] != "docker:")
    assert commands.count(["stop", "worker", "chain-runner"]) == 1
    assert commands.count(["stop", "django"]) == 1
    assert commands[bootstrap][-1] == "bootstrap_runtime"
    assert not any(
        "POSTGRES_HOST=db" in c and c[-2:] == ["migrate", "--noinput"] for c in commands
    )
    assert not any(c[0] == "run" and "wait_for_runtime" in c for c in commands)
    rehearsal = [c for c in commands if "POSTGRES_HOST=migration-rehearsal-db" in c]
    if migrations:
        # 演练与生产执行同一个 bootstrap_runtime。
        assert [c[-1] for c in rehearsal] == ["bootstrap_runtime"]
        rehearsal_index = command_index(
            commands,
            ["run"],
            contains="POSTGRES_HOST=migration-rehearsal-db",
        )
        assert (stop_beat < rehearsal_index) is quiesced
        plans = [i for i, c in enumerate(commands) if c[-2:] == ["migrate", "--plan"]]
        verify_plan = plans[-1]
        assert len(plans) == 2
        assert rehearsal_index < verify_plan < bootstrap
        # 在线演练时 plan 复核在停 Django 之前，不占 HTTP 停机窗口。
        assert (verify_plan < stop_django) is not quiesced
    else:
        assert rehearsal == []


def test_retired_service_containers_are_stopped_before_draining_workers(run_upgrade):
    """compose 已不认识退役服务，必须按标签停掉旧容器，不能让旧代码跑过 migrate。"""
    result, commands = run_upgrade(
        migrations=True,
        retired={"worker-scan": ["old-scan-1", "old-scan-2"], "beat": ["old-beat-1"]},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    stop_scan = commands.index(["docker:", "stop", "old-scan-1", "old-scan-2"])
    stop_beat = commands.index(["docker:", "stop", "old-beat-1"])
    stop_workers = commands.index(["stop", "worker", "chain-runner"])
    stop_django = commands.index(["stop", "django"])
    [bootstrap] = production_bootstraps(commands)
    assert retired_lookup("worker-scan") in commands
    assert retired_lookup("beat") in commands
    assert max(stop_scan, stop_beat) < stop_workers < stop_django < bootstrap
    assert not any(c[:2] == ["docker:", "start"] for c in commands)


def test_pre_migration_failure_restores_retired_service_containers(run_upgrade):
    result, commands = run_upgrade(
        migrations=True,
        failure="stop_django",
        running="worker",
        retired={"beat": ["old-beat-1"]},
    )
    assert result.returncode != 0
    assert ["docker:", "start", "old-beat-1"] in commands
    assert ["start", "worker"] in commands


def test_upgrade_without_retired_containers_skips_retired_stop(run_upgrade):
    result, commands = run_upgrade(retired={})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(c[:2] == ["docker:", "stop"] for c in commands)


def test_pull_failure_leaves_existing_services_running(run_upgrade):
    result, commands = run_upgrade(failure="pull")
    assert result.returncode != 0
    assert not any(c[0] in ("stop", "start") for c in commands)


@pytest.mark.parametrize(
    "failure",
    ["pending_plan", "production_plan", "rehearsal", "celery_messages_preview"],
)
def test_pre_stop_failure_aborts_before_touching_production(run_upgrade, failure):
    result, commands = run_upgrade(migrations=True, failure=failure)
    assert result.returncode != 0
    assert not any(c[0] in ("stop", "start") for c in commands)
    assert not any(c[:2] == ["docker:", "stop"] for c in commands)
    assert production_bootstraps(commands) == []
    assert ("迁移演练失败" in result.stderr) is (failure == "rehearsal")


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


@pytest.mark.parametrize(
    ("failure", "quiesced"),
    [("production_plan", True), ("stop_workers", False), ("stop_django", False)],
)
def test_pre_migration_failure_restores_only_previously_running_containers(
    run_upgrade, failure, quiesced
):
    result, commands = run_upgrade(
        migrations=True,
        quiesced=quiesced,
        failure=failure,
        running="worker",
    )
    assert result.returncode != 0
    assert ["start", "worker"] in commands
    assert not any(c[0] == "up" and "worker" in c for c in commands)


@pytest.mark.parametrize("failure", ["production_migrate", "bootstrap_interrupted"])
def test_failed_or_unconfirmed_migration_does_not_restart_apps(run_upgrade, failure):
    result, commands = run_upgrade(failure=failure)
    assert result.returncode != 0
    assert ["stop", "worker", "chain-runner"] in commands
    assert not any(c[0] in ("start", "up") and "worker" in c for c in commands)
    assert not any(
        "migrate_celery_messages" in c
        for c in commands
        if c[0] == "run" and "--dry-run" not in c
    )


def test_post_migration_recovery_retries_setup_before_starting_services(run_upgrade):
    result, commands = run_upgrade(failure="bootstrap_once")
    assert result.returncode != 0
    runtime = command_index(commands, ["up"], contains="worker")
    bootstrap = [i for i, c in enumerate(commands) if "bootstrap_runtime" in c]
    assert len(bootstrap) == 2
    assert commands[bootstrap[0]][-1] == "bootstrap_runtime"
    assert commands[bootstrap[-1]][-1] == "--skip-migrations"
    assert bootstrap[-1] < runtime
    assert not any(c[0] == "start" for c in commands)
    # 在途消息尚未迁移：恢复服务后提示运维手工执行迁移命令。
    assert "manage.py migrate_celery_messages" in result.stderr


def test_celery_message_migration_failure_still_restores_service(run_upgrade):
    """迁移已完成：在途消息迁移失败时按新 schema 恢复服务，消息留在 Redis 等运维手工迁移。"""
    result, commands = run_upgrade(failure="celery_messages")
    assert result.returncode != 0
    migrate_messages = manage_command(commands, "migrate_celery_messages")
    runtime = command_index(commands, ["up"], contains="worker")
    recovery_setup = command_index(commands, ["run"], contains="--skip-migrations")
    assert migrate_messages < recovery_setup < runtime
    assert "manage.py migrate_celery_messages" in result.stderr
    assert "upgrade completed" not in result.stdout


def test_runtime_start_failure_does_not_wait_or_report_success(run_upgrade):
    result, commands = run_upgrade(failure="start_runtime")
    assert result.returncode != 0
    assert not any("wait_for_runtime" in c for c in commands)
    assert "upgrade completed" not in result.stdout


def test_persistent_setup_failure_cannot_start_unprepared_services(run_upgrade):
    result, commands = run_upgrade(failure="bootstrap")
    assert result.returncode != 0
    assert sum("bootstrap_runtime" in c for c in commands) == 2
    assert not any(c[0] == "up" and "worker" in c for c in commands)


def test_worker_stop_failure_does_not_take_http_down(run_upgrade):
    result, commands = run_upgrade(failure="stop_workers")
    assert result.returncode != 0
    assert ["stop", "django"] not in commands
    assert not any("bootstrap_runtime" in c for c in commands)
    assert not any(c[-2:] == ["migrate", "--noinput"] for c in commands)


def test_readiness_failure_is_not_reported_as_success(run_upgrade):
    result, commands = run_upgrade(failure="ready")
    assert result.returncode != 0
    assert not any("reload" in c for c in commands)
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
