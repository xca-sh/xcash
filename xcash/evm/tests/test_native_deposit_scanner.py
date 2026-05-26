from decimal import Decimal
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3

from chains.models import Transfer
from chains.models import TransferStatus
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from evm.models import EvmScanCursor
from evm.scanner.logs import EvmLogScanner
from evm.scanner.watchers import EvmWatchSet
from evm.tests._fixtures import make_crypto
from evm.tests._fixtures import make_evm_chain
from evm.tests._fixtures import make_evm_system_address


class EvmNativeDepositScanWindowTests(SimpleTestCase):
    def test_native_compute_scan_window_initial_cursor_starts_from_first_batch(self):
        cursor = EvmScanCursor(last_scanned_block=0)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=2000,
            batch_size=100,
        )

        self.assertEqual(from_block, 1)
        self.assertEqual(to_block, 100)

    def test_native_compute_scan_window_batch_size_is_net_forward_progress(self):
        cursor = EvmScanCursor(last_scanned_block=1000)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=2000,
            batch_size=100,
        )

        self.assertEqual(from_block, 999)
        self.assertEqual(to_block, 1100)


@override_settings(DEBUG=False)
class EvmLogScannerTests(TestCase):
    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.native = make_crypto(symbol="NATIVE-SCAN", name="Native Scanner Coin")
        self.native.decimals = 18
        self.native.save(update_fields=["decimals"])
        self.chain = make_evm_chain(
            code="native-scan",
            chain_id=910101,
            native_coin=self.native,
        )
        self.slot = make_evm_system_address(suffix="aa")
        self.payer = Web3.to_checksum_address("0x" + "bb" * 20)
        self.watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.slot.address}),
            tokens_by_address={},
        )

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @staticmethod
    def _address_topic(address: str) -> str:
        normalized = Web3.to_checksum_address(address)
        return "0x" + "0" * 24 + normalized[2:].lower()

    def _build_native_log(
        self,
        *,
        slot_address: str | None = None,
        payer: str | None = None,
        value: int = 10**18,
        log_index: int = 7,
        block_number: int = 120,
    ) -> dict:
        return {
            "address": slot_address or self.slot.address,
            "topics": [
                Web3.keccak(text="XcashNativeDeposited(address,uint256)"),
                self._address_topic(payer or self.payer),
            ],
            "data": hex(value),
            "blockNumber": block_number,
            "blockHash": bytes.fromhex("22" * 32),
            "logIndex": log_index,
            "transactionHash": bytes.fromhex("cd" * 32),
        }

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    def test_scan_range_creates_native_transfer_from_deposit_event(
        self,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: [self._build_native_log()],
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        logs, _native_observed, _erc20_observed, created, _created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=self.watch_set,
                from_block=120,
                to_block=120,
            )
        )

        transfer = Transfer.objects.get()
        self.assertEqual(len(logs), 1)
        self.assertEqual(created, 1)
        self.assertEqual(transfer.crypto, self.native)
        self.assertEqual(transfer.from_address, self.payer)
        self.assertEqual(transfer.to_address, self.slot.address)
        self.assertEqual(transfer.value, Decimal(10**18))
        self.assertEqual(transfer.amount, Decimal("1"))
        self.assertEqual(transfer.event_id, "native:7")
        self.assertEqual(transfer.hash, "0x" + "cd" * 32)
        self.assertEqual(transfer.block_hash, "0x" + "22" * 32)

    @patch("evm.scanner.observed_transfers.TransferService.create_observed_transfer")
    def test_scan_range_builds_observed_payload_for_native_event(
        self,
        create_observed_transfer_mock,
    ):
        create_observed_transfer_mock.return_value = type(
            "Result",
            (),
            {"created": True},
        )()
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: [self._build_native_log()],
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        EvmLogScanner.scan_range_without_cursor(
            chain=self.chain,
            rpc_client=rpc_client,
            watch_set=self.watch_set,
            from_block=120,
            to_block=120,
        )

        observed = create_observed_transfer_mock.call_args.kwargs["observed"]
        self.assertEqual(observed.crypto, self.native)
        self.assertEqual(observed.from_address, self.payer)
        self.assertEqual(observed.to_address, self.slot.address)
        self.assertEqual(observed.value, Decimal(10**18))
        self.assertEqual(observed.amount, Decimal("1"))
        self.assertEqual(observed.event_id, "native:7")
        self.assertEqual(observed.source, "evm-scan")

    @patch("chains.service.TransferService.create_observed_transfer")
    def test_scan_range_skips_malformed_logs_without_blocking_batch(
        self,
        create_observed_transfer_mock,
    ):
        logs = [
            {**self._build_native_log(), "data": "0xnot-hex"},
            {
                key: value
                for key, value in self._build_native_log().items()
                if key != "transactionHash"
            },
            {**self._build_native_log(), "logIndex": "not-int"},
        ]
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: logs,
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        observed_logs, _native_observed, _erc20_observed, created, _created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=self.watch_set,
                from_block=120,
                to_block=120,
            )
        )

        self.assertEqual(len(observed_logs), 3)
        self.assertEqual(created, 0)
        create_observed_transfer_mock.assert_not_called()

    @patch("chains.service.TransferService.create_observed_transfer")
    def test_scan_range_skips_removed_zero_amount_and_unwatched_slot(
        self,
        create_observed_transfer_mock,
    ):
        unwatched_slot = Web3.to_checksum_address("0x" + "cc" * 20)
        logs = [
            {**self._build_native_log(), "removed": True},
            self._build_native_log(value=0),
            self._build_native_log(slot_address=unwatched_slot),
            {**self._build_native_log(), "topics": [Web3.keccak(text="x")]},
        ]
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: logs,
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        observed_logs, _native_observed, _erc20_observed, created, _created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=self.watch_set,
                from_block=120,
                to_block=120,
            )
        )

        self.assertEqual(len(observed_logs), 4)
        self.assertEqual(created, 0)
        create_observed_transfer_mock.assert_not_called()

    @patch("evm.scanner.logs.load_watch_set")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_advances_cursor_to_latest_when_watch_set_empty(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        load_watch_set_mock,
    ):
        get_latest_block_number_mock.return_value = 200
        load_watch_set_mock.return_value = EvmWatchSet(
            watched_addresses=frozenset(),
            tokens_by_address={},
        )

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32).native

        cursor = EvmScanCursor.objects.get(chain=self.chain)
        self.assertEqual(result.latest_block, 200)
        self.assertEqual(result.observed_logs, 0)
        self.assertEqual(cursor.last_scanned_block, 200)
        get_logs_mock.assert_not_called()

    def test_scan_range_drops_reorged_native_transfer_when_replay_logs_empty(self):
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=120,
            block_hash="0x" + "11" * 32,
            hash="0x" + "cd" * 32,
            event_id="native:7",
            crypto=self.native,
            from_address=self.payer,
            to_address=self.slot.address,
            value=Decimal(10**18),
            amount=Decimal("1"),
            timestamp=1_700_000_000,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
        )
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: [],
                "get_block_hash": lambda *_args, **_kwargs: "0x" + "22" * 32,
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        logs, _native_observed, _erc20_observed, created, _created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=self.watch_set,
                from_block=119,
                to_block=121,
            )
        )

        self.assertEqual(logs, [])
        self.assertEqual(created, 0)
        self.assertFalse(Transfer.objects.filter(pk=old_transfer.pk).exists())

    def test_scan_range_ignores_removed_log_hash_when_dropping_reorged_transfer(self):
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=120,
            block_hash="0x" + "11" * 32,
            hash="0x" + "cd" * 32,
            event_id="native:7",
            crypto=self.native,
            from_address=self.payer,
            to_address=self.slot.address,
            value=Decimal(10**18),
            amount=Decimal("1"),
            timestamp=1_700_000_000,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
        )
        removed_log = self._build_native_log()
        removed_log["removed"] = True
        removed_log["blockHash"] = bytes.fromhex("11" * 32)
        rpc_client = Mock()
        rpc_client.get_logs.return_value = [removed_log]
        rpc_client.get_block_hash.return_value = "0x" + "22" * 32

        logs, _native_observed, _erc20_observed, created, _created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=self.watch_set,
                from_block=119,
                to_block=121,
            )
        )

        self.assertEqual(logs, [removed_log])
        self.assertEqual(created, 0)
        rpc_client.get_block_hash.assert_called_once_with(block_number=120)
        rpc_client.get_block_timestamp.assert_not_called()
        self.assertFalse(Transfer.objects.filter(pk=old_transfer.pk).exists())
