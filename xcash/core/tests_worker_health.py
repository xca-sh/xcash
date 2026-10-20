"""消费心跳的故障判定与发布新鲜度；不依赖业务数据或链节点。"""

from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.urls import reverse

from config.worker_health import WORKER_HEALTH_GROUP
from config.worker_health import WORKER_HEALTH_MAX_AGE_SECONDS
from config.worker_health import record_chain_runner_heartbeat
from config.worker_health import record_worker_health
from config.worker_health import stale_worker_groups
from config.worker_health import worker_health_key
from core.tasks import report_worker_health


@pytest.fixture
def heartbeats(settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": uuid4().hex,
        }
    }
    with patch("config.worker_health.time.time", return_value=1000):
        record_worker_health(published_at=1000)
        record_chain_runner_heartbeat()
        yield


def test_workers_health_requires_both_groups_and_no_auth(client, heartbeats):
    response = client.get(reverse("health-workers"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("group", ["worker", "chain-runner"])
def test_missing_worker_is_detected_independently(client, heartbeats, group):
    cache.delete(worker_health_key(group))
    response = client.get(reverse("health-workers"))
    assert response.status_code == 503
    assert response.json() == {"status": "stalled"}


def test_probe_records_schedule_time_and_execution_time(heartbeats):
    """探针写入周期计划时刻与实际执行时刻：前者证明周期投递在跑，后者证明有执行容量。"""
    report_worker_health(timestamp=990)

    assert cache.get(worker_health_key(WORKER_HEALTH_GROUP)) == {
        "published_at": 990,
        "completed_at": 1000,
    }


def test_probe_scheduled_too_long_ago_is_unhealthy(client, heartbeats):
    """周期投递停摆时，只剩执行旧积压探针的回执，不能被判为健康。"""
    cache.set(
        worker_health_key(WORKER_HEALTH_GROUP),
        {
            "published_at": 1000 - WORKER_HEALTH_MAX_AGE_SECONDS - 1,
            "completed_at": 1000,
        },
    )
    assert client.get(reverse("health-workers")).status_code == 503


def test_stopped_workers_become_unhealthy_even_if_cache_has_not_expired(
    client, heartbeats
):
    with patch(
        "config.worker_health.time.time",
        return_value=1000 + WORKER_HEALTH_MAX_AGE_SECONDS + 1,
    ):
        assert client.get(reverse("health-workers")).status_code == 503


@pytest.mark.parametrize("value", [None, "bad", float("nan"), 1001, True])
def test_corrupt_or_future_heartbeat_fails_closed(client, heartbeats, value):
    cache.set(
        worker_health_key(WORKER_HEALTH_GROUP),
        {"published_at": value, "completed_at": 1000},
    )
    assert client.get(reverse("health-workers")).status_code == 503


def test_cache_failure_returns_503_without_internal_details(client, heartbeats):
    with patch("config.worker_health.cache.get_many", side_effect=OSError("private")):
        response = client.get(reverse("health-workers"))
    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy"}


def test_release_rejects_cached_receipts_from_before_current_check(heartbeats):
    assert stale_worker_groups(completed_after=1001) == ["chain-runner", "worker"]
    assert stale_worker_groups(completed_after=1000) == []


def test_release_accepts_old_probe_executed_by_new_worker(heartbeats):
    """新 worker 可能先执行升级前留下的探针：计划时刻早于就绪下限，执行时刻晚于它即算就绪。"""
    cache.set(
        worker_health_key(WORKER_HEALTH_GROUP),
        {"published_at": 900, "completed_at": 1000},
    )
    assert stale_worker_groups(completed_after=950) == []
