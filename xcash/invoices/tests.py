import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.db import IntegrityError
from django.db import close_old_connections
from django.db import connection
from django.db import connections
from django.test import TestCase
from django.test import TransactionTestCase
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIRequestFactory
from web3 import Web3

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Chain
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from common.error_codes import ErrorCode
from common.exceptions import APIError
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from currencies.models import Fiat
from invoices.exceptions import InvoiceStatusError
from invoices.models import DifferRecipientAddress
from invoices.models import Invoice
from invoices.models import InvoiceProtocol
from invoices.models import InvoiceStatus
from invoices.serializers import InvoiceCreateSerializer
from invoices.service import InvoiceService
from invoices.tasks import check_expired
from invoices.tasks import fallback_invoice_expired
from invoices.viewsets import InvoiceViewSet
from projects.models import InvoiceReceivingMode
from projects.models import Project
from users.models import User


def create_active_evm_test_chain(*, code=ChainCode.Ethereum) -> Chain:
    chain = Chain.objects.create(code=code, rpc="", active=False)
    Chain.objects.filter(pk=chain.pk).update(
        rpc="http://evm-chain.invalid",
        active=True,
    )
    chain.refresh_from_db()
    return chain


def disable_vault_slot_deploy_schedule(test_case) -> None:
    patcher = patch("chains.models.VaultSlot.schedule_deploy", return_value=None)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class InvoiceTestMixin:
    """共享的测试基础数据构造 mixin，避免各测试类重复创建 User/Project/Crypto/Chain 等。"""

    def setup_base_fixtures(
        self,
        *,
        username: str = "merchant",
        project_name: str = "TestProject",
        crypto_symbol: str = "USDT",
        chain_name: str = ChainCode.Ethereum,
    ):
        self.user = User.objects.create(username=username)
        self.project = Project.objects.create(
            name=project_name,
        )
        self.crypto = Crypto.objects.create(
            name=f"{crypto_symbol} Token",
            symbol=crypto_symbol,
            prices={"USD": "1"},
            coingecko_id=f"{crypto_symbol.lower()}-test",
        )
        self.chain = create_active_evm_test_chain(code=chain_name)
        Fiat.objects.get_or_create(code="USD")
        self.crypto_on_chain = CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000010"
            ),
            decimals=6,
        )

    def create_test_invoice(self, *, out_no: str = "test-order", **kwargs) -> Invoice:
        defaults = {
            "project": self.project,
            "out_no": out_no,
            "title": "Test invoice",
            "currency": self.crypto.symbol,
            "amount": Decimal("10"),
            "methods": {self.crypto.symbol: [self.chain.code]},
            "expires_at": timezone.now() + timedelta(minutes=10),
        }
        defaults.update(kwargs)
        return Invoice.objects.create(**defaults)


class InvoiceInitializationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="merchant")
        self.project = Project.objects.create(
            name="Demo",
        )
        self.eth = Crypto.objects.create(
            name="Ethereum",
            symbol="ETH",
            coingecko_id="ethereum",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.Ethereum)


class InvoicePaymentSelectionTests(TestCase):
    def setUp(self):
        disable_vault_slot_deploy_schedule(self)
        self.user = User.objects.create(username="merchant-payments")
        self.project = Project.objects.create(
            name="SlotProject",
            evm_vault=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000A1"
            ),
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        self.crypto = Crypto.objects.create(
            name="Tether USD",
            symbol="USDT",
            coingecko_id="tether-invoice-slots",
        )
        self.chain_a = create_active_evm_test_chain(code=ChainCode.Ethereum)
        self.chain_b = create_active_evm_test_chain(code=ChainCode.BSC)
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain_a,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000C1"
            ),
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain_b,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000C2"
            ),
            decimals=6,
        )
        Fiat.objects.get_or_create(code="USD")

    def create_invoice(self, *, out_no: str = "payment-order", **kwargs) -> Invoice:
        defaults = {
            "project": self.project,
            "out_no": out_no,
            "title": "Slot invoice",
            "currency": "USDT",
            "amount": Decimal("10"),
            "methods": {"USDT": [ChainCode.Ethereum, ChainCode.BSC]},
            "expires_at": timezone.now() + timedelta(minutes=10),
        }
        defaults.update(kwargs)
        return Invoice.objects.create(**defaults)

    def create_transfer(
        self, *, chain: Chain, pay_amount: Decimal, pay_address: str
    ) -> Transfer:
        now = timezone.now()
        return Transfer.objects.create(
            chain=chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash=f"0x{chain.chain_id:08x}{int(now.timestamp() * 1000000):056x}",
            crypto=self.crypto,
            from_address="0x00000000000000000000000000000000000000B1",
            to_address=pay_address,
            value=Decimal(pay_amount * Decimal("100000000")),
            amount=pay_amount,
            timestamp=int(now.timestamp()),
            datetime=now,
        )

    def enable_differ_mode(self, *, address_suffix: str = "d01") -> str:
        self.project.evm_invoice_receiving_mode = InvoiceReceivingMode.Differ
        self.project.tron_invoice_receiving_mode = InvoiceReceivingMode.Differ
        self.project.save(
            update_fields=["evm_invoice_receiving_mode", "tron_invoice_receiving_mode"]
        )
        address = Web3.to_checksum_address(f"0x{int(address_suffix, 16):040x}")
        DifferRecipientAddress.objects.create(
            project=self.project,
            chain_type=ChainType.EVM,
            address=address,
        )
        return address

    def test_select_method_replaces_current_payment(self):
        # 账单切换支付方式后，只保留当前支付指引，旧指引不再参与自动匹配。
        invoice = self.create_invoice()

        invoice.select_method(self.crypto, self.chain_a)
        first_pay_address = invoice.pay_address
        first_pay_amount = invoice.pay_amount
        invoice.select_method(self.crypto, self.chain_b)

        invoice.refresh_from_db()
        self.assertEqual(invoice.chain, self.chain_b)
        self.assertEqual(invoice.pay_address, first_pay_address)
        self.assertEqual(invoice.pay_amount, first_pay_amount)

    def test_try_match_invoice_rejects_previous_payment_after_switch(self):
        # 旧支付方式不再作为账单入口；用户切换后打到旧链/旧指引，不自动命中该账单。
        invoice = self.create_invoice(out_no="payment-match")

        invoice.select_method(self.crypto, self.chain_a)
        first_pay_address = invoice.pay_address
        first_pay_amount = invoice.pay_amount
        invoice.select_method(self.crypto, self.chain_b)

        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=first_pay_amount,
            pay_address=first_pay_address,
        )

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertFalse(matched)
        invoice.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertIsNone(invoice.transfer_id)
        self.assertNotEqual(transfer.type, TransferType.Invoice)

    def test_transfer_drop_clears_observed_invoice_binding(self):
        # Invoice 不再维护 CONFIRMING/drop 状态；回滚时删除 Transfer 即可通过
        # on_delete=SET_NULL 清理支付页观察绑定，支付指引继续保持原样。
        invoice = self.create_invoice(out_no="payment-drop")

        invoice.select_method(self.crypto, self.chain_a)
        pay_address = invoice.pay_address
        pay_amount = invoice.pay_amount
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=pay_amount,
            pay_address=pay_address,
        )
        InvoiceService.try_match_invoice(transfer)
        invoice.refresh_from_db()

        transfer.drop()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertIsNone(invoice.transfer_id)
        self.assertEqual(invoice.pay_address, pay_address)
        self.assertEqual(invoice.pay_amount, pay_amount)

    def test_select_method_skips_expired_waiting_payment_combo(self):
        # 回归：旧账单已过 expires_at 但状态仍是 WAITING 时，其 (pay_address, pay_amount)
        # 组合仍被 uniq_invoice_active_payment 约束锁定。支付分配必须把它视为已占用、
        # 跳到下一档金额，而不是当成空闲再次返回（那会触发约束冲突并陷入重试死循环）。
        first = self.create_invoice(out_no="expired-waiting-first")
        first.select_method(self.crypto, self.chain_a)
        first.refresh_from_db()
        first_combo = (first.pay_address, first.pay_amount)

        # 让 first 过期，但保持 WAITING（模拟过期任务尚未翻转的时间窗口）。
        Invoice.objects.filter(pk=first.pk).update(
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        second = self.create_invoice(out_no="expired-waiting-second")
        second.select_method(self.crypto, self.chain_a)
        second.refresh_from_db()
        second_combo = (second.pay_address, second.pay_amount)

        # second 必须拿到不同于 first 的组合；first 的过期 WAITING 组合不被复用。
        self.assertNotEqual(second_combo, first_combo)
        # first 的组合保持不变，未被 second 抢占。
        first.refresh_from_db()
        self.assertEqual((first.pay_address, first.pay_amount), first_combo)

    def test_check_expired_marks_waiting_invoice_expired(self):
        invoice = self.create_invoice(out_no="payment-expire")
        invoice.select_method(self.crypto, self.chain_a)

        # 将账单设为已过期（check_expired 会校验 expires_at <= now）
        Invoice.objects.filter(pk=invoice.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        check_expired(invoice.pk)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.EXPIRED)

    @patch("invoices.service.WebhookService.create_event")
    def test_try_match_invoice_only_binds_transfer_without_webhook(
        self, create_event_mock
    ):
        # 观察到链上付款时只绑定 Transfer，webhook 必须等 Transfer.confirm() 后发送。
        invoice = self.create_invoice(out_no="payment-observed")
        Invoice.objects.filter(pk=invoice.pk).update(
            notify_url="https://merchant.example.com/invoice-observed"
        )
        invoice.refresh_from_db()
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        matched = InvoiceService.try_match_invoice(transfer)
        self.assertTrue(matched)
        create_event_mock.assert_not_called()
        invoice.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertEqual(invoice.transfer_id, transfer.pk)
        self.assertEqual(transfer.type, TransferType.Invoice)

    def test_select_method_allocates_evm_differ_payment(self):
        differ_address = self.enable_differ_mode()
        invoice = self.create_invoice(
            out_no="payment-differ",
            amount=Decimal("10.001"),
        )

        invoice.select_method(self.crypto, self.chain_a)

        invoice.refresh_from_db()
        self.assertEqual(invoice.pay_address, differ_address)
        self.assertEqual(invoice.pay_amount, Decimal("10.01"))
        self.assertFalse(VaultSlot.objects.filter(address=differ_address).exists())

    def test_differ_payment_increments_by_cent_when_combo_is_occupied(self):
        differ_address = self.enable_differ_mode()
        first = self.create_invoice(out_no="payment-differ-first")
        second = self.create_invoice(out_no="payment-differ-second")

        first.select_method(self.crypto, self.chain_a)
        second.select_method(self.crypto, self.chain_a)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.pay_address, differ_address)
        self.assertEqual(second.pay_address, differ_address)
        self.assertEqual(first.pay_amount, Decimal("10.00"))
        self.assertEqual(second.pay_amount, Decimal("10.01"))

    @patch("invoices.service.send_saas_callback")
    @patch("invoices.service.WebhookService.create_event")
    @patch("invoices.service.VaultSlot.schedule_collect_for_invoice")
    def test_confirm_differ_invoice_does_not_schedule_collect(
        self,
        schedule_collect_mock,
        _create_event_mock,
        _send_saas_callback_mock,
    ):
        self.enable_differ_mode()
        invoice = self.create_invoice(out_no="payment-differ-confirm")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        self.assertTrue(InvoiceService.try_match_invoice(transfer))
        Transfer.objects.filter(pk=transfer.pk).update(status=TransferStatus.CONFIRMED)
        invoice.refresh_from_db()

        InvoiceService.confirm_invoice(invoice)

        schedule_collect_mock.assert_not_called()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.COMPLETED)


class InvoiceFinalizeMethodsOrderingTests(TestCase):
    def test_requested_chain_codes_are_sorted_by_chain_sort_order(self):
        project = Project.objects.create(
            name="Method Order Project",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        crypto = Crypto.objects.create(
            name="Tether Ordered",
            symbol="USDTO",
            coingecko_id="tether-ordered",
        )
        eth_chain = Chain(
            code=ChainCode.Ethereum,
            type=ChainType.EVM,
            rpc="http://ethereum.invalid",
            active=True,
            sort_order=20,
        )
        bsc_chain = Chain(
            code=ChainCode.BSC,
            type=ChainType.EVM,
            rpc="http://bsc.invalid",
            active=True,
            sort_order=10,
        )
        Chain.objects.bulk_create([eth_chain, bsc_chain])
        eth_chain.refresh_from_db()
        bsc_chain.refresh_from_db()
        CryptoOnChain.objects.create(
            crypto=crypto,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000000001",
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=crypto,
            chain=bsc_chain,
            address="0x0000000000000000000000000000000000000002",
            decimals=6,
        )
        project.evm_vault = "0x0000000000000000000000000000000000000003"
        project.save(update_fields=["evm_vault"])

        methods = InvoiceService.finalize_methods(
            project=project,
            requested={crypto.symbol: [eth_chain.code, bsc_chain.code]},
        )

        self.assertEqual(methods, {crypto.symbol: [bsc_chain.code, eth_chain.code]})


class InvoicePaymentSelectionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        disable_vault_slot_deploy_schedule(self)
        self.user = User.objects.create(username="merchant-concurrency")
        self.project = Project.objects.create(
            name="ConcurrencyProject",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        self.crypto = Crypto.objects.create(
            name="Tether USD Concurrency",
            symbol="USDTC",
            prices={"USD": "1"},
            coingecko_id="tether-invoice-concurrency",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000C11"
            ),
            decimals=6,
        )
        Fiat.objects.get_or_create(code="USD")
        self.project.evm_vault = "0x00000000000000000000000000000000000000A1"
        self.project.save(update_fields=["evm_vault"])

    def test_select_method_allocates_distinct_payments_under_concurrency(self):
        # 两个并发账单抢同一条链/币种支付组合时，必须各自拿到不同当前支付指引。
        invoice1 = Invoice.objects.create(
            project=self.project,
            out_no="con-1",
            title="Concurrent 1",
            currency="USD",
            amount=Decimal("10"),
            methods={"USDTC": [ChainCode.Ethereum]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        invoice2 = Invoice.objects.create(
            project=self.project,
            out_no="con-2",
            title="Concurrent 2",
            currency="USD",
            amount=Decimal("10"),
            methods={"USDTC": [ChainCode.Ethereum]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        barrier = threading.Barrier(2)
        results: list[tuple[int, str, str]] = []
        errors: list[Exception] = []

        def allocate(invoice_id: int) -> None:
            close_old_connections()
            try:
                invoice = Invoice.objects.get(pk=invoice_id)
                barrier.wait()
                invoice.select_method(self.crypto, self.chain)
                invoice.refresh_from_db()
                results.append(
                    (
                        invoice.pk,
                        invoice.pay_address,
                        str(invoice.pay_amount),
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                # 线程内新开的数据库连接必须显式关闭，否则 TransactionTestCase flush 易死锁。
                connections.close_all()

        threads = [
            threading.Thread(target=allocate, args=(invoice1.pk,)),
            threading.Thread(target=allocate, args=(invoice2.pk,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertFalse(errors)
        self.assertEqual(len(results), 2)
        self.assertEqual(len({(address, amount) for _, address, amount in results}), 2)


class InvoiceDuplicateOutNoTests(TestCase):
    def setUp(self):
        # 屏蔽 SaaS 权限回调，避免单测触发真实 HTTP 请求
        patcher = patch("invoices.viewsets.check_saas_permission")
        self.mock_check_saas = patcher.start()
        self.addCleanup(patcher.stop)

    def test_viewset_create_translates_unique_conflict_to_api_error(self):
        # 并发重复 out_no 命中数据库唯一约束时，接口必须返回业务错误而不是 500。
        project = Project.objects.create(
            name="DuplicateInvoiceProject",
        )
        request = APIRequestFactory().post(
            "/v1/invoice",
            {},
            format="json",
            HTTP_XC_APPID=project.appid,
        )
        serializer = SimpleNamespace(
            is_valid=Mock(return_value=True),
            validated_data={
                "out_no": "dup-order",
                "title": "Duplicate",
                "currency": "USD",
                "amount": Decimal("1"),
                "methods": {"ETH": [ChainCode.Ethereum]},
                "duration": 10,
            },
            errors={},
        )

        with (
            patch.object(InvoiceViewSet, "get_serializer", return_value=serializer),
            patch(
                "invoices.viewsets.Invoice.objects.create",
                side_effect=IntegrityError,
            ),
        ):
            response = InvoiceViewSet.as_view({"post": "create"})(request)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], ErrorCode.DUPLICATE_OUT_NO.code)


class InvoiceAllowedMethodsCapabilityTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_contract_available_methods_exposes_evm_for_vault_project(self):
        project = Project.objects.create(
            name="Invoice Contract Only Project",
            evm_vault="0x0000000000000000000000000000000000008801",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        usdt = Crypto.objects.create(
            name="Tether USD EVM",
            symbol="USDTEVMCO",
            coingecko_id="tether-evm-contract-only",
        )
        eth_chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        CryptoOnChain.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000008802",
            decimals=6,
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(methods[usdt.symbol], [eth_chain.code])

    def test_differ_available_methods_exposes_evm_without_vault(self):
        project = Project.objects.create(
            name="Invoice Differ EVM Project",
            evm_invoice_receiving_mode=InvoiceReceivingMode.Differ,
            tron_invoice_receiving_mode=InvoiceReceivingMode.Differ,
        )
        usdt = Crypto.objects.create(
            name="Tether USD EVM Differ",
            symbol="USDTEVMDIFF",
            coingecko_id="tether-evm-differ",
        )
        eth_chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        CryptoOnChain.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000008812",
            decimals=6,
        )
        DifferRecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000008813",
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(methods[usdt.symbol], [eth_chain.code])

    def test_differ_available_methods_excludes_native_coin(self):
        project = Project.objects.create(
            name="Invoice Differ Native Project",
            evm_invoice_receiving_mode=InvoiceReceivingMode.Differ,
            tron_invoice_receiving_mode=InvoiceReceivingMode.Differ,
        )
        eth_chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        eth = eth_chain.native_coin
        CryptoOnChain.objects.update_or_create(
            crypto=eth,
            chain=eth_chain,
            defaults={
                "address": "",
                "decimals": 18,
                "active": True,
            },
        )
        DifferRecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000008823",
        )

        methods = Invoice.available_methods(project)

        self.assertNotIn(eth.symbol, methods)

    def test_differ_available_methods_allows_tron_native_coin(self):
        # Tron 原生 TRX 在差额模式可用：逐块 TransferContract 扫描能观测 EOA 收原生。
        project = Project.objects.create(
            name="Invoice Differ Tron Native Project",
            evm_invoice_receiving_mode=InvoiceReceivingMode.Differ,
            tron_invoice_receiving_mode=InvoiceReceivingMode.Differ,
        )
        tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="",
            tron_api_key="tron-key",
            active=True,
        )
        trx = tron_chain.native_coin
        DifferRecipientAddress.objects.create(
            project=project,
            chain_type=ChainType.TRON,
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(methods.get(trx.symbol), [tron_chain.code])

    @override_settings(IS_SAAS=True, SAAS_API_TOKEN="xcash-saas-token")
    def test_available_methods_ignores_cached_saas_chain_crypto_whitelist(self):
        project = Project.objects.create(
            name="Invoice SaaS All Methods Project",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        eth_chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        bsc_chain = create_active_evm_test_chain(code=ChainCode.BSC)
        usdt = Crypto.objects.create(
            name="USDT SaaS Allowed",
            symbol="USDTSAASAM",
            coingecko_id="usdt-saas-allowed-methods",
        )
        usdc = Crypto.objects.create(
            name="USDC SaaS Denied",
            symbol="USDCSAASAM",
            coingecko_id="usdc-saas-allowed-methods",
        )
        CryptoOnChain.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000009911",
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=usdt,
            chain=bsc_chain,
            address="0x0000000000000000000000000000000000009912",
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=usdc,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000009913",
            decimals=6,
        )
        project.evm_vault = "0x0000000000000000000000000000000000009914"
        project.save(update_fields=["evm_vault"])
        cache.set(
            f"saas:permission:{project.appid}",
            {
                "frozen": False,
                "enable_deposit": True,
                "allowed_chain_codes": [eth_chain.code],
                "allowed_crypto_symbols": [usdt.symbol],
            },
            None,
        )

        methods = Invoice.available_methods(project)

        self.assertIn(usdt.symbol, methods)
        self.assertIn(usdc.symbol, methods)
        self.assertEqual(set(methods[usdt.symbol]), {eth_chain.code, bsc_chain.code})
        self.assertEqual(methods[usdc.symbol], [eth_chain.code])


class InvoiceContractBillingValidationTests(TestCase):
    """账单最终 methods 生成只暴露当前 VaultSlot 支持的 EVM 链。"""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        self.project = Project.objects.create(
            name="Invoice Mixed Billing Project",
            evm_vault="0x0000000000000000000000000000000000007801",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        self.usdt = Crypto.objects.create(
            name="Tether USD",
            symbol="USDT",
            coingecko_id="usdt-mixed-billing",
        )
        Fiat.objects.get_or_create(code="USD")
        self.eth_chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        self.tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="http://tron.invalid",
            tron_api_key="tron-key",
            active=True,
        )
        CryptoOnChain.objects.create(
            crypto=self.usdt,
            chain=self.eth_chain,
            address="0x0000000000000000000000000000000000007802",
            decimals=6,
        )
        CryptoOnChain.objects.create(
            crypto=self.usdt,
            chain=self.tron_chain,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )

    def build_serializer(self, *, methods):
        request = self.factory.post(
            "/invoices",
            {},
            format="json",
            HTTP_XC_APPID=self.project.appid,
        )
        data = {
            "out_no": "contract-order",
            "title": "contract",
            "currency": self.usdt.symbol,
            "amount": "10",
            "methods": methods,
        }
        return InvoiceCreateSerializer(data=data, context={"request": request})

    def test_default_methods_filters_out_tron(self):
        # 不传 methods：全局 VaultSlot 模式下，Tron 未通过运行时门控时不暴露 Tron。
        serializer = self.build_serializer(methods={})

        self.assertTrue(serializer.is_valid(raise_exception=True))
        self.assertEqual(
            serializer.validated_data["methods"],
            {self.usdt.symbol: [self.eth_chain.code]},
        )

    def test_explicit_tron_rejected(self):
        # 显式要求 Tron，但全局 VaultSlot 模式下 Tron 运行时未就绪，拒绝。
        serializer = self.build_serializer(
            methods={self.usdt.symbol: [self.tron_chain.code]},
        )

        with self.assertRaises(APIError) as ctx:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(ctx.exception.error_code, ErrorCode.NO_RECIPIENT_ADDRESS)

    def test_differ_methods_exposed_without_tron_vault_slot_runtime_gate(self):
        self.project.evm_invoice_receiving_mode = InvoiceReceivingMode.Differ
        self.project.tron_invoice_receiving_mode = InvoiceReceivingMode.Differ
        self.project.save(
            update_fields=["evm_invoice_receiving_mode", "tron_invoice_receiving_mode"]
        )
        DifferRecipientAddress.objects.create(
            project=self.project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000007803",
        )
        DifferRecipientAddress.objects.create(
            project=self.project,
            chain_type=ChainType.TRON,
            address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )

        methods = Invoice.available_methods(self.project)

        self.assertEqual(
            set(methods[self.usdt.symbol]),
            {self.eth_chain.code, self.tron_chain.code},
        )

    def test_select_method_allocates_tron_differ_address(self):
        self.project.evm_invoice_receiving_mode = InvoiceReceivingMode.Differ
        self.project.tron_invoice_receiving_mode = InvoiceReceivingMode.Differ
        self.project.save(
            update_fields=["evm_invoice_receiving_mode", "tron_invoice_receiving_mode"]
        )
        differ_address = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        DifferRecipientAddress.objects.create(
            project=self.project,
            chain_type=ChainType.TRON,
            address=differ_address,
        )
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="tron-differ-select-method",
            title="tron",
            currency=self.usdt.symbol,
            amount=Decimal("10"),
            methods={self.usdt.symbol: [self.tron_chain.code]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        selected = invoice.select_method(self.usdt, self.tron_chain)

        self.assertTrue(selected)
        invoice.refresh_from_db()
        self.assertEqual(invoice.chain, self.tron_chain)
        self.assertEqual(invoice.pay_address, differ_address)
        self.assertEqual(invoice.pay_amount, Decimal("10.00"))
        self.assertFalse(
            VaultSlot.objects.filter(
                chain=self.tron_chain,
                project=self.project,
                address=invoice.pay_address,
            ).exists()
        )

    @override_settings(
        TRON_VAULT_SLOT_NILE_VERIFIED=True,
        TRON_VAULT_SLOT_FACTORY_ADDRESS="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        TRON_VAULT_SLOT_TEMPLATE_ADDRESS="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
        TRON_VAULT_SLOT_FEE_LIMIT=150_000_000,
        TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT=300_000_000,
    )
    def test_tron_methods_exposed_only_after_runtime_gate(self):
        self.project.evm_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot
        self.project.tron_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot
        self.project.tron_vault = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        self.project.save(
            update_fields=[
                "evm_invoice_receiving_mode",
                "tron_invoice_receiving_mode",
                "tron_vault",
            ]
        )

        methods = Invoice.available_methods(self.project)

        self.assertEqual(
            set(methods[self.usdt.symbol]),
            {self.eth_chain.code, self.tron_chain.code},
        )

    @override_settings(
        TRON_VAULT_SLOT_NILE_VERIFIED=True,
        TRON_VAULT_SLOT_FACTORY_ADDRESS="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        TRON_VAULT_SLOT_TEMPLATE_ADDRESS="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
        TRON_VAULT_SLOT_FEE_LIMIT=150_000_000,
        TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT=300_000_000,
    )
    def test_select_method_allocates_tron_vault_slot(self):
        from chains.models import VaultSlot

        self.project.evm_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot
        self.project.tron_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot
        self.project.tron_vault = "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8"
        self.project.save(
            update_fields=[
                "evm_invoice_receiving_mode",
                "tron_invoice_receiving_mode",
                "tron_vault",
            ]
        )
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="tron-select-method",
            title="tron",
            currency=self.usdt.symbol,
            amount=Decimal("10"),
            methods={self.usdt.symbol: [self.tron_chain.code]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        selected = invoice.select_method(self.usdt, self.tron_chain)

        self.assertTrue(selected)
        invoice.refresh_from_db()
        self.assertEqual(invoice.chain, self.tron_chain)
        self.assertTrue(invoice.pay_address.startswith("T"))
        self.assertTrue(
            VaultSlot.objects.filter(
                chain=self.tron_chain,
                project=self.project,
                address=invoice.pay_address,
            ).exists()
        )


class InvoiceConfirmStatusTests(TestCase):
    """confirm_invoice 的状态前置校验测试。"""

    def setUp(self):
        self.user = User.objects.create(username="merchant-status")
        self.project = Project.objects.create(
            name="StatusProject",
        )
        self.crypto = Crypto.objects.create(
            name="Status USDT",
            symbol="STATUS-USDT",
            prices={"USD": "1"},
            coingecko_id="status-usdt",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.Ethereum)

    def _make_invoice(self, status):
        return Invoice.objects.create(
            project=self.project,
            out_no=f"status-{status}",
            title="Status test",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            status=status,
            expires_at=timezone.now() + timedelta(minutes=10),
        )

    def test_confirm_invoice_rejects_unbound_payable_status(self):
        # Invoice 只有绑定已确认 Transfer 后才能完成；WAITING/EXPIRED 本身不是可确认事实。
        for bad_status in [
            InvoiceStatus.WAITING,
            InvoiceStatus.EXPIRED,
        ]:
            invoice = self._make_invoice(bad_status)
            with self.assertRaises(InvoiceStatusError):
                InvoiceService.confirm_invoice(invoice)

    @patch("invoices.service.send_saas_callback")
    @patch("invoices.service.WebhookService.create_event")
    def test_confirm_native_invoice_uses_invoice_notify_url(
        self, create_event_mock, _callback_mock
    ):
        # 原生 Invoice 若配置了账单级 notify_url，最终通知应投递到该地址；
        # 为空时 WebhookEvent.delivery_url 维持默认空串，由投递层 fallback 到 Project.webhook。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="status-native-notify",
            title="Status native notify",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            status=InvoiceStatus.WAITING,
            protocol=InvoiceProtocol.NATIVE,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000A01"
            ),
            pay_amount=Decimal("10"),
            notify_url="https://merchant.example.com/invoice-notify",
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "a1" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000A02"
            ),
            to_address=invoice.pay_address,
            value=Decimal("1000000000"),
            amount=invoice.pay_amount,
            status=TransferStatus.CONFIRMED,
            timestamp=int(timezone.now().timestamp()),
            datetime=timezone.now(),
        )
        Invoice.objects.filter(pk=invoice.pk).update(transfer=transfer)
        invoice.refresh_from_db()

        InvoiceService.confirm_invoice(invoice)

        self.assertEqual(
            create_event_mock.call_args.kwargs["delivery_url"],
            "https://merchant.example.com/invoice-notify",
        )


class InvoiceWebhookPayloadTests(TestCase):
    """build_webhook_payload 边界测试：crypto/pay_amount 为 None 时不应崩溃。"""

    def setUp(self):
        self.user = User.objects.create(username="merchant-content")
        self.project = Project.objects.create(
            name="ContentProject",
        )

    def test_payload_with_crypto_none(self):
        # 未选支付方式的账单，payload 应安全返回 None 字段而非抛异常。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="content-none",
            title="Content test",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        payload = InvoiceService.build_webhook_payload(invoice)
        self.assertEqual(payload["type"], "invoice")
        self.assertIsNone(payload["data"]["crypto"])
        self.assertIsNone(payload["data"]["pay_amount"])
        self.assertIsNone(payload["data"]["chain"])
        self.assertIsNone(payload["data"]["hash"])
        self.assertIsNone(payload["data"]["block"])
        self.assertFalse(payload["data"]["confirmed"])
        self.assertNotIn("tx", payload)


class InvoiceExpiredMatchTests(TestCase):
    """过期 Invoice 的当前支付指引仍可按链上发生时间命中。"""

    def setUp(self):
        disable_vault_slot_deploy_schedule(self)
        self.user = User.objects.create(username="merchant-expired-match")
        self.project = Project.objects.create(
            name="ExpiredMatchProject",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        self.crypto = Crypto.objects.create(
            name="Tether Expired",
            symbol="USDTE",
            prices={"USD": "1"},
            coingecko_id="tether-expired-match",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        Fiat.objects.get_or_create(code="USD")
        self.recipient_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000E1"
        )
        self.project.evm_vault = self.recipient_address
        self.project.save(update_fields=["evm_vault"])
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000e2"
            ),
            decimals=6,
        )

    def test_expired_invoice_can_still_match_current_payment_by_transfer_time(self):
        # scanner 可能晚于过期任务看到链上交易；只要交易发生在账单窗口内，
        # 当前支付指引仍应命中，避免误拒绝已按时付款的用户。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="expired-match-order",
            title="Expired match",
            currency="USDTE",
            amount=Decimal("10"),
            methods={"USDTE": [ChainCode.Ethereum]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        invoice.select_method(self.crypto, self.chain)
        pay_address = invoice.pay_address
        pay_amount = invoice.pay_amount

        expired_at = timezone.now()
        Invoice.objects.filter(pk=invoice.pk).update(
            status=InvoiceStatus.EXPIRED,
            updated_at=expired_at,
        )

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.EXPIRED)

        # 链上付款在过期前发生（datetime 在 started_at 和 expires_at 之间）
        transfer_time = invoice.started_at + timedelta(seconds=30)
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "e1" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000F1"
            ),
            to_address=pay_address,
            value=Decimal(pay_amount * Decimal("100000000")),
            amount=pay_amount,
            timestamp=int(transfer_time.timestamp()),
            datetime=transfer_time,
        )

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertTrue(matched)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.EXPIRED)
        self.assertEqual(invoice.transfer_id, transfer.pk)


class FallbackInvoiceExpiredTests(TestCase):
    """fallback_invoice_expired 批量过期的逻辑测试。"""

    def setUp(self):
        disable_vault_slot_deploy_schedule(self)
        self.user = User.objects.create(username="merchant-fallback")
        self.project = Project.objects.create(
            name="FallbackProject",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        self.crypto = Crypto.objects.create(
            name="Tether Fallback",
            symbol="USDTF",
            prices={"USD": "1"},
            coingecko_id="tether-fallback",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        Fiat.objects.get_or_create(code="USD")
        self.project.evm_vault = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000F1"
        )
        self.project.save(update_fields=["evm_vault"])
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f2"
            ),
            decimals=6,
        )

    def test_fallback_expires_waiting_invoices(self):
        # fallback 任务应批量将过期的 WAITING 账单标记为 EXPIRED。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="fallback-order",
            title="Fallback test",
            currency="USDTF",
            amount=Decimal("10"),
            methods={"USDTF": [ChainCode.Ethereum]},
            # 设置过去的过期时间
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        invoice.select_method(self.crypto, self.chain)

        fallback_invoice_expired()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.EXPIRED)

    def test_fallback_skips_waiting_invoice_with_observed_transfer(self):
        # 已观察到链上付款的 WAITING 账单正在等待 Transfer 确认，不应被 fallback 误过期。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="fallback-observed",
            title="Fallback observed",
            currency="USDTF",
            amount=Decimal("10"),
            methods={"USDTF": [ChainCode.Ethereum]},
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        invoice.select_method(self.crypto, self.chain)
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=1,
            block_hash="0x" + "bb" * 32,
            hash="0x" + "f1" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000F3"
            ),
            to_address=invoice.pay_address,
            value=Decimal(invoice.pay_amount * Decimal("100000000")),
            amount=invoice.pay_amount,
            timestamp=int(timezone.now().timestamp()),
            datetime=timezone.now(),
        )
        Invoice.objects.filter(pk=invoice.pk).update(transfer=transfer)

        fallback_invoice_expired()

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertEqual(invoice.transfer_id, transfer.pk)


class CheckExpiredAtomicityTests(TransactionTestCase):
    """验证 check_expired 在并发场景下的原子性。"""

    def setUp(self):
        disable_vault_slot_deploy_schedule(self)
        self.user = User.objects.create(username="merchant-atomic")
        self.project = Project.objects.create(
            name="AtomicProject",
            evm_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
            tron_invoice_receiving_mode=InvoiceReceivingMode.VaultSlot,
        )
        self.crypto = Crypto.objects.create(
            name="Tether Atomic",
            symbol="USDTA",
            prices={"USD": "1"},
            coingecko_id="tether-atomic",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.Ethereum)
        Fiat.objects.get_or_create(code="USD")
        self.project.evm_vault = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000A7"
        )
        self.project.save(update_fields=["evm_vault"])
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000a8"
            ),
            decimals=6,
        )

    def test_check_expired_skips_already_matched_invoice(self):
        # 并发场景：check_expired 执行时如果账单已绑定 Transfer，
        # select_for_update + transfer 条件应使其安全跳过，不会误过期。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="atomic-order",
            title="Atomic test",
            currency="USDTA",
            amount=Decimal("10"),
            methods={"USDTA": [ChainCode.Ethereum]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        invoice.select_method(self.crypto, self.chain)

        # 模拟在 check_expired 执行前，账单已被匹配
        now = timezone.now()
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "a7" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000B7"
            ),
            to_address=invoice.pay_address,
            value=Decimal(invoice.pay_amount * Decimal("100000000")),
            amount=invoice.pay_amount,
            timestamp=int(now.timestamp()),
            datetime=now,
        )
        InvoiceService.try_match_invoice(transfer)
        Invoice.objects.filter(pk=invoice.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )

        # check_expired 应该安全跳过已观察到付款的账单
        check_expired(invoice.pk)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertEqual(invoice.transfer_id, transfer.pk)


class InvoiceCreatePermissionCheckTests(TestCase):
    """v2 SaaS 模式：账单收款入口调用 check_saas_permission。"""

    def setUp(self):
        self.project = Project.objects.create(
            name="InvoicePermCheckProject",
        )

    def _make_request(self):
        return APIRequestFactory().post(
            "/v1/invoice",
            {},
            format="json",
            HTTP_XC_APPID=self.project.appid,
        )

    def _make_serializer_stub(self):
        return SimpleNamespace(
            is_valid=Mock(return_value=True),
            validated_data={
                "out_no": "perm-inv-order",
                "title": "PermCheck Invoice",
                "currency": "USD",
                "amount": Decimal("10"),
                "methods": {},
                "duration": 10,
                "return_url": "",
            },
            errors={},
        )

    @patch("invoices.viewsets.check_saas_permission")
    def test_create_calls_permission_check_with_correct_args(self, mock_check):
        """账单创建时只校验 invoice 账号状态，不占用 deposit 功能锁。"""
        serializer_stub = self._make_serializer_stub()

        with (
            patch.object(
                InvoiceViewSet, "get_serializer", return_value=serializer_stub
            ),
            patch(
                "invoices.viewsets.Invoice.objects.create",
                return_value=Mock(
                    sys_no="inv-0001",
                    out_no="perm-inv-order",
                    project=self.project,
                    status="waiting",
                ),
            ),
            patch("invoices.viewsets.InvoiceService.initialize_invoice"),
            patch(
                "invoices.viewsets.InvoiceDisplaySerializer",
                return_value=Mock(data={}),
            ),
        ):
            InvoiceViewSet.as_view({"post": "create"})(self._make_request())

        mock_check.assert_called_once_with(
            appid=self.project.appid,
            action="invoice",
        )

    @patch("invoices.viewsets.check_saas_permission")
    def test_create_relies_on_finalized_methods_without_per_method_recheck(
        self,
        mock_check,
    ):
        """创建阶段 methods 已由 available_methods 收敛，不再逐项重复复检。"""
        serializer_stub = self._make_serializer_stub()
        serializer_stub.validated_data["methods"] = {
            "USDT": ["ethereum-mainnet", "bsc-mainnet"],
            "USDC": ["ethereum-mainnet"],
        }

        with (
            patch.object(
                InvoiceViewSet, "get_serializer", return_value=serializer_stub
            ),
            patch(
                "invoices.viewsets.Invoice.objects.create",
                return_value=Mock(
                    sys_no="inv-0002",
                    out_no="perm-inv-order",
                    project=self.project,
                    status="waiting",
                ),
            ),
            patch("invoices.viewsets.InvoiceService.initialize_invoice"),
            patch(
                "invoices.viewsets.InvoiceDisplaySerializer",
                return_value=Mock(data={}),
            ),
        ):
            InvoiceViewSet.as_view({"post": "create"})(self._make_request())

        mock_check.assert_called_once_with(
            appid=self.project.appid,
            action="invoice",
        )

    @patch("invoices.viewsets.check_saas_permission")
    def test_select_method_checks_invoice_account_status(self, mock_check):
        """支付页选择支付方式时只复检 invoice 账号状态。"""
        invoice = Mock(
            status=InvoiceStatus.WAITING,
            transfer_id=None,
            expires_at=timezone.now() + timedelta(minutes=10),
            methods={"USDT": ["ethereum-mainnet"]},
            project=Mock(appid=self.project.appid),
        )
        serializer_stub = SimpleNamespace(
            is_valid=Mock(return_value=True),
            validated_data={"crypto": "USDT", "chain": "ethereum-mainnet"},
            errors={},
        )
        crypto = Mock(symbol="USDT")
        chain = Mock(code="ethereum-mainnet")

        with (
            patch.object(
                InvoiceViewSet, "get_serializer", return_value=serializer_stub
            ),
            patch.object(InvoiceViewSet, "get_object", return_value=invoice),
            patch("invoices.viewsets.CryptoService.get_by_symbol", return_value=crypto),
            patch("invoices.viewsets.ChainService.get_by_code", return_value=chain),
            patch(
                "invoices.viewsets.InvoiceDisplaySerializer",
                return_value=Mock(data={}),
            ),
        ):
            InvoiceViewSet.as_view({"post": "select_method"})(
                APIRequestFactory().post(
                    "/v1/invoice/inv-0002/select-method",
                    {"crypto": "USDT", "chain": "ethereum-mainnet"},
                    format="json",
                    HTTP_XC_APPID=self.project.appid,
                ),
                sys_no="inv-0002",
            )

        mock_check.assert_called_once_with(
            appid=self.project.appid,
            action="invoice",
        )

    @patch("invoices.viewsets.check_saas_permission")
    def test_select_method_rejects_observed_invoice(self, mock_check):
        """Transfer 已绑定后账单仍是 WAITING,但不再允许重选支付方式。"""
        invoice = Mock(
            status=InvoiceStatus.WAITING,
            transfer_id=123,
            expires_at=timezone.now() + timedelta(minutes=10),
            methods={"USDT": ["ethereum-mainnet"]},
            project=Mock(appid=self.project.appid),
        )
        serializer_stub = SimpleNamespace(
            is_valid=Mock(return_value=True),
            validated_data={"crypto": "USDT", "chain": "ethereum-mainnet"},
            errors={},
        )

        with (
            patch.object(
                InvoiceViewSet, "get_serializer", return_value=serializer_stub
            ),
            patch.object(InvoiceViewSet, "get_object", return_value=invoice),
        ):
            response = InvoiceViewSet.as_view({"post": "select_method"})(
                APIRequestFactory().post(
                    "/v1/invoice/inv-0002/select-method",
                    {"crypto": "USDT", "chain": "ethereum-mainnet"},
                    format="json",
                    HTTP_XC_APPID=self.project.appid,
                ),
                sys_no="inv-0002",
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], ErrorCode.INVALID_INVOICE_STATUS.code)
        mock_check.assert_not_called()

    @patch("invoices.viewsets.check_saas_permission")
    def test_create_does_not_use_deposit_feature_gate(self, mock_check):
        """创建 Invoice 时不应触发 deposit 功能锁，否则低套餐会被错误拒绝。"""

        def reject_deposit_action(*, action, **kwargs):
            if action == "deposit":
                raise APIError(ErrorCode.FEATURE_NOT_ENABLED, detail="deposit")

        mock_check.side_effect = reject_deposit_action

        serializer_stub = self._make_serializer_stub()

        with (
            patch.object(
                InvoiceViewSet, "get_serializer", return_value=serializer_stub
            ),
            patch(
                "invoices.viewsets.Invoice.objects.create",
                return_value=Mock(
                    sys_no="inv-0003",
                    out_no="perm-inv-order",
                    project=self.project,
                    status="waiting",
                ),
            ),
            patch("invoices.viewsets.InvoiceService.initialize_invoice"),
            patch(
                "invoices.viewsets.InvoiceDisplaySerializer",
                return_value=Mock(data={}),
            ),
        ):
            response = InvoiceViewSet.as_view({"post": "create"})(self._make_request())

        self.assertEqual(response.status_code, 201)
        self.assertNotIn(
            "deposit",
            [call.kwargs.get("action") for call in mock_check.call_args_list],
        )

    @patch("invoices.viewsets.check_saas_permission")
    def test_create_blocked_when_account_frozen(self, mock_check):
        """账户冻结时，充值账单创建应返回 403。"""

        mock_check.side_effect = APIError(ErrorCode.ACCOUNT_FROZEN)

        response = InvoiceViewSet.as_view({"post": "create"})(self._make_request())

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], ErrorCode.ACCOUNT_FROZEN.code)


class InvoiceSelectForUpdateLockScopeTests(InvoicePaymentSelectionTests):
    """select_for_update(of=("self",)) 回归测试。

    StressRun 高并发压测时，三处 `select_for_update().select_related("project")`
    会让 PostgreSQL 把 join 中的 projects_project / currencies_crypto 父行也锁成
    FOR UPDATE，与并发 INSERT/UPDATE 子表自动加的 FK FOR KEY SHARE 互斥，引发
    `OperationalError: deadlock detected`。修复后必须显式 `of=("self",)`，仅锁
    主表本行。这里通过捕获实际 SQL，断言锁子句不再触及任何父表。
    """

    def _for_update_tails(self, captured):
        # 每条 FOR UPDATE 语句的锁子句尾部，用来检查 `OF ...` 范围。
        tails = []
        for query in captured.captured_queries:
            sql = query["sql"]
            if "FOR UPDATE" not in sql:
                continue
            tails.append(sql[sql.rindex("FOR UPDATE") :])
        return tails

    def _assert_lock_scope_is_self_only(self, tails):
        self.assertTrue(
            tails,
            "应至少触发一次 SELECT ... FOR UPDATE 行锁",
        )
        for tail in tails:
            # 不带 OF 子句 = 锁所有 JOIN 表的行，正是死锁根因。
            self.assertIn(
                " OF ",
                tail,
                f"select_for_update 必须带 of=(...) 限定主表: {tail}",
            )
            for parent_table in (
                '"projects_project"',
                '"currencies_crypto"',
                '"chains_chain"',
            ):
                self.assertNotIn(
                    parent_table,
                    tail,
                    f"父表 {parent_table} 不应出现在 FOR UPDATE 子句中: {tail}",
                )

    def test_try_match_invoice_locks_only_self_rows(self):
        invoice = self.create_invoice(out_no="lock-scope-match")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )

        with CaptureQueriesContext(connection) as captured:
            InvoiceService.try_match_invoice(transfer)

        self._assert_lock_scope_is_self_only(self._for_update_tails(captured))

    def test_confirm_invoice_locks_only_self_rows(self):
        invoice = self.create_invoice(out_no="lock-scope-confirm")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        InvoiceService.try_match_invoice(transfer)
        Transfer.objects.filter(pk=transfer.pk).update(status=TransferStatus.CONFIRMED)
        invoice.refresh_from_db()

        with CaptureQueriesContext(connection) as captured:
            InvoiceService.confirm_invoice(invoice)

        self._assert_lock_scope_is_self_only(self._for_update_tails(captured))


class InvoiceVaultSlotPaymentTest(TestCase, InvoiceTestMixin):
    def setUp(self):
        self.setup_base_fixtures()

    def _make_minimal_invoice(self):
        return self.create_test_invoice(out_no="billing-mode-test")

    def _set_invoice_payment(
        self,
        invoice: Invoice,
        *,
        crypto: Crypto,
        chain: Chain,
        pay_address: str,
        pay_amount: Decimal,
    ) -> None:
        Invoice.objects.filter(pk=invoice.pk).update(
            crypto=crypto,
            chain=chain,
            pay_address=pay_address,
            pay_amount=pay_amount,
        )
        invoice.refresh_from_db()

    def test_contract_slot_uses_project_vault(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F01"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        invoice = self.create_test_invoice(
            out_no="contract-vault-source",
        )

        pay_address, pay_amount = invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )
        slot = VaultSlot.objects.get(
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            chain=self.chain,
        )

        self.assertEqual(pay_address, slot.address)
        self.assertEqual(pay_amount, Decimal("10"))

    def test_contract_slot_creates_invoice_vault_slot_with_index_without_customer(
        self,
    ):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F02"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        invoice = self.create_test_invoice(
            out_no="contract-slot-row",
        )

        with self.captureOnCommitCallbacks(execute=False):
            pay_address, pay_amount = invoice._allocate_contract_slot(
                self.crypto,
                self.chain,
                Decimal("10"),
            )

        slot = VaultSlot.objects.get(
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            chain=self.chain,
        )
        self.assertIsNone(slot.customer_id)
        self.assertEqual(slot.address, pay_address)
        self.assertEqual(pay_amount, Decimal("10"))

    def test_contract_slot_selection_returns_invoice_vault_slot(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F12"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        invoice = self.create_test_invoice(
            out_no="contract-slot-object",
        )

        with self.captureOnCommitCallbacks(execute=False):
            slot = invoice.get_vault_slot(
                crypto=self.crypto,
                chain=self.chain,
                crypto_amount=Decimal("10"),
            )

        self.assertEqual(slot.project, self.project)
        self.assertEqual(slot.chain, self.chain)
        self.assertEqual(slot.usage, VaultSlotUsage.INVOICE)

    def test_reusing_undeployed_contract_slot_does_not_schedule_deploy_for_token(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F16"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        slot = VaultSlot.objects.create(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000A16"
            ),
            salt=b"\x16" * 32,
        )
        invoice = self.create_test_invoice(out_no="contract-token-lazy-deploy")

        with (
            patch.object(VaultSlot, "schedule_deploy") as schedule_deploy,
            self.captureOnCommitCallbacks(execute=True),
        ):
            selected = invoice.get_vault_slot(
                crypto=self.crypto,
                chain=self.chain,
                crypto_amount=Decimal("10"),
            )

        self.assertEqual(selected.pk, slot.pk)
        schedule_deploy.assert_not_called()

    def test_reusing_undeployed_contract_slot_schedules_deploy_for_native(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F17"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        slot = VaultSlot.objects.create(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000A17"
            ),
            salt=b"\x17" * 32,
        )
        invoice = self.create_test_invoice(out_no="contract-native-predeploy")

        with (
            patch.object(VaultSlot, "schedule_deploy") as schedule_deploy,
            self.captureOnCommitCallbacks(execute=True),
        ):
            selected = invoice.get_vault_slot(
                crypto=self.chain.native_coin,
                chain=self.chain,
                crypto_amount=Decimal("10"),
            )

        self.assertEqual(selected.pk, slot.pk)
        schedule_deploy.assert_called_once_with(slot.pk)
        self.assertEqual(slot.invoice_index, 0)
        self.assertIsNone(slot.customer_id)
        self.assertEqual(slot.project.evm_vault, vault_address)

    def test_contract_slot_reuses_slot_when_existing_invoice_expired(self):
        # 旧账单已被过期任务翻成 EXPIRED 后，其 (pay_address, pay_amount) 组合脱离
        # uniq_invoice_active_payment 约束（约束只覆盖 status=WAITING），新合约账单
        # 可以安全复用同一 VaultSlot 地址。
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F03"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-reuse-first",
            status=InvoiceStatus.EXPIRED,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        first_pay_address, first_pay_amount = first_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )
        self._set_invoice_payment(
            first_invoice,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=first_pay_address,
            pay_amount=first_pay_amount,
        )
        second_invoice = self.create_test_invoice(
            out_no="contract-reuse-second",
        )

        second_pay_address, _ = second_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )

        self.assertEqual(second_pay_address, first_pay_address)
        self.assertEqual(
            VaultSlot.objects.filter(
                project=self.project,
                usage=VaultSlotUsage.INVOICE,
                chain=self.chain,
            ).count(),
            1,
        )

    def test_contract_slot_not_reused_when_existing_invoice_waiting_but_expired(self):
        # 回归：旧账单已过 expires_at 但状态仍是 WAITING（过期任务尚未翻转）时，
        # uniq_invoice_active_payment 约束仍锁着其 (pay_address, pay_amount) 组合。
        # 占用判定必须只看 status=WAITING、不看 expires_at，否则复用同一槽位会在后续
        # _set_current_payment 命中约束、陷入 IntegrityError 重试死循环。
        # 正确行为：改用下一个 invoice_index 的新 VaultSlot。
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F05"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-waiting-expired-first",
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        first_pay_address, first_pay_amount = first_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )
        self._set_invoice_payment(
            first_invoice,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=first_pay_address,
            pay_amount=first_pay_amount,
        )
        # first_invoice 仍是默认 WAITING，只是 expires_at 已过——典型的"过期未翻转"窗口。
        self.assertEqual(first_invoice.status, InvoiceStatus.WAITING)

        second_invoice = self.create_test_invoice(
            out_no="contract-waiting-expired-second",
        )
        second_pay_address, _ = second_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )

        self.assertNotEqual(second_pay_address, first_pay_address)
        self.assertEqual(
            VaultSlot.objects.filter(
                project=self.project,
                usage=VaultSlotUsage.INVOICE,
                chain=self.chain,
            ).count(),
            2,
        )

    def test_contract_slot_reuses_existing_slot_when_amount_differs(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F13"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-reuse-amount-first",
        )
        first_pay_address, first_pay_amount = first_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )
        self._set_invoice_payment(
            first_invoice,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=first_pay_address,
            pay_amount=first_pay_amount,
        )
        second_invoice = self.create_test_invoice(
            out_no="contract-reuse-amount-second",
        )

        second_pay_address, _ = second_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10.00000001"),
        )

        self.assertEqual(second_pay_address, first_pay_address)
        self.assertEqual(
            VaultSlot.objects.filter(
                project=self.project,
                usage=VaultSlotUsage.INVOICE,
                chain=self.chain,
            ).count(),
            1,
        )

    def test_contract_slot_reuses_existing_slot_when_crypto_differs(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F14"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        other_crypto = Crypto.objects.create(
            name="USD Coin Contract",
            symbol="USDCC",
            prices={"USD": "1"},
            coingecko_id="usdc-contract-slot",
        )
        first_invoice = self.create_test_invoice(
            out_no="contract-reuse-crypto-first",
        )
        first_pay_address, first_pay_amount = first_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )
        self._set_invoice_payment(
            first_invoice,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=first_pay_address,
            pay_amount=first_pay_amount,
        )
        second_invoice = self.create_test_invoice(
            out_no="contract-reuse-crypto-second",
        )

        second_pay_address, _ = second_invoice._allocate_contract_slot(
            other_crypto,
            self.chain,
            Decimal("10"),
        )

        self.assertEqual(second_pay_address, first_pay_address)
        self.assertEqual(
            VaultSlot.objects.filter(
                project=self.project,
                usage=VaultSlotUsage.INVOICE,
                chain=self.chain,
            ).count(),
            1,
        )

    def test_contract_slot_creates_next_index_when_existing_payment_overlaps(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F04"
        )
        self.project.evm_vault = vault_address
        self.project.save(update_fields=["evm_vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-overlap-first",
        )
        first_pay_address, first_pay_amount = first_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )
        self._set_invoice_payment(
            first_invoice,
            crypto=self.crypto,
            chain=self.chain,
            pay_address=first_pay_address,
            pay_amount=first_pay_amount,
        )
        second_invoice = self.create_test_invoice(
            out_no="contract-overlap-second",
        )

        second_pay_address, _ = second_invoice._allocate_contract_slot(
            self.crypto,
            self.chain,
            Decimal("10"),
        )

        self.assertNotEqual(second_pay_address, first_pay_address)
        second_slot = VaultSlot.objects.get(
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            chain=self.chain,
            invoice_index=1,
        )
        self.assertEqual(second_pay_address, second_slot.address)
        self.assertEqual(
            VaultSlot.objects.filter(
                project=self.project,
                usage=VaultSlotUsage.INVOICE,
                chain=self.chain,
            ).count(),
            2,
        )

    def test_select_method_contract_retries_and_reselects_slot_after_integrity_error(
        self,
    ):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F15"
        )
        self.project.evm_vault = vault_address
        self.project.evm_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot
        self.project.tron_invoice_receiving_mode = InvoiceReceivingMode.VaultSlot
        self.project.save(
            update_fields=[
                "evm_vault",
                "evm_invoice_receiving_mode",
                "tron_invoice_receiving_mode",
            ]
        )
        invoice = self.create_test_invoice(
            out_no="contract-retry-reselect",
        )
        with self.captureOnCommitCallbacks(execute=False):
            VaultSlot.ensure_invoice_address(
                project=self.project,
                chain=self.chain,
                invoice_index=0,
                crypto=self.crypto,
            )
            VaultSlot.ensure_invoice_address(
                project=self.project,
                chain=self.chain,
                invoice_index=1,
                crypto=self.crypto,
            )
        slot0 = VaultSlot.objects.get(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
        )
        slot1 = VaultSlot.objects.get(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=1,
        )
        original_set_current_payment = invoice._set_current_payment

        def set_current_payment_with_first_conflict(*args, **kwargs):
            if set_current_payment_with_first_conflict.calls == 0:
                set_current_payment_with_first_conflict.calls += 1
                raise IntegrityError("simulated active payment conflict")
            set_current_payment_with_first_conflict.calls += 1
            return original_set_current_payment(*args, **kwargs)

        set_current_payment_with_first_conflict.calls = 0

        with (
            patch.object(
                invoice,
                "get_vault_slot",
                side_effect=[slot0, slot1],
            ) as slot_selector,
            patch.object(
                invoice,
                "_set_current_payment",
                side_effect=set_current_payment_with_first_conflict,
            ) as update_mock,
        ):
            invoice.select_method(self.crypto, self.chain)

        invoice.refresh_from_db()
        self.assertEqual(slot_selector.call_count, 2)
        self.assertEqual(update_mock.call_count, 2)
        self.assertEqual(invoice.pay_address, slot1.address)

    def test_contract_slot_rejects_project_without_vault(self):
        invoice = self.create_test_invoice(
            out_no="contract-vault-missing",
        )

        with self.assertRaises(Invoice.InvoiceAllocationError):
            invoice._allocate_contract_slot(self.crypto, self.chain, Decimal("10"))


class TryMatchContractInvoiceTest(TestCase, InvoiceTestMixin):
    def setUp(self):
        self.setup_base_fixtures(
            username="contract-match-merchant",
            project_name="ContractMatchProject",
            crypto_symbol="USDTMAT",
            chain_name=ChainCode.Polygon,
        )
        self.invoice = self.create_test_invoice(
            out_no="contract-match-order",
            amount=Decimal("100"),
        )
        self.slot_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000ce"
        )
        Invoice.objects.filter(pk=self.invoice.pk).update(
            crypto=self.crypto,
            chain=self.chain,
            pay_address=self.slot_address,
            pay_amount=Decimal("100"),
        )
        self.invoice.refresh_from_db()
        VaultSlot.objects.create(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=self.slot_address,
            salt=b"\x11" * 32,
        )

    def _make_transfer(self, amount: Decimal) -> Transfer:
        now = timezone.now()
        return Transfer.objects.create(
            chain=self.chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash=f"0x{self.chain.chain_id:08x}{int(now.timestamp() * 1000000):056x}",
            crypto=self.crypto,
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000b2"
            ),
            to_address=self.slot_address,
            value=Decimal(amount * Decimal("100000000")),
            amount=amount,
            timestamp=int(now.timestamp()),
            datetime=now,
        )

    def test_matches_when_transfer_amount_equals_pay_amount(self):
        transfer = self._make_transfer(Decimal("100"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertTrue(matched)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.WAITING)
        self.assertEqual(self.invoice.transfer_id, transfer.pk)

    def test_does_not_match_when_transfer_amount_greater_than_pay_amount(self):
        transfer = self._make_transfer(Decimal("150"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertFalse(matched)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.WAITING)

    def test_contract_match_uses_exact_amount_when_slot_is_shared(self):
        newer_invoice = self.create_test_invoice(
            out_no="contract-match-newer",
            amount=Decimal("150"),
        )
        Invoice.objects.filter(pk=newer_invoice.pk).update(
            crypto=self.crypto,
            chain=self.chain,
            pay_address=self.slot_address,
            pay_amount=Decimal("150"),
        )

        transfer = self._make_transfer(Decimal("100"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertTrue(matched)
        self.invoice.refresh_from_db()
        newer_invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.WAITING)
        self.assertEqual(self.invoice.transfer_id, transfer.pk)
        self.assertEqual(newer_invoice.status, InvoiceStatus.WAITING)

    @patch("chains.models.VaultSlot.schedule_collect_for_invoice")
    @patch("invoices.service.send_saas_callback")
    @patch("invoices.service.WebhookService.create_event")
    def test_confirm_contract_invoice_schedules_erc20_slot_collection(
        self,
        _create_event_mock,
        _send_saas_callback_mock,
        schedule_collect_mock,
    ):
        transfer = self._make_transfer(Decimal("100"))
        InvoiceService.try_match_invoice(transfer)
        Transfer.objects.filter(pk=transfer.pk).update(status=TransferStatus.CONFIRMED)
        self.invoice.refresh_from_db()

        InvoiceService.confirm_invoice(self.invoice)

        schedule_collect_mock.assert_called_once_with(self.invoice.pk)

    def test_does_not_match_when_transfer_amount_less_than_pay_amount(self):
        transfer = self._make_transfer(Decimal("99.99"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertFalse(matched)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.WAITING)


class InvoicePaymentUriTests(InvoiceTestMixin, TestCase):
    """InvoiceService.build_payment_uri 的 EIP-681 编码行为。

    覆盖金额→最小单位换算、chainId/合约地址编码、原生币与代币两种形态，
    以及精度溢出、非 EVM 链、未分配支付指引时的安全降级（返回 None）。
    """

    PAY_ADDRESS = Web3.to_checksum_address(
        "0x00000000000000000000000000000000000000d1"
    )

    def setUp(self):
        # USDT @ Ethereum，decimals=6，chainId=1。
        self.setup_base_fixtures()

    def make_invoice(self, *, crypto, chain, pay_amount, out_no="uri-order"):
        return self.create_test_invoice(
            out_no=out_no,
            crypto=crypto,
            chain=chain,
            pay_address=self.PAY_ADDRESS,
            pay_amount=pay_amount,
        )

    def test_erc20_token_uri_encodes_contract_recipient_and_base_units(self):
        # 代币：target 是合约地址，/transfer 携带收款地址与最小单位金额。
        invoice = self.make_invoice(
            crypto=self.crypto,
            chain=self.chain,
            pay_amount=Decimal("12.34"),
        )
        contract = self.crypto.address(self.chain)
        self.assertEqual(
            InvoiceService.build_payment_uri(invoice),
            f"ethereum:{contract}@1/transfer"
            f"?address={self.PAY_ADDRESS}&uint256=12340000",
        )

    def test_native_coin_uri_encodes_value_in_wei(self):
        # 原生币：target 即收款地址，金额走 value（18 位精度 → wei）。
        # Ethereum 链的原生币（ETH）在建链链路中已自动登记，复用之，
        # 避免与自动创建的 Crypto/部署撞 unique 约束。
        eth = self.chain.native_coin
        CryptoOnChain.objects.get_or_create(
            crypto=eth,
            chain=self.chain,
            defaults={"address": "", "decimals": 18},
        )
        invoice = self.make_invoice(
            crypto=eth,
            chain=self.chain,
            pay_amount=Decimal("0.05"),
            out_no="native-uri",
        )
        self.assertEqual(
            InvoiceService.build_payment_uri(invoice),
            f"ethereum:{self.PAY_ADDRESS}@1?value=50000000000000000",
        )

    def test_chain_id_comes_from_chain_not_hardcoded(self):
        # 换到 BSC（chainId=56，decimals=18）验证 chainId 与精度均取自链上币种记录。
        bsc = create_active_evm_test_chain(code=ChainCode.BSC)
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=bsc,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000e2"
            ),
            decimals=18,
        )
        invoice = self.make_invoice(
            crypto=self.crypto,
            chain=bsc,
            pay_amount=Decimal("1.5"),
            out_no="bsc-uri",
        )
        contract = self.crypto.address(bsc)
        self.assertEqual(
            InvoiceService.build_payment_uri(invoice),
            f"ethereum:{contract}@56/transfer"
            f"?address={self.PAY_ADDRESS}&uint256=1500000000000000000",
        )

    def test_precision_beyond_chain_decimals_returns_none(self):
        # USDT 链上 6 位精度，7 位报价无法精确编码 → 降级 None，绝不截断金额。
        invoice = self.make_invoice(
            crypto=self.crypto,
            chain=self.chain,
            pay_amount=Decimal("0.1234567"),
        )
        self.assertIsNone(InvoiceService.build_payment_uri(invoice))

    def test_non_evm_chain_returns_none(self):
        # Tron 无跨钱包 URI 标准（chain_id 为 None）→ 不生成结构化 URI。
        tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="http://tron.invalid",
            tron_api_key="tron-key",
            active=True,
        )
        CryptoOnChain.objects.create(
            crypto=self.crypto,
            chain=tron_chain,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        invoice = self.make_invoice(
            crypto=self.crypto,
            chain=tron_chain,
            pay_amount=Decimal("12.34"),
            out_no="tron-uri",
        )
        self.assertIsNone(InvoiceService.build_payment_uri(invoice))

    def test_unallocated_invoice_returns_none(self):
        # 尚未选择支付方式（无 crypto/chain/pay_address/pay_amount）时不生成 URI。
        invoice = self.create_test_invoice(out_no="unallocated-uri")
        self.assertIsNone(InvoiceService.build_payment_uri(invoice))
