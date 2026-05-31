from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from web3 import Web3

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import Wallet
from currencies.models import ChainToken
from currencies.models import Crypto
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from deposits.service import DepositService
from evm.models import EvmTxTask
from evm.models import VaultSlot
from evm.models import VaultSlotUsage
from projects.models import Customer
from projects.models import Project


class DepositServiceCoreTests(TestCase):
    """Deposit 不再维护独立状态机，确认状态完全取自其 Transfer。"""

    def test_drop_deposit_noop_when_transfer_unconfirmed(self):
        # 未确认充值由 Transfer.drop 的级联删除收口，drop_deposit 不抛错、不显式删除。
        deposit = SimpleNamespace(confirmed=False)

        DepositService.drop_deposit(deposit)

    def test_drop_deposit_rejects_confirmed(self):
        # 已确认充值不允许随 Transfer 回退被静默抹除，抛错中断 drop 事务。
        deposit = SimpleNamespace(confirmed=True)

        with self.assertRaises(DepositStatusError):
            DepositService.drop_deposit(deposit)

    def test_content_property_handles_null_customer(self):
        transfer = SimpleNamespace(
            chain=SimpleNamespace(code="ethereum"),
            block=100,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "a" * 64,
            crypto=SimpleNamespace(symbol="USDT"),
            amount=Decimal("1.5"),
        )
        fake_deposit = SimpleNamespace(
            sys_no="DXC-test",
            customer=None,
            transfer=transfer,
            confirmed=False,
            risk_level=None,
            risk_score=None,
        )

        content = Deposit.content.fget(fake_deposit)

        self.assertIsNone(content["data"]["uid"])
        self.assertEqual(content["data"]["sys_no"], "DXC-test")
        self.assertEqual(content["data"]["chain"], "ethereum")


class DepositCreationTests(TestCase):
    def test_inactive_crypto_transfer_does_not_create_deposit(self):
        transfer = SimpleNamespace(
            chain=SimpleNamespace(type=ChainType.EVM),
            crypto=SimpleNamespace(active=False),
        )

        created = DepositService.try_create_deposit(transfer)

        self.assertFalse(created)

    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_try_create_deposit_does_not_schedule_collect(self, schedule_collect):
        context = create_deposit_context()

        created = DepositService.try_create_deposit(context.transfer)

        self.assertTrue(created)
        schedule_collect.assert_not_called()

    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_initialize_deposit_does_not_schedule_collect(self, schedule_collect):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
        )

        DepositService.initialize_deposit(deposit)

        schedule_collect.assert_not_called()


class DepositNotificationTests(TestCase):
    @patch("deposits.service.send_internal_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(DepositService, "schedule_collect_for_completed_deposit")
    def test_confirm_deposit_schedules_collect_for_erc20(
        self, schedule_collect, create_event_mock, send_internal_callback_mock
    ):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        schedule_collect.assert_called_once_with(deposit)

    @patch("deposits.service.send_internal_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(
        DepositService,
        "schedule_collect_for_completed_deposit",
        side_effect=RuntimeError("collect failed"),
    )
    def test_confirm_deposit_still_notifies_when_collect_schedule_fails(
        self, schedule_collect, create_event_mock, send_internal_callback_mock
    ):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        # 归集调度失败不应影响确认状态（取自 Transfer）与后续通知/回调。
        self.assertTrue(deposit.confirmed)
        schedule_collect.assert_called_once_with(deposit)
        create_event_mock.assert_called_once()
        send_internal_callback_mock.assert_called_once()

    @patch("deposits.service.send_internal_callback")
    @patch("deposits.service.WebhookService.create_event")
    def test_confirm_deposit_does_not_create_collect_task_for_native(
        self, create_event_mock, send_internal_callback_mock
    ):
        context = create_deposit_context(native=True)
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        self.assertFalse(EvmTxTask.objects.exists())

    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_schedule_collect_for_completed_deposit_calls_collect_for_erc20(
        self, schedule_collect
    ):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
        )

        scheduled = DepositService.schedule_collect_for_completed_deposit(deposit)

        self.assertTrue(scheduled)
        schedule_collect.assert_called_once_with(deposit.pk)

    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_schedule_collect_for_completed_deposit_skips_native(
        self, schedule_collect
    ):
        context = create_deposit_context(native=True)
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
        )

        scheduled = DepositService.schedule_collect_for_completed_deposit(deposit)

        self.assertFalse(scheduled)
        schedule_collect.assert_not_called()

    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_schedule_collect_for_completed_deposit_rejects_unconfirmed(
        self, schedule_collect
    ):
        context = create_deposit_context(confirmed=False)
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
        )

        with self.assertRaises(DepositStatusError):
            DepositService.schedule_collect_for_completed_deposit(deposit)

        schedule_collect.assert_not_called()

    @patch("deposits.service.send_internal_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_confirm_deposit_emits_completed_webhook(
        self, schedule_collect, create_event_mock, send_internal_callback_mock
    ):
        project = Project.objects.create(
            name="DemoConfirm",
            wallet=Wallet.objects.create(),
            pre_notify=True,
        )
        customer = Customer.objects.create(project=project, uid="customer-confirm")
        chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        crypto = Crypto.objects.create(
            name="Tether Confirm",
            symbol="USDTC",
            coingecko_id="tether-confirm",
        )
        transfer = Transfer.objects.create(
            chain=chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "4" * 64,
            crypto=crypto,
            from_address="0x0000000000000000000000000000000000000002",
            to_address="0x0000000000000000000000000000000000000011",
            value="1",
            amount=Decimal("1"),
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMED,
            type=TransferType.Deposit,
        )
        deposit = Deposit.objects.create(
            customer=customer,
            transfer=transfer,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        create_event_mock.assert_called_once()
        payload = create_event_mock.call_args.kwargs["payload"]
        self.assertEqual(payload["type"], "deposit")
        self.assertEqual(payload["data"]["sys_no"], deposit.sys_no)
        self.assertEqual(payload["data"]["uid"], customer.uid)
        self.assertTrue(payload["data"]["confirmed"])
        send_internal_callback_mock.assert_called_once_with(
            event="deposit.confirmed",
            appid=project.appid,
            sys_no=deposit.sys_no,
            worth="1.000000",
            currency=crypto.symbol,
        )


def create_deposit_context(*, native: bool = False, confirmed: bool = True):
    wallet = Wallet.objects.create()
    project = Project.objects.create(name="DepositTestProject", wallet=wallet)
    customer = Customer.objects.create(project=project, uid="deposit-test-customer")
    vault = Address.objects.create(
        wallet=wallet,
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
        address_index=0,
        address=Web3.to_checksum_address("0x0000000000000000000000000000000000000d01"),
    )
    chain = Chain.objects.create(
        code=ChainCode.Ethereum,
        rpc="",
        active=True,
    )
    native_coin = chain.native_coin
    crypto = native_coin
    if not native:
        crypto = Crypto.objects.create(
            name="Deposit Test Token",
            symbol="DTT",
            coingecko_id="deposit-test-token",
        )
        ChainToken.objects.create(
            crypto=crypto,
            chain=chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000e20"
            ),
            decimals=6,
        )
        VaultSlot.objects.create(
            customer=customer,
            chain=chain,
            usage=VaultSlotUsage.DEPOSIT,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            salt=b"\x11" * 32,
        )
    transfer = Transfer.objects.create(
        chain=chain,
        block=1,
        block_hash="0x" + "aa" * 32,
        hash="0x" + ("1" if native else "2") * 64,
        crypto=crypto,
        from_address="0x0000000000000000000000000000000000000002",
        to_address=Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000a11"
        ),
        value="1",
        amount=Decimal("1"),
        timestamp=1,
        datetime=timezone.now(),
        status=(
            TransferStatus.CONFIRMED if confirmed else TransferStatus.CONFIRMING
        ),
    )
    return SimpleNamespace(
        wallet=wallet,
        project=project,
        customer=customer,
        vault=vault,
        chain=chain,
        crypto=crypto,
        transfer=transfer,
    )
