import time
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from tron.client import TronClientError

from chains.constants import ChainCode
from chains.models import Chain
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.runner import ChainLoop
from chains.tests_fixtures import make_evm_chain
from chains.tests_fixtures import make_tron_chain
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from evm.scanner.rpc import EvmScannerRpcError


@override_settings(DEBUG=False, TRON_SCAN_SAFE_LAG_BLOCKS=1)
class ScanLivenessTests(TestCase):
    """真实执行 chain-runner 的单轮扫描和 HTTP 探针，只替换链 RPC 与确认消息的外部发送。

    手工给链写 scan_last_error 只能验证探针查询，覆盖不到预算耗尽、RPC 失败是否真正
    留下失败信号，也覆盖不到扫描异常是否连带跳过已有充值的确认派发。
    """

    def make_scan_case(self, *, tron=False):
        if tron:
            chain = make_tron_chain(latest_block_number=100)
            token = Crypto.objects.create(
                symbol="LIVENESS", name="Liveness token", coingecko_id="liveness-token"
            )
            CryptoOnChain.objects.create(
                chain=chain,
                crypto=token,
                address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
                decimals=6,
            )
        else:
            chain = make_evm_chain(
                code=ChainCode.Anvil,
                latest_block_number=100,
                evm_log_max_block_range=10,
            )
        Chain.objects.filter(pk=chain.pk).update(scan_cursor_block=100)
        Transfer.objects.create(
            chain=chain,
            block=10,
            block_hash="0x" + "11" * 32,
            hash="0x" + "12" * 32,
            crypto=chain.native_coin,
            from_address=(
                "TJRabPrwbZy45sbavfcjinPJC18kjpRTv8" if tron else "0x" + "11" * 20
            ),
            to_address=(
                "TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb" if tron else "0x" + "22" * 20
            ),
            value=1,
            amount=1,
            timestamp=1,
            datetime=timezone.now(),
            processed_at=timezone.now(),
            confirm_mode=ConfirmMode.FULL,
        )
        return chain

    def run_round(self, chain, *, expired_budget=False):
        loop = ChainLoop(chain)
        if not expired_budget:
            loop.run_once()
            return
        with patch("chains.runner.step_deadline", return_value=time.monotonic() - 1):
            loop.run_once()

    def assert_scan_failed(self, *, chain, scanned_block, error):
        chain.refresh_from_db()
        self.assertEqual(chain.scan_cursor_block, scanned_block)
        self.assertIsNotNone(chain.scan_last_error_at)
        self.assertIn(error, chain.scan_last_error)
        self.assertGreaterEqual(chain.last_scanned_at, chain.scan_last_error_at)
        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 503)

    @patch("chains.tasks.enqueue_due_confirmations")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_evm_rpc_failures_keep_confirmation_live_and_probe_recovers(
        self, get_head, get_logs, dispatch
    ):
        chain = self.make_scan_case()
        get_head.side_effect = range(200, 206)
        get_logs.side_effect = EvmScannerRpcError("node down")
        for _ in range(5):
            self.run_round(chain)
        self.assertEqual(dispatch.call_count, 5)
        self.assert_scan_failed(chain=chain, scanned_block=100, error="node down")

        get_logs.side_effect = None
        get_logs.return_value = []
        self.run_round(chain)
        chain.refresh_from_db()
        self.assertGreater(chain.scan_cursor_block, 100)
        self.assertIsNone(chain.scan_last_error_at)
        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 200)

    @patch("chains.tasks.enqueue_due_confirmations")
    @patch("tron.scanner.TronHttpClient")
    def test_tron_rpc_failures_keep_confirmation_live_and_idle_recovery_clears_error(
        self, client_cls, dispatch
    ):
        chain = self.make_scan_case(tron=True)
        client = client_cls.return_value
        client.get_latest_solid_block_number.side_effect = range(200, 205)
        client.get_transaction_infos_by_block.side_effect = TronClientError("down")
        for _ in range(5):
            self.run_round(chain)
        self.assertEqual(dispatch.call_count, 5)
        self.assert_scan_failed(chain=chain, scanned_block=100, error="down")

        # 节点恢复且位点已追平时，没有扫块动作也应该解除上一轮错误。
        Chain.objects.filter(pk=chain.pk).update(scan_cursor_block=203)
        client.get_latest_solid_block_number.side_effect = None
        client.get_latest_solid_block_number.return_value = 204
        self.run_round(chain)
        chain.refresh_from_db()
        self.assertIsNone(chain.scan_last_error_at)
        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 200)
        self.assertEqual(dispatch.call_count, 5)  # 链高未推进，不重复派发。

    @patch("chains.tasks.enqueue_due_confirmations")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs", return_value=[])
    @patch(
        "evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number", return_value=200
    )
    def test_evm_budget_exhaustion_retains_progress_and_reports_failing(
        self, _head, _logs, dispatch
    ):
        chain = self.make_scan_case()
        self.run_round(chain, expired_budget=True)
        dispatch.assert_called_once_with(chain)
        # 窗口从 97 起（回放 4 块），预算耗尽前仍扫完首段 [97, 106]。
        self.assert_scan_failed(
            chain=chain,
            scanned_block=106,
            error="ScanRoundBudgetExceeded",
        )

    @patch("chains.tasks.enqueue_due_confirmations")
    @patch("tron.scanner.TronHttpClient")
    def test_tron_budget_exhaustion_retains_progress_and_reports_failing(
        self, client_cls, dispatch
    ):
        chain = self.make_scan_case(tron=True)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 200
        client.get_transaction_infos_by_block.return_value = []
        self.run_round(chain, expired_budget=True)
        dispatch.assert_called_once_with(chain)
        self.assert_scan_failed(
            chain=chain,
            scanned_block=101,
            error="ScanRoundBudgetExceeded",
        )

    @patch(
        "chains.tasks.enqueue_due_confirmations",
        side_effect=ConnectionError("database down"),
    )
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs", return_value=[])
    @patch(
        "evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number", return_value=120
    )
    def test_confirmation_publish_failure_does_not_abort_scan(
        self, _head, _logs, dispatch
    ):
        chain = self.make_scan_case()
        self.run_round(chain)
        dispatch.assert_called_once_with(chain)
        chain.refresh_from_db()
        self.assertEqual(chain.scan_cursor_block, 119)
