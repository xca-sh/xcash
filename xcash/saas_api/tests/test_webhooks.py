from datetime import timedelta

import pytest

from projects.models import Project
from webhooks.models import DeliveryAttempt
from webhooks.models import WebhookEvent

AUTH_HEADER = "Bearer test-saas-token"


def _make_attempt(event, try_number, *, ok=False):
    return DeliveryAttempt.objects.create(
        event=event,
        try_number=try_number,
        request_headers={},
        request_body="{}",
        response_status=200 if ok else 500,
        duration_ms=10,
        ok=ok,
    )


@pytest.mark.django_db
class TestSaasDeliveryAttemptEndpoint:
    def test_filters_attempts_by_event(self, client, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(name="webhook-project")
        other_project = Project.objects.create(name="other-webhook-project")
        event = WebhookEvent.objects.create(
            project=project, payload={"type": "invoice"}
        )
        sibling = WebhookEvent.objects.create(
            project=project, payload={"type": "deposit"}
        )
        foreign = WebhookEvent.objects.create(
            project=other_project, payload={"type": "invoice"}
        )
        _make_attempt(event, 1)
        _make_attempt(event, 2, ok=True)
        _make_attempt(sibling, 1)
        _make_attempt(foreign, 1)
        url = f"/saas/v1/projects/{project.appid}/delivery-attempts"

        response = client.get(url, {"event": event.pk}, HTTP_AUTHORIZATION=AUTH_HEADER)
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert {item["event_nonce"] for item in data["results"]} == {event.nonce}

        # 不带 event 参数时保持原行为：返回项目下全部投递
        response = client.get(url, HTTP_AUTHORIZATION=AUTH_HEADER)
        assert response.json()["count"] == 3

    def test_cannot_read_other_project_event_attempts(self, client, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(name="webhook-project")
        other_project = Project.objects.create(name="other-webhook-project")
        foreign = WebhookEvent.objects.create(
            project=other_project, payload={"type": "invoice"}
        )
        _make_attempt(foreign, 1)

        response = client.get(
            f"/saas/v1/projects/{project.appid}/delivery-attempts",
            {"event": foreign.pk},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert response.json()["count"] == 0

    def test_invalid_event_id_returns_empty(self, client, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(name="webhook-project")
        event = WebhookEvent.objects.create(
            project=project, payload={"type": "invoice"}
        )
        _make_attempt(event, 1)

        response = client.get(
            f"/saas/v1/projects/{project.appid}/delivery-attempts",
            {"event": "abc"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert response.json()["count"] == 0


@pytest.mark.django_db
class TestSaasWebhookEventListFilters:
    @pytest.fixture
    def setup(self, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(name="webhook-project")
        other_project = Project.objects.create(name="other-webhook-project")
        invoice_ok = WebhookEvent.objects.create(
            project=project,
            payload={"type": "invoice"},
            status=WebhookEvent.Status.SUCCEEDED,
        )
        deposit_failed = WebhookEvent.objects.create(
            project=project,
            payload={"type": "deposit"},
            status=WebhookEvent.Status.FAILED,
        )
        deposit_pending = WebhookEvent.objects.create(
            project=project, payload={"type": "deposit"}
        )
        # 其他项目的同类事件不能出现在结果里
        WebhookEvent.objects.create(project=other_project, payload={"type": "deposit"})
        return {
            "url": f"/saas/v1/projects/{project.appid}/webhook-events",
            "invoice_ok": invoice_ok,
            "deposit_failed": deposit_failed,
            "deposit_pending": deposit_pending,
        }

    def _ids(self, client, url, params):
        response = client.get(url, params, HTTP_AUTHORIZATION=AUTH_HEADER)
        assert response.status_code == 200, response.content
        return {item["id"] for item in response.json()["results"]}

    def test_nonce_prefix_and_exact_match(self, client, setup):
        event = setup["deposit_failed"]
        assert self._ids(client, setup["url"], {"q": event.nonce}) == {event.pk}
        assert event.pk in self._ids(client, setup["url"], {"q": event.nonce[:6]})
        # 中间片段不是前缀，不命中
        assert self._ids(client, setup["url"], {"q": event.nonce[3:]}) == set()

    def test_filter_by_type_and_status(self, client, setup):
        assert self._ids(client, setup["url"], {"type": "deposit"}) == {
            setup["deposit_failed"].pk,
            setup["deposit_pending"].pk,
        }
        assert self._ids(
            client, setup["url"], {"type": "deposit", "status": "pending"}
        ) == {setup["deposit_pending"].pk}
        assert self._ids(client, setup["url"], {"status": "succeeded"}) == {
            setup["invoice_ok"].pk
        }

    def test_filter_by_created_range(self, client, setup):
        old = setup["invoice_ok"]
        WebhookEvent.objects.filter(pk=old.pk).update(
            created_at=old.created_at - timedelta(days=10)
        )
        boundary = (old.created_at - timedelta(days=5)).isoformat()
        assert old.pk not in self._ids(
            client, setup["url"], {"created_after": boundary}
        )
        assert self._ids(client, setup["url"], {"created_before": boundary}) == {old.pk}

    @pytest.mark.parametrize(
        "params",
        [
            {"type": "test"},
            {"status": "delivered"},
            {"created_after": "yesterday"},
        ],
    )
    def test_invalid_params_return_400(self, client, setup, params):
        response = client.get(setup["url"], params, HTTP_AUTHORIZATION=AUTH_HEADER)
        assert response.status_code == 400

    def test_detail_ignores_list_filters(self, client, setup):
        event = setup["invoice_ok"]
        response = client.get(
            f"{setup['url']}/{event.pk}",
            {"type": "deposit"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert response.json()["nonce"] == event.nonce
