from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Chain
from chains.models import VaultSlot
from chains.models import VaultSlotBalance
from chains.models import VaultSlotUsage
from chains.tasks import REFRESH_VAULT_SLOT_AFTER_MANUAL_OPERATION_TASK
from chains.vault_slots import MANUAL_OPERATION_REFRESH_DELAY_SECONDS
from chains.vault_slots import refresh_vault_slot_from_chain
from common.tests.job_helpers import queued_args
from common.tests.job_helpers import queued_jobs
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from projects.models import Customer
from projects.models import Project

AUTH_HEADER = "Bearer test-saas-token"
TRON_VAULT = "TLsV52sRDL79HXGGm9yzwKibb6BeruhUzy"
TRON_SLOT = "TJCnKsPa7y5okkXvQAidZBzqx3QyQ6sxMW"
TRON_USDT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


@pytest.fixture
def saas_token(settings):
    settings.SAAS_API_TOKEN = "test-saas-token"


def create_tron_slot(project: Project, *, uid: str = "user-1") -> VaultSlot:
    chain = Chain.objects.create(
        code=ChainCode.Tron, type=ChainType.TRON, rpc="", active=False
    )
    customer = Customer.objects.create(project=project, uid=uid)
    return VaultSlot.objects.create(
        chain=chain,
        usage=VaultSlotUsage.DEPOSIT,
        project=project,
        customer=customer,
        address=TRON_SLOT,
        salt=bytes(range(32)),
    )


@pytest.mark.django_db
@pytest.mark.usefixtures("saas_token")
class TestVaultSlotBalanceWalletFields:
    def test_tron_balances_carry_deploy_and_collect_arguments(self, client):
        project = Project.objects.create(name="wallet-fields", tron_vault=TRON_VAULT)
        slot = create_tron_slot(project)
        usdt = Crypto.objects.create(name="Tether USD", symbol="USDT", active=True)
        CryptoOnChain.objects.create(
            crypto=usdt, chain=slot.chain, address=TRON_USDT, decimals=6
        )
        trx = slot.chain.native_coin
        for crypto in (usdt, trx):
            VaultSlotBalance.objects.create(
                vault_slot=slot,
                chain=slot.chain,
                crypto=crypto,
                value=Decimal(1),
                amount=Decimal(1),
                worth=Decimal(1),
            )

        response = client.get(
            f"/saas/v1/projects/{project.appid}/vault-slot-balances",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 200
        items = {item["crypto"]: item for item in response.json()["results"]}
        usdt_item = items["USDT"]
        assert usdt_item["chain_type"] == ChainType.TRON
        assert usdt_item["chain_id"] is None
        assert usdt_item["is_deployed"] is False
        assert usdt_item["salt"] == "0x" + bytes(range(32)).hex()
        assert usdt_item["vault"] == TRON_VAULT
        assert usdt_item["factory"] == (
            slot.chain.vault_slot_contract_addresses().factory
        )
        assert usdt_item["token_address"] == TRON_USDT
        # 本链原生币归集走 collect(address(0))。
        assert items["TRX"]["token_address"] == (
            "0x0000000000000000000000000000000000000000"
        )


@pytest.mark.django_db
@pytest.mark.usefixtures("saas_token")
class TestVaultSlotRefreshEndpoint:
    def url(self, project: Project) -> str:
        return f"/saas/v1/projects/{project.appid}/vault-slots/refresh"

    def test_schedules_single_delayed_refresh(self, client):
        project = Project.objects.create(name="refresh", tron_vault=TRON_VAULT)
        slot = create_tron_slot(project)
        before = timezone.now()

        for _ in range(2):
            response = client.post(
                self.url(project),
                {"chain": ChainCode.Tron, "address": TRON_SLOT},
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
            assert response.status_code == 202

        # 重复通知合并为一个待执行任务，且延迟到约 60 秒后执行。
        assert queued_args(REFRESH_VAULT_SLOT_AFTER_MANUAL_OPERATION_TASK) == [
            {"slot_pk": slot.pk}
        ]
        (job,) = queued_jobs(REFRESH_VAULT_SLOT_AFTER_MANUAL_OPERATION_TASK)
        assert job.scheduled_at >= before + timedelta(
            seconds=MANUAL_OPERATION_REFRESH_DELAY_SECONDS
        )

    def test_rejects_slot_of_other_project(self, client):
        owner = Project.objects.create(name="owner", tron_vault=TRON_VAULT)
        other = Project.objects.create(name="other")
        create_tron_slot(owner)

        response = client.post(
            self.url(other),
            {"chain": ChainCode.Tron, "address": TRON_SLOT},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 404
        assert queued_jobs(REFRESH_VAULT_SLOT_AFTER_MANUAL_OPERATION_TASK) == []

    def test_rejects_missing_fields(self, client):
        project = Project.objects.create(name="bad-request")

        response = client.post(
            self.url(project),
            {"chain": ChainCode.Tron},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400


@pytest.mark.django_db
class TestRefreshVaultSlotFromChain:
    def test_marks_deployed_and_refreshes_every_balance(self):
        project = Project.objects.create(name="refresh-task", tron_vault=TRON_VAULT)
        slot = create_tron_slot(project)
        usdt = Crypto.objects.create(name="Tether USD", symbol="USDT", active=True)
        VaultSlotBalance.objects.create(
            vault_slot=slot, chain=slot.chain, crypto=usdt, value=Decimal(5)
        )

        with (
            patch("tron.vault_slots.is_deployed_on_chain", return_value=True),
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance"
            ) as refresh_balance,
        ):
            refresh_vault_slot_from_chain(slot.pk)

        slot.refresh_from_db()
        assert slot.is_deployed is True
        refresh_balance.assert_called_once()
        assert refresh_balance.call_args.kwargs["crypto"] == usdt

    def test_keeps_undeployed_when_chain_has_no_code(self):
        project = Project.objects.create(name="not-deployed", tron_vault=TRON_VAULT)
        slot = create_tron_slot(project)

        with (
            patch("tron.vault_slots.is_deployed_on_chain", return_value=False),
            patch("chains.vault_slot_balances.refresh_vault_slot_balance"),
        ):
            refresh_vault_slot_from_chain(slot.pk)

        slot.refresh_from_db()
        assert slot.is_deployed is False
