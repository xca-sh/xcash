import pytest


@pytest.mark.django_db
def test_project_list_requires_saas_token(client):
    response = client.get("/saas/v1/projects")

    assert response.status_code == 401
