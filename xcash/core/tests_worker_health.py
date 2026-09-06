"""消费心跳的故障判定与发布新鲜度；不依赖业务数据或链节点。"""

from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.urls import reverse

from config.periodic_tasks import WORKER_HEALTH_TASK_GROUPS
from config.worker_health import WORKER_HEALTH_MAX_AGE_SECONDS
from config.worker_health import WORKER_HEALTH_PUBLISHED_HEADER
from config.worker_health import record_worker_health
from config.worker_health import stale_worker_groups
from config.worker_health import stamp_worker_health_probe
from config.worker_health import worker_health_key


@pytest.fixture
def heartbeats(settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": uuid4().hex,
        }
    }
    with patch("config.worker_health.time.time", return_value=1000):
        for name in WORKER_HEALTH_TASK_GROUPS:
            headers = {}
            stamp_worker_health_probe(sender=name, headers=headers)
            record_worker_health(
                SimpleNamespace(name=name, request=SimpleNamespace(headers=headers))
            )
        yield


def test_workers_health_requires_both_groups_and_no_auth(client, heartbeats):
    response = client.get(reverse("health-workers"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("group", ["celery", "scan"])
def test_missing_worker_is_detected_independently(client, heartbeats, group):
    cache.delete(worker_health_key(group))
    response = client.get(reverse("health-workers"))
    assert response.status_code == 503
    assert response.json() == {"status": "stalled"}


def test_consuming_old_backlog_does_not_prove_beat_is_alive(client, heartbeats):
    cache.set(
        worker_health_key("celery"),
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
        worker_health_key("celery"),
        {"published_at": value, "completed_at": 1000},
    )
    assert client.get(reverse("health-workers")).status_code == 503


def test_cache_failure_returns_503_without_internal_details(client, heartbeats):
    with patch("config.worker_health.cache.get_many", side_effect=OSError("private")):
        response = client.get(reverse("health-workers"))
    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy"}


def test_release_rejects_cached_receipts_from_before_current_check(heartbeats):
    assert stale_worker_groups(published_after=1001) == ["celery", "scan"]
    assert stale_worker_groups(published_after=1000) == []


def test_publication_hook_does_not_change_business_messages():
    headers = {"id": "business-id"}
    stamp_worker_health_probe(sender="webhooks.tasks.deliver", headers=headers)
    assert WORKER_HEALTH_PUBLISHED_HEADER not in headers
