import pytest

from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"


@pytest.mark.django_db
class TestSaasDepositEndpoint:

    def test_unknown_chain_code_still_returns_invalid_chain(self, client, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(
            name="saas-deposit-project-2",
        )

        response = client.get(
            f"/saas/v1/projects/{project.appid}/deposits/address",
            {"uid": "user-1", "chain": "missing-chain"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400
        assert response.json() == {
            "code": "2000",
            "message": "无效链",
            "detail": "",
        }
