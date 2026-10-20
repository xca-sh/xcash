"""发布门控应拒绝不可服务的 HTTP 与旧进程留下的心跳。"""

from io import StringIO
from unittest.mock import patch

import httpx
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from config.worker_health import CHAIN_RUNNER_HEALTH_GROUP
from config.worker_health import WORKER_HEALTH_GROUP

MODULE = "core.management.commands.wait_for_runtime"


@pytest.fixture
def runtime():
    # 人工时钟让超时与恢复路径确定完成，无须在单元测试中真实等待。
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    with (
        patch(f"{MODULE}.httpx.Client") as client_class,
        patch(f"{MODULE}.stale_worker_groups", return_value=[]) as stale,
        patch(f"{MODULE}.time.monotonic", side_effect=lambda: now[0]),
        patch(f"{MODULE}.time.time", return_value=1234),
        patch(f"{MODULE}.time.sleep", side_effect=sleep),
    ):
        client = client_class.return_value.__enter__.return_value
        client.get.return_value.json.return_value = {"status": "ok"}
        yield client, stale


def run(**options):
    output = StringIO()
    call_command("wait_for_runtime", timeout=3, stdout=output, **options)
    return output.getvalue()


def test_waits_for_http_recovery(runtime):
    client, stale = runtime
    good = client.get.return_value
    client.get.side_effect = [httpx.ConnectError("not listening"), good]
    assert "ready" in run()
    # 未给下限时只认本命令开始之后执行的心跳。
    stale.assert_called_once_with(completed_after=1234)
    assert client.get.call_args.kwargs["timeout"] > 0


@pytest.mark.parametrize("failure", ["http", "payload", "chain_runner", "worker"])
def test_readiness_failure_times_out_instead_of_reporting_success(runtime, failure):
    client, stale = runtime
    if failure == "http":
        client.get.side_effect = httpx.ConnectError("http://secret:password@private")
    elif failure == "payload":
        client.get.return_value.json.return_value = {"status": "unhealthy"}
    elif failure == "chain_runner":
        stale.return_value = [CHAIN_RUNNER_HEALTH_GROUP]
    else:
        stale.return_value = [WORKER_HEALTH_GROUP]
    output = StringIO()
    with pytest.raises(CommandError, match="timed out") as error:
        call_command("wait_for_runtime", timeout=3, stdout=output)
    assert "password" not in str(error.value)
    assert "password" not in output.getvalue()
    assert "runtime ready" not in output.getvalue()


def test_http_recovery_is_reported_even_while_heartbeats_are_missing(runtime):
    _, stale = runtime
    stale.return_value = [WORKER_HEALTH_GROUP]
    output = StringIO()
    with pytest.raises(CommandError, match="timed out"):
        call_command(
            "wait_for_runtime",
            timeout=3,
            http_stopped_at=1200,
            stdout=output,
        )
    assert "HTTP ready through Caddy" in output.getvalue()
    assert "34.0s since Django stop was requested" in output.getvalue()
    assert "waiting for heartbeats: worker" in output.getvalue()
    assert "runtime ready" not in output.getvalue()


@pytest.mark.parametrize(("started_after", "expected"), [(None, 1234), (1200, 1200)])
def test_requires_heartbeats_after_start_and_recovers(runtime, started_after, expected):
    """升级脚本传入发起启动的时刻时以其为准：新进程的首个心跳可能早于本命令开始。"""
    _, stale = runtime
    stale.side_effect = [[WORKER_HEALTH_GROUP, CHAIN_RUNNER_HEALTH_GROUP], []]
    output = run(started_after=started_after)
    assert "ready" in output
    assert all(
        call.kwargs == {"completed_after": expected} for call in stale.call_args_list
    )


def test_runtime_timeout_must_be_positive():
    with pytest.raises(CommandError, match="positive"):
        call_command("wait_for_runtime", timeout=0)
