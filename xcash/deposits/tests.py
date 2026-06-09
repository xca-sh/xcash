from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
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
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from chains.models import Wallet
from chains.tests_fixtures import make_evm_chain
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from deposits.service import DepositService
from deposits.viewsets import wait_deposit_address_deployed
from evm.models import EvmTxTask
from projects.models import Customer
from projects.models import Project


class DepositServiceCoreTests(TestCase):
    """Deposit 不再维护独立状态机，确认状态完全取自其 Transfer。"""

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

        created = DepositService.try_match_deposit_transfer(transfer)

        self.assertFalse(created)

    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_try_match_deposit_transfer_does_not_create_deposit_or_schedule_collect(
        self, schedule_collect
    ):
        context = create_deposit_context()

        created = DepositService.try_match_deposit_transfer(context.transfer)

        self.assertTrue(created)
        self.assertFalse(Deposit.objects.filter(transfer=context.transfer).exists())
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

    def test_try_match_deposit_transfer_matches_tron_vault_slot(self):
        context = create_tron_deposit_context()

        created = DepositService.try_match_deposit_transfer(context.transfer)

        self.assertTrue(created)
        self.assertFalse(Deposit.objects.filter(transfer=context.transfer).exists())
        context.transfer.refresh_from_db()
        self.assertEqual(context.transfer.type, TransferType.Deposit)


class DepositAddressDebugWaitTests(SimpleTestCase):
    @patch("deposits.viewsets.time.sleep", return_value=None)
    def test_wait_deposit_address_deployed_polls_until_code_exists(self, sleep_mock):
        get_code = SimpleNamespace(side_effects=[b"", b"", b"\x01"])

        def fake_get_code(address):
            get_code.address = address
            return get_code.side_effects.pop(0)

        chain = SimpleNamespace(
            w3=SimpleNamespace(eth=SimpleNamespace(get_code=fake_get_code))
        )
        address = "0x0000000000000000000000000000000000000001"

        wait_deposit_address_deployed(chain=chain, address=address)

        self.assertEqual(get_code.address, address)
        self.assertEqual(sleep_mock.call_count, 2)


class DepositNotificationTests(TestCase):
    @patch("deposits.service.send_saas_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(DepositService, "schedule_collect_for_completed_deposit")
    def test_create_confirmed_deposit_creates_deposit_and_schedules_collect(
        self, schedule_collect, create_event_mock, send_saas_callback_mock
    ):
        context = create_deposit_context()

        deposit = DepositService.create_confirmed_deposit(context.transfer)

        self.assertIsNotNone(deposit)
        self.assertEqual(deposit.customer, context.customer)
        schedule_collect.assert_called_once_with(deposit)
        create_event_mock.assert_called_once()
        send_saas_callback_mock.assert_called_once()

    @patch("deposits.service.send_saas_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(
        DepositService,
        "schedule_collect_for_completed_deposit",
        side_effect=RuntimeError("collect failed"),
    )
    def test_confirm_deposit_still_notifies_when_collect_schedule_fails(
        self, schedule_collect, create_event_mock, send_saas_callback_mock
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
        send_saas_callback_mock.assert_called_once()

    @patch("deposits.service.send_saas_callback")
    @patch("deposits.service.WebhookService.create_event")
    def test_confirm_deposit_does_not_create_collect_task_for_native(
        self, create_event_mock, send_saas_callback_mock
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
    def test_schedule_collect_for_completed_deposit_allows_tron_native(
        self, schedule_collect
    ):
        context = create_tron_deposit_context(native=True)
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
        )

        scheduled = DepositService.schedule_collect_for_completed_deposit(deposit)

        self.assertTrue(scheduled)
        schedule_collect.assert_called_once_with(deposit.pk)

    @patch("deposits.service.send_saas_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_confirm_deposit_dispatches_tron_collect_scheduler(
        self,
        schedule_collect,
        create_event_mock,
        send_saas_callback_mock,
    ):
        context = create_tron_deposit_context()
        deposit = Deposit.objects.create(
            customer=context.customer,
            transfer=context.transfer,
            worth=Decimal("1"),
        )

        DepositService.confirm_deposit(deposit)

        schedule_collect.assert_called_once_with(deposit.pk)
        create_event_mock.assert_called_once()
        send_saas_callback_mock.assert_called_once()

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

    @patch("deposits.service.send_saas_callback")
    @patch("deposits.service.WebhookService.create_event")
    @patch.object(VaultSlot, "schedule_collect_for_deposit")
    def test_confirm_deposit_emits_completed_webhook(
        self, schedule_collect, create_event_mock, send_saas_callback_mock
    ):
        project = Project.objects.create(
            name="DemoConfirm",
        )
        customer = Customer.objects.create(project=project, uid="customer-confirm")
        chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=False,
        )
        Chain.objects.filter(pk=chain.pk).update(
            rpc="http://evm.invalid",
            active=True,
        )
        chain.refresh_from_db()
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
        send_saas_callback_mock.assert_called_once_with(
            SaasCallback(
                event=CallbackEvent.DEPOSIT_CONFIRMED,
                appid=project.appid,
                sys_no=deposit.sys_no,
                worth="1.000000",
                currency=crypto.symbol,
            )
        )


def create_deposit_context(*, native: bool = False, confirmed: bool = True):
    wallet = Wallet.objects.create()
    project = Project.objects.create(name="DepositTestProject")
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
        active=False,
    )
    Chain.objects.filter(pk=chain.pk).update(
        rpc="http://evm.invalid",
        active=True,
    )
    chain.refresh_from_db()
    native_coin = chain.native_coin
    crypto = native_coin
    if not native:
        crypto = Crypto.objects.create(
            name="Deposit Test Token",
            symbol="DTT",
            coingecko_id="deposit-test-token",
        )
        CryptoOnChain.objects.create(
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


def create_tron_deposit_context(*, native=False):
    wallet = Wallet.objects.create()
    project = Project.objects.create(name="TronDepositTestProject")
    customer = Customer.objects.create(project=project, uid="tron-deposit-customer")
    chain = Chain.objects.create(
        code=ChainCode.Tron,
        rpc="",
        tron_api_key="tron-key",
        active=True,
    )
    if native:
        crypto = chain.native_coin
    else:
        crypto = Crypto.objects.create(
            name="Tron Deposit USDT",
            symbol="USDT",
            coingecko_id="tron-deposit-usdt",
        )
        CryptoOnChain.objects.create(
            crypto=crypto,
            chain=chain,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
    slot_address = "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb"
    VaultSlot.objects.create(
        customer=customer,
        chain=chain,
        usage=VaultSlotUsage.DEPOSIT,
        address=slot_address,
        salt=b"\x22" * 32,
    )
    transfer = Transfer.objects.create(
        chain=chain,
        block=1,
        block_hash="b" * 64,
        hash=("4" if native else "3") * 64,
        crypto=crypto,
        from_address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        to_address=slot_address,
        value="1000000",
        amount=Decimal("1"),
        timestamp=1,
        datetime=timezone.now(),
        status=TransferStatus.CONFIRMED,
    )
    return SimpleNamespace(
        wallet=wallet,
        project=project,
        customer=customer,
        chain=chain,
        crypto=crypto,
        transfer=transfer,
    )


class DepositAddressTestnetGateTests(TestCase):
    """充币地址门控：项目 is_test 与链 is_testnet 必须一致，否则拒绝。"""

    def test_address_rejects_chain_mismatching_project_is_test(self):
        from rest_framework.permissions import AllowAny
        from rest_framework.test import APIRequestFactory

        from common.consts import APPID_HEADER
        from common.error_codes import ErrorCode
        from deposits.viewsets import DepositViewSet

        # 测试项目 + 主网链（is_testnet=False）→ 不匹配，应被拒。
        project = Project.objects.create(name="Gate Deposit Project", is_test=True)
        make_evm_chain(code=ChainCode.Ethereum)

        factory = APIRequestFactory()
        header_key = "HTTP_" + APPID_HEADER.upper().replace("-", "_")
        request = factory.get(
            "/deposit/address",
            {"uid": "u1", "chain": ChainCode.Ethereum, "crypto": "USDT"},
            **{header_key: project.appid},
        )

        # address action 在生产即 AllowAny；as_view() 直调不会套用 action 级权限，
        # 故在类级别对齐为 AllowAny，避免被默认 IsAuthenticated 在到达门控前拦下。
        with (
            patch.object(DepositViewSet, "permission_classes", [AllowAny]),
            patch("deposits.viewsets.check_saas_permission", return_value=None),
        ):
            response = DepositViewSet.as_view({"get": "address"})(request)

        self.assertEqual(response.status_code, ErrorCode.INVALID_CHAIN.status)
        self.assertEqual(response.data["code"], ErrorCode.INVALID_CHAIN.code)
