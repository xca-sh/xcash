from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from chains.models import Address
from chains.models import AddressUsage
from chains.models import TransferType
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import Wallet
from currencies.models import Crypto
from currencies.models import ChainToken
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from deposits.models import DepositStatus
from deposits.service import DepositService
from evm.models import DepositSlot
from evm.models import EvmTxTask
from projects.models import Project
from users.models import Customer
from web3 import Web3


class DepositServiceCoreTests(TestCase):
    """仅保留与当前 DepositSlot 充值生命周期无关的 Deposit 核心行为。"""

    @patch("deposits.service.Deposit.objects")
    def test_confirm_deposit_idempotent_when_already_completed(
        self, deposit_objects_mock
    ):
        deposit = SimpleNamespace(
            pk=1, status=DepositStatus.COMPLETED, refresh_from_db=Mock()
        )

        DepositService.confirm_deposit(deposit)

        deposit_objects_mock.select_for_update.return_value.filter.assert_called_once_with(
            pk=1
        )

    @patch("deposits.service.Deposit.objects")
    def test_drop_deposit_idempotent_when_already_deleted(self, deposit_objects_mock):
        deposit_objects_mock.select_for_update.return_value.filter.return_value.exists.return_value = (
            False
        )
        deposit = SimpleNamespace(pk=1)

        DepositService.drop_deposit(deposit)

    @patch("deposits.service.Deposit.objects")
    def test_drop_deposit_rejects_non_confirming_status(self, deposit_objects_mock):
        deposit = SimpleNamespace(pk=1, status=DepositStatus.COMPLETED)
        deposit.refresh_from_db = Mock()
        deposit.delete = Mock()
        deposit_objects_mock.select_for_update.return_value.filter.return_value.exists.return_value = (
            True
        )

        with self.assertRaises(DepositStatusError):
            DepositService.drop_deposit(deposit)

    def test_content_property_handles_null_customer(self):
        transfer = SimpleNamespace(
            chain=SimpleNamespace(code="eth"),
            block=100,
            hash="0x" + "a" * 64,
            crypto=SimpleNamespace(symbol="USDT"),
            amount=Decimal("1.5"),
        )
        fake_deposit = SimpleNamespace(
            sys_no="DXC-test",
            customer=None,
            transfer=transfer,
            status=DepositStatus.CONFIRMING,
            risk_level=None,
            risk_score=None,
        )

        content = Deposit.content.fget(fake_deposit)

        self.assertIsNone(content["data"]["uid"])
        self.assertEqual(content["data"]["sys_no"], "DXC-test")
        self.assertEqual(content["data"]["chain"], "eth")


class DepositCreationTests(TestCase):
    def test_inactive_placeholder_transfer_does_not_create_deposit(self):
        transfer = SimpleNamespace(
            chain=SimpleNamespace(type=ChainType.EVM),
            crypto=SimpleNamespace(active=False),
        )

        created = DepositService.try_create_deposit(transfer)

        self.assertFalse(created)

    @patch.object(DepositSlot, "schedule_collect_for_deposit")
    def test_try_create_deposit_does_not_schedule_collect(self, schedule_collect):
        context = create_deposit_context()

        created = DepositService.try_create_deposit(context.transfer)

        self.assertTrue(created)
        schedule_collect.assert_not_called()

    @patch.object(DepositSlot, "schedule_collect_for_deposit")
    def test_initialize_deposit_does_not_schedule_collect(self, schedule_collect):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            status=DepositStatus.CONFIRMING,
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
            status=DepositStatus.CONFIRMING,
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
    def test_confirm_deposit_keeps_completed_when_collect_schedule_fails(
        self, schedule_collect, create_event_mock, send_internal_callback_mock
    ):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            status=DepositStatus.CONFIRMING,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        deposit.refresh_from_db()
        self.assertEqual(deposit.status, DepositStatus.COMPLETED)
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
            status=DepositStatus.CONFIRMING,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        self.assertFalse(EvmTxTask.objects.exists())

    @patch.object(DepositSlot, "schedule_collect_for_deposit")
    def test_schedule_collect_for_completed_deposit_calls_collect_for_erc20(
        self, schedule_collect
    ):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            status=DepositStatus.COMPLETED,
        )

        scheduled = DepositService.schedule_collect_for_completed_deposit(deposit)

        self.assertTrue(scheduled)
        schedule_collect.assert_called_once_with(deposit.pk)

    @patch.object(DepositSlot, "schedule_collect_for_deposit")
    def test_schedule_collect_for_completed_deposit_skips_native(
        self, schedule_collect
    ):
        context = create_deposit_context(native=True)
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            status=DepositStatus.COMPLETED,
        )

        scheduled = DepositService.schedule_collect_for_completed_deposit(deposit)

        self.assertFalse(scheduled)
        schedule_collect.assert_not_called()

    @patch.object(DepositSlot, "schedule_collect_for_deposit")
    def test_schedule_collect_for_completed_deposit_rejects_uncompleted(
        self, schedule_collect
    ):
        context = create_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            status=DepositStatus.CONFIRMING,
        )

        with self.assertRaises(DepositStatusError):
            DepositService.schedule_collect_for_completed_deposit(deposit)

        schedule_collect.assert_not_called()

    @patch("deposits.service.send_internal_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(DepositSlot, "schedule_collect_for_deposit")
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
            name="EthereumConfirm",
            code="eth-confirm",
            type=ChainType.EVM,
            native_coin=Crypto.objects.create(
                name="Ethereum Confirm",
                symbol="ETHC",
                coingecko_id="ethereum-confirm",
            ),
            chain_id=101,
            rpc="http://localhost:8545",
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
            hash="0x" + "4" * 64,
            event_id="erc20:4",
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
            status=DepositStatus.CONFIRMING,
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


def create_deposit_context(*, native: bool = False):
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
    native_coin = Crypto.objects.create(
        name="Deposit Test Native",
        symbol="DTN",
        coingecko_id="deposit-test-native",
    )
    chain = Chain.objects.create(
        name="Deposit Test Chain",
        code="deposit-test-chain",
        type=ChainType.EVM,
        native_coin=native_coin,
        chain_id=818_181,
        rpc="http://deposit-test.local",
        active=True,
    )
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
        )
        DepositSlot.objects.create(
            customer=customer,
            chain=chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            vault_address=vault.address,
            salt=b"\x11" * 32,
        )
    transfer = Transfer.objects.create(
        chain=chain,
        block=1,
        hash="0x" + ("1" if native else "2") * 64,
        event_id="native:1" if native else "erc20:2",
        crypto=crypto,
        from_address="0x0000000000000000000000000000000000000002",
        to_address=Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000a11"
        ),
        value="1",
        amount=Decimal("1"),
        timestamp=1,
        datetime=timezone.now(),
        status=TransferStatus.CONFIRMED,
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
