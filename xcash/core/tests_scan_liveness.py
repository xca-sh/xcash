from unittest.mock import patch

from celery.exceptions import SoftTimeLimitExceeded
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from tron.models import TronWatchCursor
from tron.tasks import scan_tron_chain

from chains.constants import ChainCode
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.tests_fixtures import make_evm_chain
from chains.tests_fixtures import make_tron_chain
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from evm.models import EvmScanCursor
from evm.tasks import _scan_evm_chain


@override_settings(DEBUG=False, TRON_SCAN_SAFE_LAG_BLOCKS=1)
class ScanLivenessTests(TestCase):
    """真实执行扫描任务和 HTTP 探针，只替换链 RPC 与确认消息的外部发送。

    手工给游标写 last_error 只能验证探针查询，覆盖不到软超时是否真正留下失败信号，
    也覆盖不到扫描异常是否连带跳过已有充值的确认派发。
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
            cursor, _ = TronWatchCursor.objects.get_or_create(chain=chain)
            task = scan_tron_chain
        else:
            chain = make_evm_chain(
                code=ChainCode.Anvil,
                latest_block_number=100,
                evm_log_max_block_range=10,
            )
            cursor, _ = EvmScanCursor.objects.get_or_create(chain=chain)
            task = _scan_evm_chain
        type(cursor).objects.filter(pk=cursor.pk).update(last_scanned_block=100)
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
        return chain, cursor, task

    def assert_scan_failed(self, *, chain, cursor, scanned_block):
        cursor.refresh_from_db()
        chain.refresh_from_db()
        self.assertEqual(cursor.last_scanned_block, scanned_block)
        self.assertIsNotNone(cursor.last_error_at)
        self.assertIn("SoftTimeLimitExceeded", cursor.last_error)
        self.assertGreaterEqual(chain.last_scanned_at, cursor.last_error_at)
        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 503)

    @patch("chains.tasks.block_number_updated.delay")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_evm_timeouts_keep_confirmation_live_and_probe_recovers(
        self, get_head, get_logs, dispatch
    ):
        chain, cursor, task = self.make_scan_case()
        get_head.side_effect = range(200, 206)
        get_logs.side_effect = SoftTimeLimitExceeded()
        for _ in range(5):
            with self.assertRaises(SoftTimeLimitExceeded):
                task.run(chain.pk)
        self.assertEqual(dispatch.call_count, 5)
        self.assert_scan_failed(chain=chain, cursor=cursor, scanned_block=100)

        get_logs.side_effect = None
        get_logs.return_value = []
        task.run(chain.pk)
        cursor.refresh_from_db()
        self.assertGreater(cursor.last_scanned_block, 100)
        self.assertIsNone(cursor.last_error_at)
        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 200)

    @patch("chains.tasks.block_number_updated.delay")
    @patch("tron.scanner.TronHttpClient")
    def test_tron_timeouts_keep_confirmation_live_and_idle_recovery_clears_error(
        self, client_cls, dispatch
    ):
        chain, cursor, task = self.make_scan_case(tron=True)
        client = client_cls.return_value
        client.get_latest_solid_block_number.side_effect = range(200, 205)
        client.get_transaction_infos_by_block.side_effect = SoftTimeLimitExceeded()
        for _ in range(5):
            with self.assertRaises(SoftTimeLimitExceeded):
                task.run(chain.pk)
        self.assertEqual(dispatch.call_count, 5)
        self.assert_scan_failed(chain=chain, cursor=cursor, scanned_block=100)

        # 节点恢复且游标已追平时，没有扫块动作也应该解除上一轮错误。
        TronWatchCursor.objects.filter(pk=cursor.pk).update(last_scanned_block=203)
        client.get_latest_solid_block_number.side_effect = None
        client.get_latest_solid_block_number.return_value = 204
        task.run(chain.pk)
        cursor.refresh_from_db()
        self.assertIsNone(cursor.last_error_at)
        self.assertEqual(self.client.get(reverse("health-scanning")).status_code, 200)
        self.assertEqual(dispatch.call_count, 5)  # 链高未推进，不重复派发。

    @patch("chains.tasks.block_number_updated.delay")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_evm_partial_progress_is_retained_on_timeout(
        self, get_head, get_logs, dispatch
    ):
        chain, cursor, task = self.make_scan_case()
        get_head.return_value = 200
        get_logs.side_effect = [[], SoftTimeLimitExceeded()]
        with self.assertRaises(SoftTimeLimitExceeded):
            task.run(chain.pk)
        dispatch.assert_called_once_with(chain.pk)
        self.assert_scan_failed(chain=chain, cursor=cursor, scanned_block=108)

    @patch("chains.tasks.block_number_updated.delay")
    @patch("tron.scanner.TronHttpClient")
    def test_tron_partial_progress_is_retained_on_timeout(self, client_cls, dispatch):
        chain, cursor, task = self.make_scan_case(tron=True)
        client = client_cls.return_value
        client.get_latest_solid_block_number.return_value = 200
        client.get_transaction_infos_by_block.side_effect = [
            [],
            SoftTimeLimitExceeded(),
        ]
        with self.assertRaises(SoftTimeLimitExceeded):
            task.run(chain.pk)
        dispatch.assert_called_once_with(chain.pk)
        self.assert_scan_failed(chain=chain, cursor=cursor, scanned_block=101)

    @patch(
        "chains.tasks.block_number_updated.delay",
        side_effect=ConnectionError("broker down"),
    )
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs", return_value=[])
    @patch(
        "evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number", return_value=120
    )
    def test_confirmation_publish_failure_does_not_abort_scan(
        self, _head, _logs, dispatch
    ):
        chain, cursor, task = self.make_scan_case()
        task.run(chain.pk)
        dispatch.assert_called_once_with(chain.pk)
        cursor.refresh_from_db()
        self.assertEqual(cursor.last_scanned_block, 119)

    @patch(
        "chains.tasks.block_number_updated.delay", side_effect=SoftTimeLimitExceeded()
    )
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch(
        "evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number", return_value=120
    )
    def test_timeout_during_confirmation_publish_still_aborts_scan(
        self, _head, get_logs, _dispatch
    ):
        chain, cursor, task = self.make_scan_case()
        with self.assertRaises(SoftTimeLimitExceeded):
            task.run(chain.pk)
        get_logs.assert_not_called()
        self.assert_scan_failed(chain=chain, cursor=cursor, scanned_block=100)
