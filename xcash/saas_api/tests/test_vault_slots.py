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
from chains.tasks import MANUAL_REFRESH_VAULT_SLOT_TASK
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
class TestVaultSlotBalanceSummary:
    def test_sums_only_this_project_positive_balances(self, client):
        project = Project.objects.create(name="summary", tron_vault=TRON_VAULT)
        other = Project.objects.create(name="summary-other")
        slot = create_tron_slot(project)
        other_slot = VaultSlot.objects.create(
            chain=slot.chain,
            usage=VaultSlotUsage.DEPOSIT,
            project=other,
            customer=Customer.objects.create(project=other, uid="other"),
            address="TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf",
            salt=b"9" * 32,
        )
        usdt = Crypto.objects.create(name="Tether USD", symbol="USDT", active=True)
        trx = slot.chain.native_coin
        for target, crypto, value, worth in (
            (slot, usdt, 120, "120.5"),
            (slot, trx, 30, "3.5"),
            # 已归集清零的快照不计入
            (slot, Crypto.objects.create(name="USD Coin", symbol="USDC"), 0, "0"),
            (other_slot, usdt, 999, "999"),
        ):
            VaultSlotBalance.objects.create(
                vault_slot=target,
                chain=slot.chain,
                crypto=crypto,
                value=Decimal(value),
                worth=Decimal(worth),
            )

        response = client.get(
            f"/saas/v1/projects/{project.appid}/vault-slot-balances/summary",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 200
        assert Decimal(response.json()["total_worth"]) == Decimal("124")
        assert response.json()["count"] == 2


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
        assert queued_args(MANUAL_REFRESH_VAULT_SLOT_TASK) == [
            {"slot_pk": slot.pk, "deploy_tx_hash": None}
        ]
        (job,) = queued_jobs(MANUAL_REFRESH_VAULT_SLOT_TASK)
        assert job.scheduled_at >= before + timedelta(
            seconds=MANUAL_OPERATION_REFRESH_DELAY_SECONDS
        )

    def test_deploy_hash_is_queued_separately_from_plain_refresh(self, client):
        # 带部署哈希的通知不能被先到的普通刷新合并掉，否则会漏补部署前原生币入账。
        project = Project.objects.create(name="refresh-deploy", tron_vault=TRON_VAULT)
        slot = create_tron_slot(project)
        deploy_hash = "ab" * 32
        for body in (
            {"chain": ChainCode.Tron, "address": TRON_SLOT},
            {
                "chain": ChainCode.Tron,
                "address": TRON_SLOT,
                "deploy_tx_hash": deploy_hash,
            },
        ):
            response = client.post(
                self.url(project),
                body,
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
            assert response.status_code == 202

        assert queued_args(MANUAL_REFRESH_VAULT_SLOT_TASK) == [
            {"slot_pk": slot.pk, "deploy_tx_hash": None},
            {"slot_pk": slot.pk, "deploy_tx_hash": "0x" + deploy_hash},
        ]

    def test_replacement_hash_is_not_merged_with_pending_original(self, client):
        project = Project.objects.create(name="refresh-replaced", tron_vault=TRON_VAULT)
        slot = create_tron_slot(project)
        original_hash = "0x" + "ab" * 32
        replacement_hash = "0x" + "cd" * 32
        for tx_hash in (
            original_hash,
            replacement_hash,
            original_hash,
            replacement_hash,
        ):
            response = client.post(
                self.url(project),
                {
                    "chain": ChainCode.Tron,
                    "address": TRON_SLOT,
                    "deploy_tx_hash": tx_hash,
                },
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
            assert response.status_code == 202

        # 原交易可能永远没有回执：替换交易必须拥有自己的补账任务，同一哈希仍幂等。
        assert queued_args(MANUAL_REFRESH_VAULT_SLOT_TASK) == [
            {"slot_pk": slot.pk, "deploy_tx_hash": original_hash},
            {"slot_pk": slot.pk, "deploy_tx_hash": replacement_hash},
        ]

    def test_hash_case_and_prefix_variants_share_one_refresh(self, client):
        project = Project.objects.create(
            name="refresh-normalized", tron_vault=TRON_VAULT
        )
        slot = create_tron_slot(project)
        raw_hash = "ab" * 32
        for tx_hash in (
            raw_hash,
            raw_hash.upper(),
            "0x" + raw_hash,
            "0x" + raw_hash.upper(),
        ):
            response = client.post(
                self.url(project),
                {
                    "chain": ChainCode.Tron,
                    "address": TRON_SLOT,
                    "deploy_tx_hash": tx_hash,
                },
                content_type="application/json",
                HTTP_AUTHORIZATION=AUTH_HEADER,
            )
            assert response.status_code == 202

        assert queued_args(MANUAL_REFRESH_VAULT_SLOT_TASK) == [
            {"slot_pk": slot.pk, "deploy_tx_hash": "0x" + raw_hash},
        ]

    def test_rejects_malformed_deploy_hash(self, client):
        project = Project.objects.create(name="bad-hash", tron_vault=TRON_VAULT)
        create_tron_slot(project)

        response = client.post(
            self.url(project),
            {"chain": ChainCode.Tron, "address": TRON_SLOT, "deploy_tx_hash": "nothex"},
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH_HEADER,
        )

        assert response.status_code == 400

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
        assert queued_jobs(MANUAL_REFRESH_VAULT_SLOT_TASK) == []

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
