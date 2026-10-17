from unittest.mock import patch

import pytest
from web3 import Web3

from chains.constants import ChainCode
from chains.tests_fixtures import make_evm_chain
from common.error_codes import ErrorCode
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from projects.models import Customer
from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"
EVM_VAULT = Web3.to_checksum_address("0x00000000000000000000000000000000000000f1")


@pytest.mark.django_db
class TestSaasDepositEndpoint:
    def test_address_requires_crypto(self, client, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(name="saas-deposit-project")
        make_evm_chain(code=ChainCode.Ethereum)

        response = client.get(
            f"/saas/v1/projects/{project.appid}/deposits/address",
            {"uid": "user-1", "chain": ChainCode.Ethereum},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == ErrorCode.INVALID_CRYPTO.status
        assert response.json() == {
            "code": ErrorCode.INVALID_CRYPTO.code,
            "message": "无效加密货币",
            "detail": "crypto is required",
        }
        assert not Customer.objects.filter(project=project, uid="user-1").exists()

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


def _usdt_on(chain):
    crypto, _ = Crypto.objects.get_or_create(
        symbol="USDT",
        defaults={"name": "Tether", "prices": {"USD": "1"}, "coingecko_id": "tether"},
    )
    CryptoOnChain.objects.create(
        crypto=crypto,
        chain=chain,
        address=Web3.to_checksum_address(
            f"0x{chain.pk:040x}",
        ),
        decimals=6,
    )
    return crypto


@pytest.mark.django_db
class TestSaasDepositAddressSharesMerchantChecks:
    """SaaS 内部充值地址接口与 /v1 商户接口共用同一套校验。"""

    def _get(self, client, project, **params):
        return client.get(
            f"/saas/v1/projects/{project.appid}/deposits/address",
            {"uid": "user-1", "crypto": "USDT", **params},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

    def test_mainnet_project_cannot_use_testnet_chain(self, client):
        # 主网项目拿到测试网地址会把两类代币混进同一个项目的账目。
        project = Project.objects.create(name="saas-mainnet", evm_vault=EVM_VAULT)
        _usdt_on(make_evm_chain(code=ChainCode.Sepolia))

        response = self._get(client, project, chain=ChainCode.Sepolia)

        assert response.status_code == ErrorCode.INVALID_CHAIN.status
        assert response.json()["code"] == ErrorCode.INVALID_CHAIN.code
        assert not Customer.objects.filter(project=project).exists()

    def test_chain_type_no_longer_picks_a_chain(self, client):
        # 旧的 chain_type 分支按主键取第一条 EVM 链，可能取到测试网；SaaS 只传 chain。
        project = Project.objects.create(name="saas-chain-type", evm_vault=EVM_VAULT)
        _usdt_on(make_evm_chain(code=ChainCode.Ethereum))

        response = self._get(client, project, chain_type="evm")

        assert response.status_code == ErrorCode.INVALID_CHAIN.status
        assert not Customer.objects.filter(project=project).exists()

    def test_customer_limit_applies(self, client):
        project = Project.objects.create(name="saas-limit", evm_vault=EVM_VAULT)
        _usdt_on(make_evm_chain(code=ChainCode.Ethereum))
        Customer.objects.create(project=project, uid="existing")

        with patch("deposits.service.get_saas_deposit_customer_limit", return_value=1):
            response = self._get(client, project, chain=ChainCode.Ethereum)

        assert response.status_code == ErrorCode.DEPOSIT_CUSTOMER_LIMIT_REACHED.status
        assert not Customer.objects.filter(project=project, uid="user-1").exists()
