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
