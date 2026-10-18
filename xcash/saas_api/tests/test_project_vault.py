"""POST /saas/v1/projects/{appid}/vault 的行为契约测试。

覆盖：set-once 写入、不可变性（已设置则 409）、智能合约收款开关把关、鉴权。
"""

from unittest.mock import patch

import pytest
from saas_api.viewsets.projects import ProjectViewSet

from invoices.models import DifferRecipientAddress
from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"
VALID_EVM_VAULT = "0x52908400098527886E0F7030069857D2E4169EE7"
VALID_TRON_VAULT = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"


@pytest.fixture
def project(db):
    return Project.objects.create(
        name="vault-test-project",
        contract_receiving_enabled=True,
    )


def _url(project):
    return f"/saas/v1/projects/{project.appid}/vault"


@pytest.mark.django_db
class TestSetVault:
    def test_set_vault_success(self, client, project):
        assert project.evm_vault in (None, "")
        response = client.post(
            _url(project),
            data={"chain_type": "evm", "vault": VALID_EVM_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert response.json()["evm_vault_address"] == VALID_EVM_VAULT
        assert response.json()["tron_vault_address"] is None
        project.refresh_from_db()
        assert project.evm_vault == VALID_EVM_VAULT

    def test_set_tron_vault_success(self, client, project):
        assert project.tron_vault in (None, "")
        response = client.post(
            _url(project),
            data={"chain_type": "tron", "vault": VALID_TRON_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        assert response.json()["evm_vault_address"] is None
        assert response.json()["tron_vault_address"] == VALID_TRON_VAULT
        project.refresh_from_db()
        assert project.tron_vault == VALID_TRON_VAULT

    def test_set_vault_is_immutable_once_set(self, client, project):
        other = "0x8617E340B3D01FA5F11F306F4090FD50E238070D"
        project.evm_vault = VALID_EVM_VAULT
        project.save(update_fields=["evm_vault"])

        response = client.post(
            _url(project),
            data={"chain_type": "evm", "vault": other},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 409
        project.refresh_from_db()
        assert project.evm_vault == VALID_EVM_VAULT

    def test_set_vault_rejects_wrong_chain_format(self, client, project):
        response = client.post(
            _url(project),
            data={"chain_type": "tron", "vault": VALID_EVM_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400
        project.refresh_from_db()
        assert project.tron_vault in (None, "")

    def test_set_vault_rejects_global_differ_address_conflict(self, client, project):
        other_project = Project.objects.create(name="vault-conflict-owner")
        DifferRecipientAddress.objects.create(
            project=other_project,
            chain_type="evm",
            address=VALID_EVM_VAULT,
        )

        response = client.post(
            _url(project),
            data={"chain_type": "evm", "vault": VALID_EVM_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400
        project.refresh_from_db()
        assert project.evm_vault in (None, "")

    def test_set_vault_rejects_global_vault_conflict(self, client, project):
        Project.objects.create(
            name="vault-conflict-project",
            evm_vault=VALID_EVM_VAULT,
        )

        response = client.post(
            _url(project),
            data={"chain_type": "evm", "vault": VALID_EVM_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400
        project.refresh_from_db()
        assert project.evm_vault in (None, "")

    def test_set_vault_requires_auth(self, client, project):
        response = client.post(
            _url(project),
            data={"chain_type": "evm", "vault": VALID_EVM_VAULT},
            content_type="application/json",
        )
        assert response.status_code in (401, 403)
        project.refresh_from_db()
        assert project.evm_vault in (None, "")


@pytest.mark.django_db
class TestSetVaultRequiresContractReceiving:
    def test_rejects_when_contract_receiving_disabled(self, client, project):
        project.contract_receiving_enabled = False
        project.save(update_fields=["contract_receiving_enabled"])

        response = client.post(
            _url(project),
            data={"chain_type": "evm", "vault": VALID_EVM_VAULT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400
        assert response.json() == {
            "vault": "未开启智能合约收款，不能设置收款归集地址。"
        }
        project.refresh_from_db()
        assert project.evm_vault in (None, "")

    def test_rechecks_switch_under_row_lock(self, client, project):
        """锁外读到的是开启状态，但写入前开关已被并发关闭：锁内复查必须拒绝写入。"""
        stale = Project.objects.get(pk=project.pk)
        Project.objects.filter(pk=project.pk).update(contract_receiving_enabled=False)

        with patch.object(ProjectViewSet, "get_object", return_value=stale):
            response = client.post(
                _url(project),
                data={"chain_type": "evm", "vault": VALID_EVM_VAULT},
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )

        assert response.status_code == 400
        assert response.json() == {
            "vault": "未开启智能合约收款，不能设置收款归集地址。"
        }
        project.refresh_from_db()
        assert project.evm_vault in (None, "")
