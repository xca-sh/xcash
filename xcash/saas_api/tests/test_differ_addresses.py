"""saas_api 钱包直收地址池 CRUD 的行为测试。

覆盖：
- 地址格式按链类型校验（EVM checksum / Tron base58）翻译为 400
- 地址类型与链类型必须匹配（EVM 地址填到 tron 上被拒）
- 全局唯一性冲突 → 400
- 作用域严格限定 project_appid：列表只见本项目、无法删他项目地址
- chain_type 过滤
- active 切换
"""

import pytest
from web3 import Web3

from invoices.models import DifferRecipientAddress
from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"

EVM_ADDR = Web3.to_checksum_address("0x000000000000000000000000000000000000abcd")
EVM_ADDR_2 = Web3.to_checksum_address("0x000000000000000000000000000000000000abce")
TRON_ADDR = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.fixture
def project(db):
    return Project.objects.create(name="differ-proj")


@pytest.fixture
def other_project(db):
    return Project.objects.create(name="differ-proj-other")


def _list_url(project):
    return f"/saas/v1/projects/{project.appid}/differ-addresses"


def _detail_url(project, pk):
    return f"/saas/v1/projects/{project.appid}/differ-addresses/{pk}"


@pytest.mark.django_db
class TestDifferAddressCreate:
    def test_create_evm_address(self, client, project):
        response = client.post(
            _list_url(project),
            data={"chain_type": "evm", "address": EVM_ADDR},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 201, response.content
        assert DifferRecipientAddress.objects.filter(
            project=project, chain_type="evm", address=EVM_ADDR
        ).exists()

    def test_create_tron_address(self, client, project):
        response = client.post(
            _list_url(project),
            data={"chain_type": "tron", "address": TRON_ADDR},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 201, response.content

    def test_create_rejects_malformed_evm_address(self, client, project):
        response = client.post(
            _list_url(project),
            data={"chain_type": "evm", "address": "0x123"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400
        assert not DifferRecipientAddress.objects.filter(project=project).exists()

    def test_create_rejects_address_chain_type_mismatch(self, client, project):
        # EVM checksum 地址填到 tron 链类型上：base58 校验不过 → 400
        response = client.post(
            _list_url(project),
            data={"chain_type": "tron", "address": EVM_ADDR},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400
        assert not DifferRecipientAddress.objects.filter(project=project).exists()

    def test_create_rejects_duplicate_address(self, client, project):
        DifferRecipientAddress.objects.create(
            project=project, chain_type="evm", address=EVM_ADDR
        )
        response = client.post(
            _list_url(project),
            data={"chain_type": "evm", "address": EVM_ADDR},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 400

    def test_create_rejects_global_vault_conflict(self, client, project):
        other_project = Project.objects.create(
            name="differ-vault-conflict-owner",
            evm_vault=EVM_ADDR,
        )
        assert other_project.evm_vault == EVM_ADDR

        response = client.post(
            _list_url(project),
            data={"chain_type": "evm", "address": EVM_ADDR},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400
        assert not DifferRecipientAddress.objects.filter(project=project).exists()


@pytest.mark.django_db
class TestDifferAddressScopingAndList:
    def test_list_only_returns_own_project_addresses(
        self, client, project, other_project
    ):
        DifferRecipientAddress.objects.create(
            project=project, chain_type="evm", address=EVM_ADDR
        )
        DifferRecipientAddress.objects.create(
            project=other_project, chain_type="evm", address=EVM_ADDR_2
        )
        response = client.get(_list_url(project), HTTP_AUTHORIZATION=AUTH_HEADER)
        assert response.status_code == 200
        # 关闭分页：响应是裸列表
        addresses = {row["address"] for row in response.json()}
        assert addresses == {EVM_ADDR}

    def test_list_filter_by_chain_type(self, client, project):
        DifferRecipientAddress.objects.create(
            project=project, chain_type="evm", address=EVM_ADDR
        )
        DifferRecipientAddress.objects.create(
            project=project, chain_type="tron", address=TRON_ADDR
        )
        response = client.get(
            _list_url(project) + "?chain_type=tron",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200
        rows = response.json()
        assert {row["chain_type"] for row in rows} == {"tron"}

    def test_cannot_delete_other_projects_address(self, client, project, other_project):
        addr = DifferRecipientAddress.objects.create(
            project=other_project, chain_type="evm", address=EVM_ADDR
        )
        # 用本项目作用域去删他项目地址 → 404，且地址不被删除
        response = client.delete(
            _detail_url(project, addr.pk), HTTP_AUTHORIZATION=AUTH_HEADER
        )
        assert response.status_code == 404
        assert DifferRecipientAddress.objects.filter(pk=addr.pk).exists()


@pytest.mark.django_db
class TestDifferAddressMutation:
    def test_delete_removes_address(self, client, project):
        addr = DifferRecipientAddress.objects.create(
            project=project, chain_type="evm", address=EVM_ADDR
        )
        response = client.delete(
            _detail_url(project, addr.pk), HTTP_AUTHORIZATION=AUTH_HEADER
        )
        assert response.status_code == 204
        assert not DifferRecipientAddress.objects.filter(pk=addr.pk).exists()

    def test_patch_toggles_active(self, client, project):
        addr = DifferRecipientAddress.objects.create(
            project=project, chain_type="evm", address=EVM_ADDR, active=True
        )
        response = client.patch(
            _detail_url(project, addr.pk),
            data={"active": False},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )
        assert response.status_code == 200, response.content
        addr.refresh_from_db()
        assert addr.active is False
