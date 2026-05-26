import hashlib
import hmac
import json
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import ANY
from unittest.mock import Mock
from unittest.mock import patch

from django.test import RequestFactory
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from hexbytes import HexBytes
from stress.evm import send_erc20
from stress.evm import send_native
from stress.models import DepositStressCaseStatus
from stress.models import InvoiceStressCase
from stress.models import InvoiceStressCaseStatus
from stress.models import StressRun
from stress.models import StressRunStatus
from stress.payment import simulate_payment
from stress.service import _ANVIL_RECIPIENT_ADDRESSES
from stress.service import StressService
from stress.service import _build_deposit_cases
from stress.service import _setup_differ_recipient_addresses
from stress.tasks import _execute
from stress.tasks import _execute_deposit
from stress.tasks import execute_deposit_case_payment
from stress.tasks import execute_stress_case_payment
from stress.tasks import prepare_stress
from stress.views import _handle_webhook

from chains.models import Chain
from chains.models import ChainType
from chains.models import Wallet
from currencies.models import ChainToken
from currencies.models import Crypto
from projects.models import DifferRecipientAddress
from projects.models import Project


@override_settings(STRESS_WEBHOOK_BASE_URL="http://localhost")
class StressServiceTests(SimpleTestCase):
    databases = {"default"}

    def test_create_invoice_posts_project_available_local_methods(self):
        from invoices.models import InvoiceBillingMode

        project = SimpleNamespace(appid="app-1", hmac_key="secret")
        stress_run = SimpleNamespace(pk=12, project=project)
        case = SimpleNamespace(
            sequence=7,
            stress_run=stress_run,
            billing_mode=InvoiceBillingMode.DIFFER,
        )

        response = Mock()
        response.status_code = 200
        response.json.return_value = {"sys_no": "INV-1"}

        with (
            patch(
                "stress.service.Invoice.available_methods",
                return_value={
                    "ETH": ["ethereum-local"],
                    "USDT": ["ethereum-local"],
                },
            ),
            patch.object(
                StressService,
                "_build_hmac_headers",
                return_value={"X-Test": "1"},
            ) as build_headers_mock,
            patch("stress.service.httpx.post", return_value=response) as post_mock,
        ):
            result = StressService.create_invoice(case)

        self.assertEqual(result, {"sys_no": "INV-1"})
        body = post_mock.call_args.kwargs["content"]
        payload = json.loads(body)
        self.assertEqual(
            payload["methods"],
            {
                "ETH": ["ethereum-local"],
                "USDT": ["ethereum-local"],
            },
        )
        self.assertEqual(payload["out_no"], "STRESS-12-7")
        build_headers_mock.assert_called_once_with(project, body)

    def test_create_invoice_includes_case_billing_mode(self):
        from invoices.models import InvoiceBillingMode

        project = SimpleNamespace(appid="app-2", hmac_key="secret")
        stress_run = SimpleNamespace(pk=33, project=project)
        case = SimpleNamespace(
            sequence=4,
            stress_run=stress_run,
            billing_mode=InvoiceBillingMode.CONTRACT,
        )
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"sys_no": "INV-CONTRACT"}

        with (
            patch(
                "stress.service.Invoice.available_methods",
                return_value={
                    "ETH": ["ethereum-local"],
                    "USDT": ["ethereum-local"],
                },
            ),
            patch.object(
                StressService,
                "_build_hmac_headers",
                return_value={"X-Test": "1"},
            ),
            patch("stress.service.httpx.post", return_value=response) as post_mock,
        ):
            StressService.create_invoice(case)

        body = post_mock.call_args.kwargs["content"]
        payload = json.loads(body)
        self.assertEqual(payload["billing_mode"], InvoiceBillingMode.CONTRACT)

    def test_create_invoice_raises_when_project_methods_incomplete(self):
        project = SimpleNamespace(appid="app-1", hmac_key="secret")
        stress_run = SimpleNamespace(pk=12, project=project)
        case = SimpleNamespace(sequence=7, stress_run=stress_run)

        with (
            patch(
                "stress.service.Invoice.available_methods",
                return_value={
                    "ETH": ["ethereum-local"],
                },
            ),
            patch("stress.service.httpx.post") as post_mock,
            self.assertRaisesMessage(
                RuntimeError,
                "Stress Project 收款地址未准备完整",
            ),
        ):
            StressService.create_invoice(case)

        post_mock.assert_not_called()

    def test_select_method_posts_one_fixed_local_method_without_fetching_invoice(self):
        project = SimpleNamespace(appid="app-1")
        stress_run = SimpleNamespace(project=project)
        case = SimpleNamespace(invoice_sys_no="INV-1", stress_run=stress_run)

        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "crypto": "USDT",
            "chain": "ethereum-local",
        }

        with (
            patch(
                "stress.service.random.choice",
                return_value=("USDT", "ethereum-local"),
            ) as choice_mock,
            patch("stress.service.httpx.get") as get_mock,
            patch("stress.service.httpx.post", return_value=response) as post_mock,
        ):
            result = StressService.select_method(case)

        self.assertEqual(
            result,
            {
                "crypto": "USDT",
                "chain": "ethereum-local",
            },
        )
        get_mock.assert_not_called()
        choice_mock.assert_called_once()
        payload = json.loads(post_mock.call_args.kwargs["content"])
        self.assertEqual(
            payload,
            {
                "crypto": "USDT",
                "chain": "ethereum-local",
            },
        )

    def test_prepare_creates_only_pay_cases(self):
        stress = StressRun(
            id=23,
            count=5,
            status=StressRunStatus.PREPARING,
        )
        stress.save = Mock()

        created_project = Project(pk=99)
        bulk_create_mock = Mock()

        with (
            patch(
                "stress.service.Project.objects.create", return_value=created_project
            ),
            patch("stress.service._setup_differ_recipient_addresses"),
            patch(
                "stress.service.InvoiceStressCase.objects.bulk_create", bulk_create_mock
            ),
            patch("stress.service.random.random", return_value=0.9),
            patch("stress.service.random.gauss", return_value=0.0),
            patch("stress.service.random.shuffle"),
        ):
            StressService.prepare(stress)

        created_cases = bulk_create_mock.call_args.args[0]
        self.assertEqual(len(created_cases), 5)
        self.assertTrue(all(not hasattr(case, "scenario") for case in created_cases))

    def test_prepare_funds_vault_when_invoice_cases_include_contract_billing(self):
        from invoices.models import InvoiceBillingMode

        stress = StressRun(
            id=23,
            count=1,
            status=StressRunStatus.PREPARING,
        )
        stress.save = Mock()

        created_project = Project(pk=99)

        with (
            patch(
                "stress.service.Project.objects.create", return_value=created_project
            ),
            patch("stress.service._setup_differ_recipient_addresses"),
            patch("stress.service._setup_wallet_for_withdrawal") as setup_wallet_mock,
            patch("stress.service._fund_vault_for_withdrawal") as fund_vault_mock,
            patch(
                "stress.service.InvoiceStressCase.objects.bulk_create"
            ) as bulk_create_mock,
            patch("stress.service.random.random", return_value=0.1),
            patch("stress.service.random.gauss", return_value=0.0),
            patch("stress.service.random.shuffle"),
        ):
            StressService.prepare(stress)

        setup_wallet_mock.assert_called_once_with(created_project)
        fund_vault_mock.assert_called_once_with(created_project)
        created_cases = bulk_create_mock.call_args.args[0]
        self.assertEqual(created_cases[0].billing_mode, InvoiceBillingMode.CONTRACT)

    def test_prepare_seeds_full_saas_permission_cache_for_created_project(self):
        stress = StressRun(
            id=23,
            count=1,
            status=StressRunStatus.PREPARING,
        )
        stress.save = Mock()

        created_project = Project(pk=99, appid="XC-STRESS")
        expected_perm = {
            "appid": "XC-STRESS",
            "frozen": False,
            "enable_deposit_withdrawal": True,
        }

        with (
            patch(
                "stress.service.Project.objects.create", return_value=created_project
            ),
            patch("stress.service._setup_differ_recipient_addresses"),
            patch("stress.service.cache", create=True) as cache_mock,
            patch("stress.service.InvoiceStressCase.objects.bulk_create"),
            patch("stress.service.random.random", return_value=0.9),
            patch("stress.service.random.gauss", return_value=0.0),
            patch("stress.service.random.shuffle"),
        ):
            StressService.prepare(stress)

        cache_mock.set.assert_any_call(
            "saas:permission:XC-STRESS", expected_perm, 86400
        )
        cache_mock.set.assert_any_call(
            "saas:permission:XC-STRESS:stale", expected_perm, 86400
        )

    def test_prepare_raises_when_recipient_setup_fails(self):
        stress = StressRun(
            id=23,
            count=5,
            status=StressRunStatus.PREPARING,
        )
        stress.save = Mock()

        with (
            patch("stress.service.Project.objects.create", return_value=Project(pk=99)),
            patch(
                "stress.service._setup_differ_recipient_addresses",
                side_effect=RuntimeError("recipient setup failed"),
            ),
            patch("stress.service.cache", create=True) as cache_mock,
            patch(
                "stress.service.InvoiceStressCase.objects.bulk_create"
            ) as bulk_create_mock,
            self.assertRaisesMessage(RuntimeError, "recipient setup failed"),
        ):
            StressService.prepare(stress)

        bulk_create_mock.assert_not_called()
        cache_mock.set.assert_not_called()

    def test_build_deposit_cases_uses_decimal_sampling_without_float_uniform(self):
        stress = StressRun(
            id=24,
            deposit_count=1,
            deposit_customer_count=1,
        )

        with (
            patch("stress.service.random.gauss", return_value=0.0),
            patch(
                "stress.service.random.choice",
                return_value=("ETH", "ethereum-local"),
            ),
            patch("stress.service.random.randint", return_value=1234567),
            patch("stress.service.random.shuffle"),
            patch(
                "stress.service.random.uniform",
                side_effect=AssertionError("不应走 float uniform 路径"),
            ),
        ):
            cases = _build_deposit_cases(stress)

        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0].amount, Decimal("0.01234567"))

    def test_execute_dispatches_payment_task_after_api_phase(self):
        """_execute 完成 API 阶段后，状态停在 CREATED，并以 countdown=2 派发链上支付 task。

        原本 _execute 内部 `time.sleep(2)` 会阻塞 worker 线程；改造后通过
        Celery countdown 让出线程，这里验证调度参数与最终状态。
        """
        case = SimpleNamespace(
            pk=1,
            status=InvoiceStressCaseStatus.CREATING,
            invoice_sys_no="",
            invoice_out_no="",
            crypto="",
            chain="",
            pay_address="",
            pay_amount=None,
            tx_hash="",
            payer_address="",
        )
        case.save = Mock()

        with (
            patch(
                "stress.tasks.StressService.create_invoice",
                return_value={"sys_no": "INV-1", "out_no": "OUT-1"},
            ),
            patch(
                "stress.tasks.StressService.select_method",
                return_value={
                    "crypto": "ETH",
                    "chain": "ethereum-local",
                    "pay_address": "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
                    "pay_amount": "1.23",
                },
            ),
            patch("stress.tasks._do_payment") as do_payment_mock,
            patch(
                "stress.tasks.execute_stress_case_payment.apply_async"
            ) as payment_dispatch_mock,
            patch(
                "stress.tasks.check_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
            patch("stress.tasks.StressService.on_case_finished"),
        ):
            _execute(case)

        # _execute 不再直接发起链上支付，而是把链上支付派发给独立 task
        do_payment_mock.assert_not_called()
        webhook_dispatch_mock.assert_not_called()
        payment_dispatch_mock.assert_called_once_with(args=[case.pk], countdown=2)
        # 状态停留在 CREATED，等待 payment task 推进
        self.assertEqual(case.status, InvoiceStressCaseStatus.CREATED)

    def test_send_native_uses_pending_nonce(self):
        payer = Mock()
        payer.address = "0x2000000000000000000000000000000000000002"
        payer.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"raw")

        eth_api = Mock()
        eth_api.gas_price = 1
        eth_api.chain_id = 31337
        eth_api.get_transaction_count.return_value = 7
        eth_api.send_raw_transaction.return_value = HexBytes(
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )
        w3 = Mock()
        w3.eth = eth_api
        w3.eth.account.create.return_value = payer
        w3.provider = Mock()
        w3.provider.make_request.return_value = {"result": True}

        with patch("stress.evm._get_w3", return_value=w3):
            send_native(
                to="0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
                amount=Decimal("1.23"),
            )

        eth_api.get_transaction_count.assert_called_once_with(
            payer.address,
            "pending",
        )
        w3.provider.make_request.assert_called_once_with("anvil_setBalance", [ANY, ANY])

    def test_send_native_uses_dedicated_payer_account(self):
        payer = Mock()
        payer.address = "0x2000000000000000000000000000000000000002"
        payer.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"raw")

        eth_api = Mock()
        eth_api.gas_price = 1
        eth_api.chain_id = 31337
        eth_api.send_raw_transaction.return_value = HexBytes(
            "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        )
        w3 = Mock()
        w3.eth = eth_api
        w3.eth.account.create.return_value = payer
        w3.provider = Mock()
        w3.provider.make_request.return_value = {"result": True}

        with patch("stress.evm._get_w3", return_value=w3):
            result = send_native(
                to="0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
                amount=Decimal("1.23"),
            )

        w3.eth.account.create.assert_called_once_with()
        w3.provider.make_request.assert_called_once_with("anvil_setBalance", [ANY, ANY])
        self.assertEqual(result["payer_address"], payer.address)
        payment_tx = payer.sign_transaction.call_args.args[0]
        self.assertEqual(payment_tx["from"], payer.address)

    def test_send_erc20_uses_dedicated_payer_account(self):
        payer = Mock()
        payer.address = "0x2000000000000000000000000000000000000002"
        payer.sign_transaction.return_value = SimpleNamespace(raw_transaction=b"payer")

        contract = Mock()
        contract.functions.mint.return_value.build_transaction.side_effect = lambda tx: (
            tx
        )
        contract.functions.transfer.return_value.build_transaction.side_effect = (
            lambda tx: tx
        )

        eth_api = Mock()
        eth_api.gas_price = 1
        eth_api.chain_id = 31337
        eth_api.get_transaction_count.side_effect = [0, 1]
        eth_api.send_raw_transaction.side_effect = [
            HexBytes(
                "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            ),
            HexBytes(
                "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
            ),
        ]
        eth_api.contract.return_value = contract

        w3 = Mock()
        w3.eth = eth_api
        w3.eth.account.create.return_value = payer
        w3.provider = Mock()
        w3.provider.make_request.return_value = {"result": True}

        with (
            patch("stress.evm._get_w3", return_value=w3),
            patch(
                "stress.evm._require_contract",
                return_value="0x3000000000000000000000000000000000000003",
            ),
        ):
            result = send_erc20(
                token_address="0x3000000000000000000000000000000000000003",
                to="0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
                amount=Decimal("5"),
                decimals=6,
            )

        w3.eth.account.create.assert_called_once_with()
        w3.provider.make_request.assert_called_once_with("anvil_setBalance", [ANY, ANY])
        contract.functions.mint.assert_called_once_with(payer.address, 5_000_000)
        transfer_tx = payer.sign_transaction.call_args.args[0]
        self.assertEqual(transfer_tx["from"], payer.address)
        self.assertEqual(result["payer_address"], payer.address)

    def test_send_erc20_requires_existing_contract(self):
        w3 = Mock()
        w3.eth.get_code.return_value = b""

        with self.assertRaisesMessage(
            ValueError,
            "本地 ERC20 合约不存在，请先初始化本地链配置",
        ):
            from stress.evm import _require_contract

            _require_contract(
                w3,
                "0x3000000000000000000000000000000000000003",
            )

    def test_simulate_payment_dispatches_to_evm_native_sender(self):
        native_coin = Mock()
        native_coin.symbol = "ETH"
        native_coin.get_decimals.return_value = 18
        chain_obj = SimpleNamespace(type=ChainType.EVM, native_coin=native_coin)

        with (
            patch("stress.payment.Chain.objects.get", return_value=chain_obj),
            patch("stress.payment.Crypto.objects.get", return_value=native_coin),
            patch(
                "stress.payment.send_native",
                return_value={
                    "tx_hash": "0xnative",
                    "payer_address": "0xpayer",
                },
            ) as send_native_mock,
        ):
            result = simulate_payment(
                to_address="0xtarget",
                chain_code="ethereum-local",
                crypto_symbol="ETH",
                amount=Decimal("1.5"),
                payment_ref="case-2",
            )

        self.assertEqual(result["tx_hash"], "0xnative")
        send_native_mock.assert_called_once_with(
            to="0xtarget",
            amount=Decimal("1.5"),
            decimals=18,
        )

    def test_simulate_payment_dispatches_native_symbol_token_to_erc20_sender(self):
        native_coin = SimpleNamespace(symbol="ETH")
        chain_obj = SimpleNamespace(type=ChainType.EVM, native_coin=native_coin)
        crypto_obj = Mock()
        crypto_obj.is_native = True
        crypto_obj.get_decimals.return_value = 6
        crypto_obj.address.return_value = "0xtoken"

        with (
            patch("stress.payment.Chain.objects.get", return_value=chain_obj),
            patch("stress.payment.Crypto.objects.get", return_value=crypto_obj),
            patch("stress.payment.send_native") as send_native_mock,
            patch(
                "stress.payment.send_erc20",
                return_value={
                    "tx_hash": "0xerc20",
                    "payer_address": "0xpayer",
                },
            ) as send_erc20_mock,
        ):
            result = simulate_payment(
                to_address="0xtarget",
                chain_code="ethereum-local",
                crypto_symbol="BSC",
                amount=Decimal("25"),
                payment_ref="case-native-like",
            )

        self.assertEqual(result["tx_hash"], "0xerc20")
        send_native_mock.assert_not_called()
        send_erc20_mock.assert_called_once_with(
            token_address="0xtoken",
            to="0xtarget",
            amount=Decimal("25"),
            decimals=6,
        )

    def test_simulate_payment_dispatches_to_evm_erc20_sender(self):
        native_coin = SimpleNamespace(symbol="ETH")
        chain_obj = SimpleNamespace(type=ChainType.EVM, native_coin=native_coin)
        crypto_obj = Mock()
        crypto_obj.is_native = False
        crypto_obj.get_decimals.return_value = 6
        crypto_obj.address.return_value = "0xtoken"

        with (
            patch("stress.payment.Chain.objects.get", return_value=chain_obj),
            patch("stress.payment.Crypto.objects.get", return_value=crypto_obj),
            patch(
                "stress.payment.send_erc20",
                return_value={
                    "tx_hash": "0xerc20",
                    "payer_address": "0xpayer",
                },
            ) as send_erc20_mock,
        ):
            result = simulate_payment(
                to_address="0xtarget",
                chain_code="ethereum-local",
                crypto_symbol="USDT",
                amount=Decimal("25"),
                payment_ref="case-3",
            )

        self.assertEqual(result["tx_hash"], "0xerc20")
        send_erc20_mock.assert_called_once_with(
            token_address="0xtoken",
            to="0xtarget",
            amount=Decimal("25"),
            decimals=6,
        )


class StressPaymentTaskTests(SimpleTestCase):
    """链上支付阶段拆 task 后的 4 项关键行为单测（Invoice 流程）。

    新 execute_stress_case_payment task 接管原 _execute 的"阶段 3"，
    需要验证：状态守卫、成功路径、失败路径，以及 _execute 调度参数。
    """

    databases = {"default"}

    def _make_invoice_case(self, **overrides):
        case = SimpleNamespace(
            pk=42,
            status=InvoiceStressCaseStatus.CREATED,
            crypto="ETH",
            chain="ethereum-local",
            pay_address="0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
            pay_amount=Decimal("1.23"),
            tx_hash="",
            payer_address="",
        )
        for key, value in overrides.items():
            setattr(case, key, value)
        case.save = Mock()
        return case

    def _patch_invoice_get(self, case):
        return patch(
            "stress.tasks.InvoiceStressCase.objects.select_related",
            return_value=SimpleNamespace(get=Mock(return_value=case)),
        )

    def test_execute_stress_case_payment_skips_when_status_not_created(self):
        """非 CREATED 状态（重复派发、SKIPPED、FAILED 等）必须直接 noop。"""
        case = self._make_invoice_case(status=InvoiceStressCaseStatus.SKIPPED)

        with (
            self._patch_invoice_get(case),
            patch("stress.tasks.simulate_payment") as simulate_mock,
            patch(
                "stress.tasks.check_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
        ):
            execute_stress_case_payment.run(case.pk)

        simulate_mock.assert_not_called()
        webhook_dispatch_mock.assert_not_called()
        case.save.assert_not_called()
        # 状态保持原样
        self.assertEqual(case.status, InvoiceStressCaseStatus.SKIPPED)

    def test_execute_stress_case_payment_success_path(self):
        """CREATED → PAYING → PAID，写入 chain_paid_at / tx_hash / payer_address，
        并派发 webhook 超时检查。"""
        case = self._make_invoice_case()

        with (
            self._patch_invoice_get(case),
            patch(
                "stress.tasks._do_payment",
                return_value={
                    "tx_hash": "0xabc",
                    "payer_address": "0x2000000000000000000000000000000000000002",
                },
            ) as do_payment_mock,
            patch(
                "stress.tasks.check_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
            patch("stress.tasks.StressService.on_case_finished") as on_finished_mock,
        ):
            execute_stress_case_payment.run(case.pk)

        do_payment_mock.assert_called_once_with(case)
        self.assertEqual(case.status, InvoiceStressCaseStatus.PAID)
        self.assertEqual(case.tx_hash, "0xabc")
        self.assertEqual(
            case.payer_address, "0x2000000000000000000000000000000000000002"
        )
        self.assertIsNotNone(case.chain_paid_at)
        webhook_dispatch_mock.assert_called_once()
        # 成功路径不应触发 on_case_finished（由 webhook / timeout 推进）
        on_finished_mock.assert_not_called()

    def test_execute_stress_case_payment_keeps_webhook_ok_when_webhook_arrives_early(
        self,
    ):
        """链上支付期间 webhook 可能先到，payment task 不得把 WEBHOOK_OK 回退为 PAID。"""
        case = self._make_invoice_case()

        def mark_webhook_ok(_case):
            case.status = InvoiceStressCaseStatus.WEBHOOK_OK
            return {
                "tx_hash": "0xabc",
                "payer_address": "0x2000000000000000000000000000000000000002",
            }

        with (
            self._patch_invoice_get(case),
            patch("stress.tasks._do_payment", side_effect=mark_webhook_ok),
            patch(
                "stress.tasks.check_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
            patch("stress.tasks.StressService.on_case_finished") as on_finished_mock,
        ):
            execute_stress_case_payment.run(case.pk)

        self.assertEqual(case.status, InvoiceStressCaseStatus.WEBHOOK_OK)
        self.assertEqual(case.tx_hash, "0xabc")
        self.assertEqual(
            case.payer_address, "0x2000000000000000000000000000000000000002"
        )
        self.assertIsNotNone(case.chain_paid_at)
        webhook_dispatch_mock.assert_not_called()
        on_finished_mock.assert_not_called()

    def test_execute_stress_case_payment_failure_path(self):
        """_do_payment raise → case 标 FAILED，调用 on_case_finished。"""
        case = self._make_invoice_case()

        with (
            self._patch_invoice_get(case),
            patch(
                "stress.tasks._do_payment",
                side_effect=RuntimeError("rpc down"),
            ),
            patch(
                "stress.tasks.check_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
            patch("stress.tasks.StressService.on_case_finished") as on_finished_mock,
        ):
            execute_stress_case_payment.run(case.pk)

        self.assertEqual(case.status, InvoiceStressCaseStatus.FAILED)
        self.assertEqual(case.error, "rpc down")
        self.assertIsNotNone(case.finished_at)
        webhook_dispatch_mock.assert_not_called()
        on_finished_mock.assert_called_once_with(case)

    # ── Deposit 流程 ────────────────────────────────────────────

    def _make_deposit_case(self, **overrides):
        case = SimpleNamespace(
            pk=77,
            status=DepositStressCaseStatus.CREATING,
            crypto="USDT",
            chain="ethereum-local",
            deposit_address="0xdepositaddr",
            amount=Decimal("0.01"),
            tx_hash="",
            payer_address="",
        )
        for key, value in overrides.items():
            setattr(case, key, value)
        case.save = Mock()
        return case

    def _patch_deposit_get(self, case):
        return patch(
            "stress.tasks.DepositStressCase.objects.select_related",
            return_value=SimpleNamespace(get=Mock(return_value=case)),
        )

    def test_execute_deposit_dispatches_payment_task_after_api_phase(self):
        """_execute_deposit 完成 API 阶段后保持 status=CREATING，并 countdown=2 调度 payment。"""
        case = self._make_deposit_case(deposit_address="")

        with (
            patch(
                "stress.tasks.StressService.get_deposit_address",
                return_value="0xdepositaddr",
            ),
            patch(
                "stress.tasks.execute_deposit_case_payment.apply_async"
            ) as payment_dispatch_mock,
            patch("stress.tasks.simulate_payment") as simulate_mock,
            patch(
                "stress.tasks.check_deposit_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
        ):
            _execute_deposit(case)

        # _execute_deposit 不再直接发起链上充值
        simulate_mock.assert_not_called()
        webhook_dispatch_mock.assert_not_called()
        payment_dispatch_mock.assert_called_once_with(args=[case.pk], countdown=2)
        # 状态停在 CREATING，等待 payment task 推进
        self.assertEqual(case.status, DepositStressCaseStatus.CREATING)
        self.assertEqual(case.deposit_address, "0xdepositaddr")

    def test_execute_deposit_case_payment_skips_when_status_not_creating(self):
        """非 CREATING 状态必须直接 noop。"""
        case = self._make_deposit_case(status=DepositStressCaseStatus.SKIPPED)

        with (
            self._patch_deposit_get(case),
            patch("stress.tasks.simulate_payment") as simulate_mock,
            patch(
                "stress.tasks.check_deposit_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
        ):
            execute_deposit_case_payment.run(case.pk)

        simulate_mock.assert_not_called()
        webhook_dispatch_mock.assert_not_called()
        case.save.assert_not_called()
        self.assertEqual(case.status, DepositStressCaseStatus.SKIPPED)

    def test_execute_deposit_case_payment_success_path(self):
        """CREATING → PAYING → PAID，写入 chain_paid_at / tx_hash / payer_address，
        并派发 webhook 超时检查。"""
        case = self._make_deposit_case()

        with (
            self._patch_deposit_get(case),
            patch(
                "stress.tasks.simulate_payment",
                return_value={
                    "tx_hash": "0xdef",
                    "payer_address": "0x3000000000000000000000000000000000000003",
                },
            ) as simulate_mock,
            patch(
                "stress.tasks.check_deposit_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
            patch("stress.tasks.StressService.on_case_finished") as on_finished_mock,
        ):
            execute_deposit_case_payment.run(case.pk)

        simulate_mock.assert_called_once_with(
            to_address="0xdepositaddr",
            chain_code="ethereum-local",
            crypto_symbol="USDT",
            amount=Decimal("0.01"),
            payment_ref=f"deposit-{case.pk}",
        )
        self.assertEqual(case.status, DepositStressCaseStatus.PAID)
        self.assertEqual(case.tx_hash, "0xdef")
        self.assertEqual(
            case.payer_address, "0x3000000000000000000000000000000000000003"
        )
        self.assertIsNotNone(case.chain_paid_at)
        webhook_dispatch_mock.assert_called_once()
        on_finished_mock.assert_not_called()

    def test_execute_deposit_case_payment_failure_path(self):
        """simulate_payment raise → case 标 FAILED，调用 on_case_finished。"""
        case = self._make_deposit_case()

        with (
            self._patch_deposit_get(case),
            patch(
                "stress.tasks.simulate_payment",
                side_effect=RuntimeError("chain unavailable"),
            ),
            patch(
                "stress.tasks.check_deposit_webhook_timeout.apply_async"
            ) as webhook_dispatch_mock,
            patch("stress.tasks.StressService.on_case_finished") as on_finished_mock,
        ):
            execute_deposit_case_payment.run(case.pk)

        self.assertEqual(case.status, DepositStressCaseStatus.FAILED)
        self.assertEqual(case.error, "chain unavailable")
        self.assertIsNotNone(case.finished_at)
        webhook_dispatch_mock.assert_not_called()
        on_finished_mock.assert_called_once_with(case)


class StressRecipientSetupTests(TestCase):
    def setUp(self):
        self.eth, _ = Crypto.objects.update_or_create(
            symbol="ETH",
            defaults={
                "name": "Ethereum",
                "coingecko_id": "ethereum",
            },
        )
        self.usdt, _ = Crypto.objects.update_or_create(
            symbol="USDT",
            defaults={
                "name": "Tether USD",
                "decimals": 6,
                "coingecko_id": "tether",
                "prices": {"USD": "1"},
            },
        )
        self.ethereum_local, _ = Chain.objects.update_or_create(
            code="ethereum-local",
            defaults={
                "name": "Ethereum Local",
                "type": ChainType.EVM,
                "native_coin": self.eth,
                "chain_id": 31337,
                "rpc": "http://127.0.0.1:8545",
                "active": True,
            },
        )
        ChainToken.objects.update_or_create(
            chain=self.ethereum_local,
            crypto=self.eth,
            defaults={"address": "", "decimals": None},
        )
        ChainToken.objects.update_or_create(
            chain=self.ethereum_local,
            crypto=self.usdt,
            defaults={
                "address": "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
                "decimals": 6,
            },
        )
        Project.objects.filter(name="Stress Target Project").delete()
        self.project = Project.objects.create(
            name="Stress Target Project",
            wallet=Wallet.objects.create(),
            webhook="http://localhost/stress/webhook",
            ip_white_list="*",
            active=True,
        )

    def test_setup_differ_recipient_addresses_creates_local_recipients_without_templates(
        self,
    ):
        _setup_differ_recipient_addresses(self.project)

        recipients = list(
            DifferRecipientAddress.objects.filter(project=self.project)
            .order_by("chain_type", "address")
            .values("chain_type", "address")
        )

        self.assertEqual(
            recipients,
            [
                {
                    "chain_type": ChainType.EVM.value,
                    "address": _ANVIL_RECIPIENT_ADDRESSES[0],
                },
            ],
        )


class FinalizeStressTimeoutTests(TestCase):
    def setUp(self):
        self.stress_run = StressRun.objects.create(
            name="timeout-test",
            count=5,
            status=StressRunStatus.RUNNING,
        )
        # 2 个终态 case + 3 个非终态 case
        InvoiceStressCase.objects.create(
            stress_run=self.stress_run,
            sequence=1,
            scheduled_offset=0,
            status=InvoiceStressCaseStatus.SUCCEEDED,
        )
        InvoiceStressCase.objects.create(
            stress_run=self.stress_run,
            sequence=2,
            scheduled_offset=1,
            status=InvoiceStressCaseStatus.FAILED,
            error="connection refused",
        )
        InvoiceStressCase.objects.create(
            stress_run=self.stress_run,
            sequence=3,
            scheduled_offset=2,
            status=InvoiceStressCaseStatus.PENDING,
        )
        InvoiceStressCase.objects.create(
            stress_run=self.stress_run,
            sequence=4,
            scheduled_offset=3,
            status=InvoiceStressCaseStatus.CREATING,
        )
        InvoiceStressCase.objects.create(
            stress_run=self.stress_run,
            sequence=5,
            scheduled_offset=4,
            status=InvoiceStressCaseStatus.PAID,
        )
        self.stress_run.succeeded = 1
        self.stress_run.failed = 1
        self.stress_run.save(update_fields=["succeeded", "failed"])

    def test_skips_non_terminal_cases_and_completes_run(self):
        from stress.tasks import finalize_stress_timeout

        finalize_stress_timeout.run(self.stress_run.pk)

        self.stress_run.refresh_from_db()
        self.assertEqual(self.stress_run.status, StressRunStatus.COMPLETED)
        self.assertEqual(self.stress_run.succeeded, 1)
        self.assertEqual(self.stress_run.failed, 1)
        self.assertEqual(self.stress_run.skipped, 3)
        self.assertIsNotNone(self.stress_run.finished_at)

        # 原终态 case 不受影响
        case1 = InvoiceStressCase.objects.get(stress_run=self.stress_run, sequence=1)
        self.assertEqual(case1.status, InvoiceStressCaseStatus.SUCCEEDED)
        case2 = InvoiceStressCase.objects.get(stress_run=self.stress_run, sequence=2)
        self.assertEqual(case2.status, InvoiceStressCaseStatus.FAILED)
        self.assertEqual(case2.error, "connection refused")

        # 非终态 case 被标记为 skipped
        for seq in (3, 4, 5):
            case = InvoiceStressCase.objects.get(
                stress_run=self.stress_run, sequence=seq
            )
            self.assertEqual(case.status, InvoiceStressCaseStatus.SKIPPED)
            self.assertEqual(case.error, "压测整轮超时，任务未执行")
            self.assertIsNotNone(case.finished_at)

    def test_noop_when_already_completed(self):
        from stress.tasks import finalize_stress_timeout

        self.stress_run.status = StressRunStatus.COMPLETED
        self.stress_run.save(update_fields=["status"])

        finalize_stress_timeout.run(self.stress_run.pk)

        self.stress_run.refresh_from_db()
        # skipped 不变，说明没有被二次处理
        self.assertEqual(self.stress_run.skipped, 0)

    def test_noop_when_all_cases_already_terminal(self):
        from stress.tasks import finalize_stress_timeout

        # 把所有非终态 case 手动设为终态
        InvoiceStressCase.objects.filter(
            stress_run=self.stress_run,
            status__in=[
                InvoiceStressCaseStatus.PENDING,
                InvoiceStressCaseStatus.CREATING,
                InvoiceStressCaseStatus.PAID,
            ],
        ).update(status=InvoiceStressCaseStatus.FAILED)

        finalize_stress_timeout.run(self.stress_run.pk)

        self.stress_run.refresh_from_db()
        # 没有 case 被 skip，状态仍为 running（由 on_case_finished 负责推进）
        self.assertEqual(self.stress_run.status, StressRunStatus.RUNNING)
        self.assertEqual(self.stress_run.skipped, 0)


class StressTaskTests(TestCase):
    def test_prepare_stress_marks_run_failed_when_prepare_raises(self):
        stress = StressRun.objects.create(
            name="prepare-failure",
            count=5,
            status=StressRunStatus.PREPARING,
        )

        with patch(
            "stress.tasks.StressService.prepare",
            side_effect=RuntimeError("prepare exploded"),
        ):
            prepare_stress(stress.pk)

        stress.refresh_from_db()
        self.assertEqual(stress.status, StressRunStatus.FAILED)
        self.assertEqual(stress.error, "prepare exploded")
        self.assertIsNotNone(stress.finished_at)


class StressWebhookTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.project = Project.objects.create(
            name="Stress Webhook Project",
            wallet=Wallet.objects.create(),
            webhook="http://localhost:8000/stress/webhook",
            ip_white_list="*",
            active=True,
            hmac_key="stress-secret-key",
        )
        self.stress_run = StressRun.objects.create(
            name="stress-webhook",
            count=1,
            status=StressRunStatus.RUNNING,
            project=self.project,
        )
        self.case = InvoiceStressCase.objects.create(
            stress_run=self.stress_run,
            sequence=1,
            scheduled_offset=0,
            invoice_sys_no="INV-STRESS-1",
            invoice_out_no="STRESS-1-1",
            status=InvoiceStressCaseStatus.PAID,
        )

    def test_handle_webhook_accepts_actual_invoice_payload_without_status_field(self):
        payload = {
            "type": "invoice",
            "data": {
                "sys_no": self.case.invoice_sys_no,
                "out_no": self.case.invoice_out_no,
                "crypto": "ETH",
                "chain": "ethereum-local",
                "pay_address": "0x9965507D1a55bcC2695C58ba16FB37d819B0A4dc",
                "pay_amount": "1.23",
                "hash": "0xa04a8394076c7f7ad4a974fc462ba2a0e08e83c820f99bbe1ea7c8f3da6e7f52",
                "block": 1,
                "confirmed": True,
            },
        }
        body = json.dumps(payload)
        nonce = "nonce-1"
        timestamp = "1710000000"
        signature = hmac.new(
            self.project.hmac_key.encode(),
            f"{nonce}{timestamp}{body}".encode(),
            hashlib.sha256,
        ).hexdigest()

        request = self.factory.post(
            "/stress/webhook",
            data=body,
            content_type="application/json",
            HTTP_XC_NONCE=nonce,
            HTTP_XC_TIMESTAMP=timestamp,
            HTTP_XC_SIGNATURE=signature,
        )

        with patch("stress.views.time.time", return_value=int(timestamp)):
            _handle_webhook(request)

        self.case.refresh_from_db()
        self.assertEqual(self.case.status, InvoiceStressCaseStatus.SUCCEEDED)
        self.assertTrue(self.case.webhook_received)


class HandleInvoiceWebhookBillingModeTests(TestCase):
    """webhook 验证通过后按 billing_mode 分流。"""

    def _build_case(self, *, billing_mode):
        from projects.models import Project

        project = Project.objects.create(
            name="stress-wh-test",
            wallet=Wallet.objects.create(),
            webhook="http://localhost/wh",
            ip_white_list="*",
            active=True,
            hmac_key="hmac-key-test",
        )
        stress = StressRun.objects.create(
            name="wh-test",
            count=1,
            project=project,
            status=StressRunStatus.RUNNING,
        )
        return InvoiceStressCase.objects.create(
            stress_run=stress,
            sequence=1,
            scheduled_offset=0,
            invoice_sys_no="INV-WH-1",
            invoice_out_no="OUT-WH-1",
            status=InvoiceStressCaseStatus.PAID,
            billing_mode=billing_mode,
        )

    def _post_webhook(self, *, case):
        from stress.views import stress_webhook_view

        from common.consts import NONCE_HEADER
        from common.consts import SIGNATURE_HEADER
        from common.consts import TIMESTAMP_HEADER

        body = {
            "type": "invoice",
            "data": {
                "sys_no": case.invoice_sys_no,
                "out_no": case.invoice_out_no,
                "confirmed": True,
            },
        }
        body_str = json.dumps(body)
        timestamp = str(int(time.time()))
        nonce = "nonce-1"
        message = f"{nonce}{timestamp}{body_str}"
        signature = hmac.new(
            case.stress_run.project.hmac_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        rf = RequestFactory()
        request = rf.post(
            "/stress/webhook",
            data=body_str,
            content_type="application/json",
        )
        request.META[f"HTTP_{NONCE_HEADER.upper().replace('-', '_')}"] = nonce
        request.META[f"HTTP_{TIMESTAMP_HEADER.upper().replace('-', '_')}"] = timestamp
        request.META[f"HTTP_{SIGNATURE_HEADER.upper().replace('-', '_')}"] = signature
        return stress_webhook_view(request)

    def test_differ_invoice_webhook_ok_marks_succeeded(self):
        from invoices.models import InvoiceBillingMode

        case = self._build_case(billing_mode=InvoiceBillingMode.DIFFER)

        with patch("stress.views.StressService.on_case_finished") as on_finish_mock:
            self._post_webhook(case=case)

        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceStressCaseStatus.SUCCEEDED)
        self.assertIsNotNone(case.finished_at)
        on_finish_mock.assert_called_once()

    def test_contract_invoice_webhook_ok_marks_webhook_ok(self):
        from invoices.models import InvoiceBillingMode

        case = self._build_case(billing_mode=InvoiceBillingMode.CONTRACT)

        with (
            patch("stress.views.StressService.on_case_finished") as on_finish_mock,
            patch(
                "stress.views._maybe_trigger_invoice_collection_verification"
            ) as trigger_mock,
        ):
            self._post_webhook(case=case)

        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceStressCaseStatus.WEBHOOK_OK)
        self.assertIsNone(case.finished_at)
        self.assertTrue(case.webhook_signature_ok)
        on_finish_mock.assert_not_called()
        trigger_mock.assert_called_once_with(case.stress_run_id)

    def test_contract_invoice_webhook_can_arrive_while_payment_task_is_paying(self):
        from invoices.models import InvoiceBillingMode

        case = self._build_case(billing_mode=InvoiceBillingMode.CONTRACT)
        case.status = InvoiceStressCaseStatus.PAYING
        case.save(update_fields=["status"])

        with (
            patch("stress.views.StressService.on_case_finished") as on_finish_mock,
            patch(
                "stress.views._maybe_trigger_invoice_collection_verification"
            ) as trigger_mock,
        ):
            self._post_webhook(case=case)

        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceStressCaseStatus.WEBHOOK_OK)
        self.assertTrue(case.webhook_received)
        self.assertIsNone(case.finished_at)
        on_finish_mock.assert_not_called()
        trigger_mock.assert_called_once_with(case.stress_run_id)

    def test_contract_invoice_webhook_replay_does_not_flip_to_failed(self):
        from invoices.models import InvoiceBillingMode

        case = self._build_case(billing_mode=InvoiceBillingMode.CONTRACT)

        # 首次 webhook：PAID → WEBHOOK_OK
        with (
            patch("stress.views.StressService.on_case_finished"),
            patch("stress.views._maybe_trigger_invoice_collection_verification"),
        ):
            self._post_webhook(case=case)

        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceStressCaseStatus.WEBHOOK_OK)

        # 再次推送同一 webhook（同 nonce）：应被丢弃，状态保持 WEBHOOK_OK
        with (
            patch("stress.views.StressService.on_case_finished") as on_finish_mock,
            patch(
                "stress.views._maybe_trigger_invoice_collection_verification"
            ) as trigger_mock,
        ):
            self._post_webhook(case=case)

        case.refresh_from_db()
        self.assertEqual(case.status, InvoiceStressCaseStatus.WEBHOOK_OK)
        self.assertIsNone(case.finished_at)
        on_finish_mock.assert_not_called()
        trigger_mock.assert_not_called()
