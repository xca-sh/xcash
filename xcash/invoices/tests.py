import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.db import DatabaseError
from django.db import IntegrityError
from django.db import close_old_connections
from django.db import connection
from django.db import connections
from django.db import transaction as db_transaction
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
from chains.models import TransferType
from chains.models import Wallet
from common.error_codes import ErrorCode
from common.exceptions import APIError
from currencies.models import ChainToken
from currencies.models import Crypto
from currencies.models import Fiat
from evm.models import VaultSlot
from evm.models import VaultSlotUsage
from invoices.exceptions import InvoiceAllocationError
from invoices.exceptions import InvoiceStatusError
from invoices.models import Invoice
from invoices.models import InvoiceBillingMode
from invoices.models import InvoiceProtocol
from invoices.models import InvoiceStatus
from invoices.serializers import InvoiceCreateSerializer
from invoices.service import InvoiceService
from invoices.tasks import check_expired
from invoices.tasks import fallback_invoice_expired
from invoices.viewsets import InvoiceViewSet
from projects.models import DifferRecipientAddress
from projects.models import Project
from users.models import User


class InvoiceTestMixin:
    """共享的测试基础数据构造 mixin，避免各测试类重复创建 User/Project/Crypto/Chain 等。"""

    def setup_base_fixtures(
        self,
        *,
        username: str = "merchant",
        project_name: str = "TestProject",
        crypto_symbol: str = "USDT",
        chain_name: str = ChainCode.Ethereum,
        with_recipient: bool = True,
    ):
        self.user = User.objects.create(username=username)
        self.project = Project.objects.create(
            name=project_name,
            wallet=Wallet.objects.create(),
        )
        self.crypto = Crypto.objects.create(
            name=f"{crypto_symbol} Token",
            symbol=crypto_symbol,
            prices={"USD": "1"},
            coingecko_id=f"{crypto_symbol.lower()}-test",
        )
        self.chain = Chain.objects.create(
            code=chain_name,
            rpc="",
            active=True,
        )
        Fiat.objects.get_or_create(code="USD")
        if with_recipient:
            self.recipient_address = Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000A1"
            )
            DifferRecipientAddress.objects.create(
                name="收款地址-test",
                project=self.project,
                chain_type=ChainType.EVM,
                address=self.recipient_address,
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
            wallet=Wallet.objects.create(),
        )
        self.eth = Crypto.objects.create(
            name="Ethereum",
            symbol="ETH",
            coingecko_id="ethereum",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )

    def test_remote_signer_project_wallet_can_initialize_and_select_method_without_local_keys(
        self,
    ):
        # 支付链路本身不依赖项目钱包持钥；即使钱包助记词只在 signer 中，也应能正常创建账单和分配收款地址。
        remote_wallet = Wallet.objects.create()
        self.eth.prices = {"USD": "1"}
        self.eth.save(update_fields=["prices"])
        with patch("projects.signals.Wallet.generate", return_value=remote_wallet):
            project = Project.objects.create(
                name="RemoteSignerInvoice",
                wallet=remote_wallet,
            )
        DifferRecipientAddress.objects.create(
            name="RemoteSigner 收款地址",
            project=project,
            chain_type=ChainType.EVM,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000b1"
            ),
        )
        invoice = Invoice.objects.create(
            project=project,
            out_no="remote-signer-invoice",
            title="Remote invoice",
            currency="USD",
            amount=Decimal("15"),
            methods={"ETH": [ChainCode.Ethereum]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

        with (
            patch("invoices.tasks.check_expired.apply_async"),
            patch.object(
                Invoice,
                "select_method",
                wraps=invoice.select_method,
            ) as select_method_mock,
            patch(
                "invoices.service.CryptoService.get_by_symbol",
                return_value=self.eth,
            ),
            patch(
                "invoices.service.ChainService.get_by_code",
                return_value=self.chain,
            ),
            patch(
                "invoices.service.FiatService.get_by_code",
                side_effect=lambda code: SimpleNamespace(
                    code=code,
                    fiat_price=Mock(return_value=Decimal("1")),
                ),
            ),
            patch(
                "invoices.models.FiatService.to_crypto",
                return_value=Decimal("15"),
            ),
            patch(
                "invoices.models.FiatService.get_by_code",
                side_effect=lambda code: SimpleNamespace(
                    code=code,
                    fiat_price=Mock(return_value=Decimal("1")),
                ),
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            InvoiceService.initialize_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertEqual(
            invoice.pay_address,
            Web3.to_checksum_address("0x00000000000000000000000000000000000000b1"),
        )
        select_method_mock.assert_called_once_with(self.eth, self.chain)


class InvoicePaymentSelectionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username="merchant-payments")
        self.project = Project.objects.create(
            name="SlotProject",
            wallet=Wallet.objects.create(),
        )
        self.crypto = Crypto.objects.create(
            name="Tether USD",
            symbol="USDT",
            coingecko_id="tether-invoice-slots",
        )
        self.chain_a = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        self.chain_b = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="",
            active=True,
        )
        Fiat.objects.get_or_create(code="USD")
        DifferRecipientAddress.objects.create(
            name="收款地址-1",
            project=self.project,
            chain_type=ChainType.EVM,
            address="0x00000000000000000000000000000000000000A1",
        )

    def create_invoice(self, *, out_no: str = "payment-order") -> Invoice:
        return Invoice.objects.create(
            project=self.project,
            out_no=out_no,
            title="Slot invoice",
            currency="USDT",
            amount=Decimal("10"),
            methods={"USDT": [ChainCode.Ethereum, ChainCode.BSC]},
            expires_at=timezone.now() + timedelta(minutes=10),
        )

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

    def test_drop_invoice_keeps_current_payment_when_not_reused(self):
        # 若链上观测后来被回滚，未被复用的当前支付指引可继续等待再次匹配。
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

        InvoiceService.drop_invoice(invoice)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.WAITING)
        self.assertIsNone(invoice.transfer_id)
        self.assertEqual(invoice.pay_address, pay_address)
        self.assertEqual(invoice.pay_amount, pay_amount)

    def test_select_method_skips_expired_waiting_payment_combo(self):
        # 回归：旧账单已过 expires_at 但状态仍是 WAITING 时，其 (pay_address, pay_amount)
        # 组合仍被 uniq_invoice_active_payment 约束锁定。差额分配必须把它视为已占用、
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

    def test_drop_invoice_clears_payment_when_occupied_by_expired_waiting_invoice(self):
        # 回归：账单 CONFIRMING 期间其组合脱离 uniq_invoice_active_payment 约束，可能被
        # 新的 WAITING 账单复用。若该占用者已过 expires_at 但状态仍是 WAITING（过期任务
        # 尚未翻转），drop_invoice 的占用判定必须仍能识别它、先清空当前支付指引再回退
        # 状态——否则回退为 WAITING 会命中约束抛 IntegrityError，账单将卡死在 CONFIRMING。
        confirming = self.create_invoice(out_no="drop-occupied-confirming")
        confirming.select_method(self.crypto, self.chain_a)
        confirming.refresh_from_db()
        pay_address = confirming.pay_address
        pay_amount = confirming.pay_amount

        # 推进到 CONFIRMING（此时其组合脱离约束）。
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=pay_amount,
            pay_address=pay_address,
        )
        InvoiceService.try_match_invoice(transfer)
        confirming.refresh_from_db()
        self.assertEqual(confirming.status, InvoiceStatus.CONFIRMING)

        # 新账单合法复用同一组合（因 confirming 已不在约束内），随后过期但保持 WAITING。
        occupant = self.create_invoice(out_no="drop-occupied-waiting")
        Invoice.objects.filter(pk=occupant.pk).update(
            crypto=self.crypto,
            chain=self.chain_a,
            pay_address=pay_address,
            pay_amount=pay_amount,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        # 回退 confirming：不应抛 IntegrityError，且应清空被占用的支付指引。
        InvoiceService.drop_invoice(confirming)

        confirming.refresh_from_db()
        self.assertEqual(confirming.status, InvoiceStatus.WAITING)
        self.assertIsNone(confirming.transfer_id)
        self.assertIsNone(confirming.pay_address)
        self.assertIsNone(confirming.pay_amount)
        self.assertIsNone(confirming.crypto_id)
        self.assertIsNone(confirming.chain_id)

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
    def test_pre_notify_enabled_emits_confirming_webhook(self, create_event_mock):
        # 开启 pre_notify 时，try_match_invoice 应发送 confirmed=False 的预通知。
        self.project.pre_notify = True
        self.project.save(update_fields=["pre_notify"])
        invoice = self.create_invoice(out_no="payment-prenotify")
        Invoice.objects.filter(pk=invoice.pk).update(
            notify_url="https://merchant.example.com/invoice-prenotify"
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
        create_event_mock.assert_called_once()
        payload = create_event_mock.call_args.kwargs["payload"]
        self.assertEqual(payload["type"], "invoice")
        self.assertFalse(payload["data"]["confirmed"])
        self.assertEqual(
            create_event_mock.call_args.kwargs["delivery_url"],
            "https://merchant.example.com/invoice-prenotify",
        )

    @patch("invoices.service.WebhookService.create_event")
    def test_pre_notify_disabled_does_not_emit_webhook(self, create_event_mock):
        # 关闭 pre_notify 时，try_match_invoice 不应发送任何 webhook。
        invoice = self.create_invoice(out_no="payment-noprenotify")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        matched = InvoiceService.try_match_invoice(transfer)
        self.assertTrue(matched)
        create_event_mock.assert_not_called()

    @patch(
        "invoices.service.WebhookService.create_event",
        side_effect=Exception("boom"),
    )
    def test_pre_notify_failure_does_not_block_invoice_match(self, create_event_mock):
        # 预通知发送异常时，invoice 匹配与状态推进不应被回滚。
        self.project.pre_notify = True
        self.project.save(update_fields=["pre_notify"])
        invoice = self.create_invoice(out_no="payment-prenotify-fail")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        matched = InvoiceService.try_match_invoice(transfer)
        self.assertTrue(matched)
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.CONFIRMING)
        self.assertEqual(invoice.transfer_id, transfer.pk)

    def test_pre_notify_db_error_does_not_block_invoice_match(self):
        # 关键回归：模拟 webhook 创建过程中触发 DatabaseError 并标记当前连接 needs_rollback；
        # try_match_invoice 内的嵌套 savepoint 必须把回滚范围限制在 savepoint 内，
        # 让外层 invoice 匹配事务仍能正常提交（invoice/paySlot/transfer 状态全部保留）。
        def _simulate_db_error(*args, **kwargs):
            # set_rollback 重现 Django 在真实 DB 错误时对连接打的回滚标记。
            db_transaction.set_rollback(True)
            raise DatabaseError("simulated db error")

        self.project.pre_notify = True
        self.project.save(update_fields=["pre_notify"])
        invoice = self.create_invoice(out_no="payment-prenotify-dberror")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        with patch(
            "invoices.service.WebhookService.create_event",
            side_effect=_simulate_db_error,
        ):
            matched = InvoiceService.try_match_invoice(transfer)
        self.assertTrue(matched)
        invoice.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.CONFIRMING)
        self.assertEqual(invoice.transfer_id, transfer.pk)
        self.assertEqual(transfer.type, TransferType.Invoice)


class InvoicePaymentSelectionConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create(username="merchant-concurrency")
        self.project = Project.objects.create(
            name="ConcurrencyProject",
            wallet=Wallet.objects.create(),
        )
        self.crypto = Crypto.objects.create(
            name="Tether USD Concurrency",
            symbol="USDTC",
            prices={"USD": "1"},
            coingecko_id="tether-invoice-concurrency",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        Fiat.objects.get_or_create(code="USD")
        DifferRecipientAddress.objects.create(
            name="收款地址-1",
            project=self.project,
            chain_type=ChainType.EVM,
            address="0x00000000000000000000000000000000000000A1",
        )

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
            wallet=Wallet.objects.create(),
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

    def test_available_methods_only_exposes_usdt_for_tron_invoice(self):
        project = Project.objects.create(
            name="Invoice Capability Project",
            wallet=Wallet.objects.create(),
        )
        tron_usdt = Crypto.objects.create(
            name="Tether USD",
            symbol="USDT",
            coingecko_id="tether-tron-invoice-capability",
        )
        tron_usdc = Crypto.objects.create(
            name="USD Coin",
            symbol="USDC",
            coingecko_id="usd-coin-tron-invoice-capability",
        )
        tron_chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="http://tron.invalid",
            active=True,
        )
        ChainToken.objects.create(
            crypto=tron_usdt,
            chain=tron_chain,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )
        ChainToken.objects.create(
            crypto=tron_usdc,
            chain=tron_chain,
            address="TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
            decimals=6,
        )
        DifferRecipientAddress.objects.create(
            name="tron-pay",
            project=project,
            chain_type=ChainType.TRON,
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(methods["USDT"], [tron_chain.code])
        self.assertNotIn("USDC", methods)

    def test_contract_available_methods_exposes_evm_for_vault_project(self):
        # 合约模式（CONTRACT）：项目设置了 vault 但没有任何差额收款地址。合约收款走
        # VaultSlot，不依赖 DifferRecipientAddress，故 EVM 链币组合在合约模式下可用。
        project = Project.objects.create(
            name="Invoice Contract Only Project",
            wallet=Wallet.objects.create(),
            vault="0x0000000000000000000000000000000000008801",
        )
        usdt = Crypto.objects.create(
            name="Tether USD EVM",
            symbol="USDTEVMCO",
            coingecko_id="tether-evm-contract-only",
        )
        eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        ChainToken.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000008802",
            decimals=6,
        )

        contract_methods = Invoice.available_methods(
            project, InvoiceBillingMode.CONTRACT
        )
        differ_methods = Invoice.available_methods(project, InvoiceBillingMode.DIFFER)

        self.assertEqual(contract_methods[usdt.symbol], [eth_chain.code])
        # 没配差额收款地址 → 差额模式无可用方式。
        self.assertEqual(differ_methods, {})

    def test_differ_available_methods_excludes_native_coin(self):
        project = Project.objects.create(
            name="Invoice Differ Non Native Project",
            wallet=Wallet.objects.create(),
        )
        eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        usdt = Crypto.objects.create(
            name="USDT EVM Differ Non Native",
            symbol="USDTDIFFNN",
            coingecko_id="usdt-evm-differ-non-native",
        )
        ChainToken.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000008841",
            decimals=6,
        )
        DifferRecipientAddress.objects.create(
            name="evm-differ-pay",
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000008842",
        )

        methods = Invoice.available_methods(project, InvoiceBillingMode.DIFFER)

        self.assertEqual(methods[usdt.symbol], [eth_chain.code])
        self.assertNotIn(eth_chain.native_coin.symbol, methods)

    @override_settings(IS_SAAS=True, INTERNAL_API_TOKEN="xcash-saas-token")
    def test_available_methods_filters_by_cached_saas_chain_crypto_whitelist(self):
        project = Project.objects.create(
            name="Invoice SaaS Allowed Methods Project",
            wallet=Wallet.objects.create(),
        )
        eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        bsc_chain = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="",
            active=True,
        )
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
        ChainToken.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000009911",
            decimals=6,
        )
        ChainToken.objects.create(
            crypto=usdt,
            chain=bsc_chain,
            address="0x0000000000000000000000000000000000009912",
            decimals=6,
        )
        ChainToken.objects.create(
            crypto=usdc,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000009913",
            decimals=6,
        )
        DifferRecipientAddress.objects.create(
            name="evm-pay",
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000009914",
        )
        cache.set(
            f"saas:permission:{project.appid}",
            {
                "frozen": False,
                "enable_deposit_withdrawal": True,
                "allowed_chain_codes": [eth_chain.code],
                "allowed_crypto_symbols": [usdt.symbol],
            },
            None,
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(set(methods), {usdt.symbol})
        self.assertEqual(methods[usdt.symbol], [eth_chain.code])

    @override_settings(IS_SAAS=True, INTERNAL_API_TOKEN="xcash-saas-token")
    def test_available_methods_empty_saas_whitelists_keep_all_methods(self):
        project = Project.objects.create(
            name="Invoice SaaS Empty Whitelist Project",
            wallet=Wallet.objects.create(),
        )
        eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        bsc_chain = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="",
            active=True,
        )
        usdt = Crypto.objects.create(
            name="USDT SaaS Empty",
            symbol="USDTSAASEM",
            coingecko_id="usdt-saas-empty-methods",
        )
        ChainToken.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000009921",
            decimals=6,
        )
        ChainToken.objects.create(
            crypto=usdt,
            chain=bsc_chain,
            address="0x0000000000000000000000000000000000009922",
            decimals=6,
        )
        DifferRecipientAddress.objects.create(
            name="evm-pay",
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000009923",
        )
        cache.set(
            f"saas:permission:{project.appid}",
            {
                "frozen": False,
                "enable_deposit_withdrawal": True,
                "allowed_chain_codes": [],
                "allowed_crypto_symbols": [],
            },
            None,
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(set(methods[usdt.symbol]), {eth_chain.code, bsc_chain.code})

    @override_settings(IS_SAAS=True, INTERNAL_API_TOKEN="xcash-saas-token")
    def test_available_methods_saas_chain_whitelist_is_case_insensitive(self):
        # SaaS 侧返回的链 code 大小写不保证与系统一致；归一后比对，避免组合被静默过滤。
        project = Project.objects.create(
            name="Invoice SaaS Case Insensitive Project",
            wallet=Wallet.objects.create(),
        )
        eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        usdt = Crypto.objects.create(
            name="USDT SaaS Case",
            symbol="USDTSAASCI",
            coingecko_id="usdt-saas-case-insensitive",
        )
        ChainToken.objects.create(
            crypto=usdt,
            chain=eth_chain,
            address="0x0000000000000000000000000000000000009931",
            decimals=6,
        )
        DifferRecipientAddress.objects.create(
            name="evm-pay",
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000009932",
        )
        cache.set(
            f"saas:permission:{project.appid}",
            {
                "frozen": False,
                "enable_deposit_withdrawal": True,
                "allowed_chain_codes": [eth_chain.code.upper()],
                "allowed_crypto_symbols": [usdt.symbol],
            },
            None,
        )

        methods = Invoice.available_methods(project)

        self.assertEqual(methods[usdt.symbol], [eth_chain.code])


class InvoiceDifferBillingValidationTests(TestCase):
    """差额账单链类型校验：差额模式可用性取决于项目是否为该 chain_type 配了差额收款地址，
    而非硬绑 Tron——EVM 同样可以走差额模式。"""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        Fiat.objects.get_or_create(code="USD")
        self.eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        self.usdt = Crypto.objects.create(
            name="USDT EVM Differ",
            symbol="USDTEVMD",
            coingecko_id="usdt-evm-differ-billing",
        )
        ChainToken.objects.create(
            crypto=self.usdt,
            chain=self.eth_chain,
            address="0x0000000000000000000000000000000000007701",
            decimals=6,
        )

    def build_serializer(self, project):
        request = self.factory.post(
            "/invoices",
            {},
            format="json",
            HTTP_XC_APPID=project.appid,
        )
        data = {
            "out_no": "differ-evm-order",
            "title": "differ evm",
            "currency": self.usdt.symbol,
            "amount": "10",
            "methods": {self.usdt.symbol: [self.eth_chain.code]},
            "billing_mode": InvoiceBillingMode.DIFFER,
        }
        return InvoiceCreateSerializer(data=data, context={"request": request})

    def test_evm_differ_allowed_when_recipient_address_configured(self):
        # 配了 EVM 差额收款地址 → EVM 合约代币可走差额模式，校验通过。
        project = Project.objects.create(
            name="EVM Differ With Recipient",
            wallet=Wallet.objects.create(),
        )
        DifferRecipientAddress.objects.create(
            name="evm-pay",
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000007702",
        )

        serializer = self.build_serializer(project)

        self.assertTrue(serializer.is_valid(raise_exception=True))

    def test_evm_differ_rejected_without_recipient_address(self):
        # 只设了 vault（合约收款）但没配差额收款地址：available_methods 仍会因合约路径暴露
        # EVM，但差额模式缺少收款地址无法分配，必须在校验阶段拒绝。
        project = Project.objects.create(
            name="EVM Vault Only",
            wallet=Wallet.objects.create(),
            vault="0x0000000000000000000000000000000000007703",
        )

        serializer = self.build_serializer(project)

        with self.assertRaises(APIError) as ctx:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(ctx.exception.error_code, ErrorCode.NO_RECIPIENT_ADDRESS)

    def test_evm_differ_rejects_native_coin_method(self):
        project = Project.objects.create(
            name="EVM Differ Native Rejected",
            wallet=Wallet.objects.create(),
        )
        DifferRecipientAddress.objects.create(
            name="evm-pay",
            project=project,
            chain_type=ChainType.EVM,
            address="0x0000000000000000000000000000000000007704",
        )
        request = self.factory.post(
            "/invoices",
            {},
            format="json",
            HTTP_XC_APPID=project.appid,
        )
        native = self.eth_chain.native_coin
        serializer = InvoiceCreateSerializer(
            data={
                "out_no": "differ-evm-native-order",
                "title": "differ evm native",
                "currency": "USD",
                "amount": "10",
                "methods": {native.symbol: [self.eth_chain.code]},
                "billing_mode": InvoiceBillingMode.DIFFER,
            },
            context={"request": request},
        )

        with self.assertRaises(APIError) as ctx:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(ctx.exception.error_code, ErrorCode.NO_RECIPIENT_ADDRESS)


class InvoiceContractBillingValidationTests(TestCase):
    """合约账单的最终 methods 生成：CONTRACT 模式只暴露 EVM 链，默认 methods 自动过滤掉
    Tron（而非报错），是「系统按 billing_mode 动态生成最终 methods」的核心行为。"""

    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()
        # 同时配了 Tron 差额地址和 vault 的多链商户。
        self.project = Project.objects.create(
            name="Invoice Mixed Billing Project",
            wallet=Wallet.objects.create(),
            vault="0x0000000000000000000000000000000000007801",
        )
        DifferRecipientAddress.objects.create(
            name="tron-pay",
            project=self.project,
            chain_type=ChainType.TRON,
            address="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
        )
        # Tron 能力规则（supports_existing_invoice_method）限定 Tron 上只放行 USDT，
        # 故混合多链场景必须用真实 USDT 符号，Tron 侧才会出现在可用方式里。
        self.usdt = Crypto.objects.create(
            name="Tether USD",
            symbol="USDT",
            coingecko_id="usdt-mixed-billing",
        )
        self.eth_chain = Chain.objects.create(
            code=ChainCode.Ethereum, rpc="", active=True
        )
        self.tron_chain = Chain.objects.create(
            code=ChainCode.Tron, rpc="http://tron.invalid", active=True
        )
        ChainToken.objects.create(
            crypto=self.usdt,
            chain=self.eth_chain,
            address="0x0000000000000000000000000000000000007802",
            decimals=6,
        )
        ChainToken.objects.create(
            crypto=self.usdt,
            chain=self.tron_chain,
            address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6,
        )

    def build_serializer(self, *, methods, billing_mode):
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
            "billing_mode": billing_mode,
        }
        return InvoiceCreateSerializer(data=data, context={"request": request})

    def test_default_methods_contract_filters_out_tron(self):
        # 不传 methods + CONTRACT：系统应自动生成 EVM-only 的最终 methods，
        # Tron 被过滤掉而不是抛错（修复并集 + reject 导致的误报）。
        serializer = self.build_serializer(
            methods={}, billing_mode=InvoiceBillingMode.CONTRACT
        )

        self.assertTrue(serializer.is_valid(raise_exception=True))
        self.assertEqual(
            serializer.validated_data["methods"],
            {self.usdt.symbol: [self.eth_chain.code]},
        )

    def test_default_methods_differ_filters_out_evm(self):
        # 同一项目走差额：差额收款地址只配了 Tron，故默认 methods 只含 Tron。
        serializer = self.build_serializer(
            methods={}, billing_mode=InvoiceBillingMode.DIFFER
        )

        self.assertTrue(serializer.is_valid(raise_exception=True))
        self.assertEqual(
            serializer.validated_data["methods"],
            {self.usdt.symbol: [self.tron_chain.code]},
        )

    def test_explicit_tron_under_contract_rejected(self):
        # 显式要求 Tron 走合约 → 合约模式不支持 Tron，拒绝。
        serializer = self.build_serializer(
            methods={self.usdt.symbol: [self.tron_chain.code]},
            billing_mode=InvoiceBillingMode.CONTRACT,
        )

        with self.assertRaises(APIError) as ctx:
            serializer.is_valid(raise_exception=True)
        self.assertEqual(ctx.exception.error_code, ErrorCode.NO_RECIPIENT_ADDRESS)


class InvoiceConfirmDropStatusTests(TestCase):
    """confirm_invoice / drop_invoice 的状态前置校验测试。"""

    def setUp(self):
        self.user = User.objects.create(username="merchant-status")
        self.project = Project.objects.create(
            name="StatusProject",
            wallet=Wallet.objects.create(),
        )

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

    def test_confirm_invoice_rejects_non_confirming_status(self):
        # confirm_invoice 仅接受 CONFIRMING 状态，其余应抛出 InvoiceStatusError。
        for bad_status in [
            InvoiceStatus.WAITING,
            InvoiceStatus.COMPLETED,
            InvoiceStatus.EXPIRED,
        ]:
            invoice = self._make_invoice(bad_status)
            with self.assertRaises(InvoiceStatusError):
                InvoiceService.confirm_invoice(invoice)

    def test_drop_invoice_rejects_non_confirming_status(self):
        # drop_invoice 仅接受 CONFIRMING 状态，其余应抛出 InvoiceStatusError。
        for bad_status in [
            InvoiceStatus.WAITING,
            InvoiceStatus.COMPLETED,
            InvoiceStatus.EXPIRED,
        ]:
            invoice = self._make_invoice(bad_status)
            with self.assertRaises(InvoiceStatusError):
                InvoiceService.drop_invoice(invoice)

    @patch("invoices.service.send_internal_callback")
    @patch("invoices.service.WebhookService.create_event")
    def test_confirm_native_invoice_uses_invoice_notify_url(
        self, create_event_mock, _callback_mock
    ):
        # 原生 Invoice 若配置了账单级 notify_url，最终通知应投递到该地址；
        # 为空时 WebhookEvent.delivery_url 维持默认空串，由投递层 fallback 到 Project.webhook。
        crypto = Crypto.objects.create(
            name="Status USDT",
            symbol="STATUS-USDT",
            prices={"USD": "1"},
            coingecko_id="status-usdt",
        )
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="status-native-notify",
            title="Status native notify",
            currency="USD",
            amount=Decimal("10"),
            methods={},
            status=InvoiceStatus.CONFIRMING,
            protocol=InvoiceProtocol.NATIVE,
            crypto=crypto,
            notify_url="https://merchant.example.com/invoice-notify",
            expires_at=timezone.now() + timedelta(minutes=10),
        )

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
            wallet=Wallet.objects.create(),
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
        self.user = User.objects.create(username="merchant-expired-match")
        self.project = Project.objects.create(
            name="ExpiredMatchProject",
            wallet=Wallet.objects.create(),
        )
        self.crypto = Crypto.objects.create(
            name="Tether Expired",
            symbol="USDTE",
            prices={"USD": "1"},
            coingecko_id="tether-expired-match",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        Fiat.objects.get_or_create(code="USD")
        self.recipient_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000E1"
        )
        DifferRecipientAddress.objects.create(
            name="收款地址-expired",
            project=self.project,
            chain_type=ChainType.EVM,
            address=self.recipient_address,
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
        self.assertEqual(invoice.status, InvoiceStatus.CONFIRMING)
        self.assertEqual(invoice.transfer_id, transfer.pk)


class FallbackInvoiceExpiredTests(TestCase):
    """fallback_invoice_expired 批量过期的逻辑测试。"""

    def setUp(self):
        self.user = User.objects.create(username="merchant-fallback")
        self.project = Project.objects.create(
            name="FallbackProject",
            wallet=Wallet.objects.create(),
        )
        self.crypto = Crypto.objects.create(
            name="Tether Fallback",
            symbol="USDTF",
            prices={"USD": "1"},
            coingecko_id="tether-fallback",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        Fiat.objects.get_or_create(code="USD")
        DifferRecipientAddress.objects.create(
            name="收款地址-fallback",
            project=self.project,
            chain_type=ChainType.EVM,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000F1"
            ),
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

    def test_fallback_skips_confirming_invoice(self):
        # 已进入 CONFIRMING 的账单不应被 fallback 误过期。
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="fallback-confirming",
            title="Fallback confirming",
            currency="USDTF",
            amount=Decimal("10"),
            methods={"USDTF": [ChainCode.Ethereum]},
            status=InvoiceStatus.CONFIRMING,
            expires_at=timezone.now() - timedelta(minutes=1),
        )

        fallback_invoice_expired()

        invoice.refresh_from_db()
        # 状态不变
        self.assertEqual(invoice.status, InvoiceStatus.CONFIRMING)


class CheckExpiredAtomicityTests(TransactionTestCase):
    """验证 check_expired 在并发场景下的原子性。"""

    def setUp(self):
        self.user = User.objects.create(username="merchant-atomic")
        self.project = Project.objects.create(
            name="AtomicProject",
            wallet=Wallet.objects.create(),
        )
        self.crypto = Crypto.objects.create(
            name="Tether Atomic",
            symbol="USDTA",
            prices={"USD": "1"},
            coingecko_id="tether-atomic",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        Fiat.objects.get_or_create(code="USD")
        DifferRecipientAddress.objects.create(
            name="收款地址-atomic",
            project=self.project,
            chain_type=ChainType.EVM,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000A7"
            ),
        )

    def test_check_expired_skips_already_matched_invoice(self):
        # 并发场景：check_expired 执行时如果账单已被 try_match 推进到 CONFIRMING，
        # select_for_update + status 条件应使其安全跳过，不会误过期。
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

        # check_expired 应该安全跳过已 CONFIRMING 的账单
        check_expired(invoice.pk)

        invoice.refresh_from_db()
        self.assertEqual(invoice.status, InvoiceStatus.CONFIRMING)
        self.assertEqual(invoice.transfer_id, transfer.pk)


class InvoiceAllocationRetryExhaustedTests(InvoiceTestMixin, TestCase):
    """MAX_ALLOCATION_RETRY 耗尽场景：所有地址/金额组合被占用时应抛出 InvoiceAllocationError。"""

    def setUp(self):
        self.setup_base_fixtures(
            username="merchant-retry",
            project_name="RetryProject",
            crypto_symbol="USDTR",
            chain_name=ChainCode.BSC,
        )

    def test_select_method_raises_when_all_payments_occupied(self):
        # 当所有地址/金额组合都被占用时，应抛出 InvoiceAllocationError。
        invoice = self.create_test_invoice(out_no="retry-order")

        with (
            patch.object(Invoice, "get_pay_differ", return_value=(None, None)),
            self.assertRaises(InvoiceAllocationError),
        ):
            invoice.select_method(self.crypto, self.chain)


class InvoiceCreatePermissionCheckTests(TestCase):
    """v2 SaaS 模式：账单收款入口调用 check_saas_permission。"""

    def setUp(self):
        self.project = Project.objects.create(
            name="InvoicePermCheckProject",
            wallet=Wallet.objects.create(),
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
        """账单创建时只校验 invoice 账号/白名单语义，不占用 deposit 功能锁。"""
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
    def test_select_method_checks_selected_chain_and_crypto(self, mock_check):
        """支付页选择支付方式时，最终选中的链币组合必须经过 SaaS 白名单校验。"""
        invoice = Mock(
            status=InvoiceStatus.WAITING,
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
            chain_code="ethereum-mainnet",
            crypto_symbol="USDT",
        )

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
        invoice.refresh_from_db()

        with CaptureQueriesContext(connection) as captured:
            InvoiceService.confirm_invoice(invoice)

        self._assert_lock_scope_is_self_only(self._for_update_tails(captured))

    def test_drop_invoice_locks_only_self_rows(self):
        invoice = self.create_invoice(out_no="lock-scope-drop")
        invoice.select_method(self.crypto, self.chain_a)
        transfer = self.create_transfer(
            chain=self.chain_a,
            pay_amount=invoice.pay_amount,
            pay_address=invoice.pay_address,
        )
        InvoiceService.try_match_invoice(transfer)
        invoice.refresh_from_db()

        with CaptureQueriesContext(connection):
            InvoiceService.drop_invoice(invoice)


class InvoiceBillingModeFieldTest(TestCase, InvoiceTestMixin):
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

    def test_invoice_default_billing_mode_is_differ(self):
        invoice = self._make_minimal_invoice()
        self.assertEqual(invoice.billing_mode, InvoiceBillingMode.DIFFER)

    def test_contract_slot_uses_project_vault(self):
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F01"
        )
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        invoice = self.create_test_invoice(
            out_no="contract-vault-source",
            billing_mode=InvoiceBillingMode.CONTRACT,
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        invoice = self.create_test_invoice(
            out_no="contract-slot-row",
            billing_mode=InvoiceBillingMode.CONTRACT,
        )

        with self.captureOnCommitCallbacks(execute=False):
            pay_address, pay_amount = (
                invoice._allocate_contract_slot(
                    self.crypto,
                    self.chain,
                    Decimal("10"),
                )
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        invoice = self.create_test_invoice(
            out_no="contract-slot-object",
            billing_mode=InvoiceBillingMode.CONTRACT,
        )

        with self.captureOnCommitCallbacks(execute=False):
            slot = invoice._get_contract_vault_slot(
                crypto=self.crypto,
                chain=self.chain,
                crypto_amount=Decimal("10"),
            )

        self.assertEqual(slot.project, self.project)
        self.assertEqual(slot.chain, self.chain)
        self.assertEqual(slot.usage, VaultSlotUsage.INVOICE)
        self.assertEqual(slot.invoice_index, 0)
        self.assertIsNone(slot.customer_id)
        self.assertEqual(slot.project.vault, vault_address)

    def test_contract_slot_reuses_slot_when_existing_invoice_expired(self):
        # 旧账单已被过期任务翻成 EXPIRED 后，其 (pay_address, pay_amount) 组合脱离
        # uniq_invoice_active_payment 约束（约束只覆盖 status=WAITING），新合约账单
        # 可以安全复用同一 VaultSlot 地址。
        vault_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000F03"
        )
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-reuse-first",
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-waiting-expired-first",
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-reuse-amount-first",
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        other_crypto = Crypto.objects.create(
            name="USD Coin Contract",
            symbol="USDCC",
            prices={"USD": "1"},
            coingecko_id="usdc-contract-slot",
        )
        first_invoice = self.create_test_invoice(
            out_no="contract-reuse-crypto-first",
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        first_invoice = self.create_test_invoice(
            out_no="contract-overlap-first",
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
        self.project.vault = vault_address
        self.project.save(update_fields=["vault"])
        invoice = self.create_test_invoice(
            out_no="contract-retry-reselect",
            billing_mode=InvoiceBillingMode.CONTRACT,
        )
        with self.captureOnCommitCallbacks(execute=False):
            VaultSlot.ensure_invoice_address(
                project=self.project,
                chain=self.chain,
                invoice_index=0,
            )
            VaultSlot.ensure_invoice_address(
                project=self.project,
                chain=self.chain,
                invoice_index=1,
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
                "_get_contract_vault_slot",
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
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
            billing_mode=InvoiceBillingMode.CONTRACT,
        )
        self.invoice.refresh_from_db()

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
        self.assertEqual(self.invoice.status, InvoiceStatus.CONFIRMING)

    def test_does_not_match_when_transfer_amount_greater_than_pay_amount(self):
        transfer = self._make_transfer(Decimal("150"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertFalse(matched)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.WAITING)

    def test_contract_match_uses_exact_amount_when_slot_is_shared(self):
        newer_invoice = self.create_test_invoice(
            out_no="contract-match-newer",
            billing_mode=InvoiceBillingMode.CONTRACT,
            amount=Decimal("150"),
        )
        Invoice.objects.filter(pk=newer_invoice.pk).update(
            crypto=self.crypto,
            chain=self.chain,
            pay_address=self.slot_address,
            pay_amount=Decimal("150"),
            billing_mode=InvoiceBillingMode.CONTRACT,
        )

        transfer = self._make_transfer(Decimal("100"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertTrue(matched)
        self.invoice.refresh_from_db()
        newer_invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.CONFIRMING)
        self.assertEqual(self.invoice.transfer_id, transfer.pk)
        self.assertEqual(newer_invoice.status, InvoiceStatus.WAITING)

    @patch("evm.models.VaultSlot.schedule_collect_for_invoice")
    @patch("invoices.service.send_internal_callback")
    @patch("invoices.service.WebhookService.create_event")
    def test_confirm_contract_invoice_schedules_erc20_slot_collection(
        self,
        _create_event_mock,
        _send_internal_callback_mock,
        schedule_collect_mock,
    ):
        transfer = self._make_transfer(Decimal("100"))
        InvoiceService.try_match_invoice(transfer)
        self.invoice.refresh_from_db()

        InvoiceService.confirm_invoice(self.invoice)

        schedule_collect_mock.assert_called_once_with(self.invoice.pk)

    def test_does_not_match_when_transfer_amount_less_than_pay_amount(self):
        transfer = self._make_transfer(Decimal("99.99"))

        matched = InvoiceService.try_match_invoice(transfer)

        self.assertFalse(matched)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, InvoiceStatus.WAITING)
