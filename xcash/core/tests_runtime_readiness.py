"""发布门控应拒绝不可服务的 HTTP、缺失的消费队列和旧调度回执。"""

from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from config.periodic_tasks import PERIODIC_TASK_GROUPS
from config.periodic_tasks import PERIODIC_TASK_QUEUES
from core.management.commands.wait_for_runtime import missing_consumer_groups

MODULE = "core.management.commands.wait_for_runtime"


@pytest.fixture
def runtime():
    # 人工时钟让超时与恢复路径确定完成，无须在单元测试中真实等待。
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with (
        patch(f"{MODULE}.httpx.Client") as client_class,
        patch(f"{MODULE}.missing_consumer_groups", return_value=[]) as consumers,
        patch(f"{MODULE}.stale_worker_groups", return_value=[]) as scheduled,
        patch(f"{MODULE}.time.monotonic", side_effect=lambda: now[0]),
        patch(f"{MODULE}.time.time", return_value=1234),
        patch(f"{MODULE}.time.sleep", side_effect=sleep),
    ):
        client = client_class.return_value.__enter__.return_value
        client.get.return_value.json.return_value = {"status": "ok"}
        yield client, consumers, scheduled


def run(phase):
    output = StringIO()
    call_command("wait_for_runtime", phase=phase, timeout=3, stdout=output)
    return output.getvalue()


def test_consumers_phase_waits_for_http_recovery(runtime):
    client, consumers, scheduled = runtime
    good = client.get.return_value
    client.get.side_effect = [httpx.ConnectError("not listening"), good]
    assert "ready" in run("consumers")
    consumers.assert_called_once()
    scheduled.assert_not_called()
    assert client.get.call_args.kwargs["timeout"] > 0


@pytest.mark.parametrize("failure", ["http", "payload", "consumers", "scheduler"])
def test_readiness_failure_times_out_instead_of_reporting_success(runtime, failure):
    client, consumers, scheduled = runtime
    if failure == "http":
        client.get.side_effect = httpx.ConnectError("http://secret:password@private")
    elif failure == "payload":
        client.get.return_value.json.return_value = {"status": "unhealthy"}
    elif failure == "consumers":
        consumers.return_value = ["scan"]
    else:
        scheduled.return_value = ["celery"]
    with pytest.raises(CommandError, match="timed out") as error:
        run("scheduler" if failure == "scheduler" else "consumers")
    assert "password" not in str(error.value)


def test_scheduler_requires_new_publications_and_recovers(runtime):
    _, consumers, scheduled = runtime
    scheduled.side_effect = [["celery", "scan"], []]
    assert "ready" in run("scheduler")
    consumers.assert_not_called()
    assert all(
        call.kwargs == {"published_after": 1234} for call in scheduled.call_args_list
    )


def test_runtime_timeout_must_be_positive():
    with pytest.raises(CommandError, match="positive"):
        call_command("wait_for_runtime", phase="consumers", timeout=0)


@pytest.mark.parametrize("missing", [None, "scan", "celery", "periodic"])
def test_consumer_inspection_requires_main_and_periodic_queues(missing):
    replies = {
        group: [{"name": group}]
        + [
            {"name": PERIODIC_TASK_QUEUES[task]}
            for task, task_group in PERIODIC_TASK_GROUPS.items()
            if task_group == group
        ]
        for group in ("celery", "scan")
    }
    if missing == "periodic":
        replies["celery"] = [{"name": "celery"}]
    elif missing:
        del replies[missing]
    inspector = MagicMock()
    inspector.active_queues.return_value = replies
    with (
        patch(f"{MODULE}.app.connection_for_read"),
        patch(f"{MODULE}.app.control.inspect", return_value=inspector),
    ):
        result = missing_consumer_groups(1)
    assert result == (
        [] if missing is None else ["celery" if missing == "periodic" else missing]
    )
