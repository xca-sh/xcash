"""POST /saas/v1/projects/{appid}/vault 的行为契约测试。

覆盖：set-once 写入、不可变性（已设置则 409）、鉴权。
"""

import pytest

from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"
VALID_VAULT = "0x52908400098527886E0F7030069857D2E4169EE7"


@pytest.fixture
def project(db):
    return Project.objects.create(name="vault-test-project")


def _url(project):
    return f"/saas/v1/projects/{project.appid}/vault"


@pytest.mark.django_db
class TestSetVault:
    def test_set_vault_success(self, client, project):
        assert project.vault in (None, "")
        response = client.post(
            _url(project),
            data={"vault": VALID_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert response.json()["vault_address"] == VALID_VAULT
        project.refresh_from_db()
        assert project.vault == VALID_VAULT

    def test_set_vault_is_immutable_once_set(self, client, project):
        other = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"
        project.vault = VALID_VAULT
        project.save(update_fields=["vault"])

        response = client.post(
            _url(project),
            data={"vault": other},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 409
        project.refresh_from_db()
        assert project.vault == VALID_VAULT

    def test_set_vault_requires_auth(self, client, project):
        response = client.post(
            _url(project),
            data={"vault": VALID_VAULT},
            content_type="application/json",
        )
        assert response.status_code in (401, 403)
        project.refresh_from_db()
        assert project.vault in (None, "")
