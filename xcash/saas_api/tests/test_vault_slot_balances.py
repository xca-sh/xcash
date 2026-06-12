from decimal import Decimal

import pytest

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Chain
from chains.models import VaultSlot
from chains.models import VaultSlotBalance
from chains.models import VaultSlotUsage
from currencies.models import Crypto
from projects.models import Customer
from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"


@pytest.mark.django_db
class TestSaasVaultSlotBalanceEndpoint:
    def test_lists_only_project_vault_slot_balances(self, client, settings):
        settings.SAAS_API_TOKEN = "test-saas-token"
        project = Project.objects.create(name="balance-project")
        other_project = Project.objects.create(name="other-balance-project")
        customer = Customer.objects.create(project=project, uid="user-1")
        customer_2 = Customer.objects.create(project=project, uid="user-3")
        other_customer = Customer.objects.create(project=other_project, uid="user-2")
        chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            type=ChainType.EVM,
            rpc="",
            active=False,
        )
        crypto = Crypto.objects.create(name="Tether USD", symbol="USDT", active=True)
        slot = VaultSlot.objects.create(
            chain=chain,
            usage=VaultSlotUsage.DEPOSIT,
            project=project,
            customer=customer,
            address="0x1111111111111111111111111111111111111111",
            salt=b"1" * 32,
        )
        other_slot = VaultSlot.objects.create(
            chain=chain,
            usage=VaultSlotUsage.DEPOSIT,
            project=other_project,
            customer=other_customer,
            address="0x2222222222222222222222222222222222222222",
            salt=b"2" * 32,
        )
        slot_2 = VaultSlot.objects.create(
            chain=chain,
            usage=VaultSlotUsage.DEPOSIT,
            project=project,
            customer=customer_2,
            address="0x3333333333333333333333333333333333333333",
            salt=b"3" * 32,
        )
        VaultSlotBalance.objects.create(
            vault_slot=slot,
            chain=chain,
            crypto=crypto,
            value=Decimal("123000000"),
            amount=Decimal("123"),
            worth=Decimal("123"),
            synced_block_number=100,
            last_tx_hash="0x" + "a" * 64,
        )
        VaultSlotBalance.objects.create(
            vault_slot=slot_2,
            chain=chain,
            crypto=crypto,
            value=Decimal("1000000"),
            amount=Decimal("1"),
            worth=Decimal("1"),
        )
        VaultSlotBalance.objects.create(
            vault_slot=other_slot,
            chain=chain,
            crypto=crypto,
            value=Decimal("456000000"),
            amount=Decimal("456"),
            worth=Decimal("456"),
        )

        response = client.get(
            f"/saas/v1/projects/{project.appid}/vault-slot-balances",
            {"ordering": "-worth"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        item = data["results"][0]
        assert item["vault_slot_address"] == "0x1111111111111111111111111111111111111111"
        assert item["usage"] == VaultSlotUsage.DEPOSIT
        assert item["customer_uid"] == "user-1"
        assert item["chain"] == ChainCode.Ethereum
        assert item["crypto"] == "USDT"
        assert item["value"] == "123000000"
        assert item["amount"] == "123"
        assert item["worth"] == "123"
        assert item["synced_block_number"] == 100

        response = client.get(
            f"/saas/v1/projects/{project.appid}/vault-slot-balances",
            {"ordering": "worth"},
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 200
        assert response.json()["results"][0]["vault_slot_address"] == (
            "0x3333333333333333333333333333333333333333"
        )
