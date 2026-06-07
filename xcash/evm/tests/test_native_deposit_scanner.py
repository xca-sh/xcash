from decimal import Decimal
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import Transfer
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from evm.models import EvmScanCursor
from evm.scanner.logs import EvmLogScanner
from evm.scanner.watchers import EvmWatchSet
from evm.tests._fixtures import make_crypto
from evm.tests._fixtures import make_evm_chain
from evm.tests._fixtures import make_evm_system_address
from projects.models import Customer
from projects.models import Project


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
        self.chain = make_evm_chain(
            code="native-scan",
            chain_id=910101,
            native_coin=self.native,
        )
        self.native = self.chain.native_coin
        self.slot = make_evm_system_address(suffix="aa")
        self.project = Project.objects.create(
            name="Native Scanner Project",
            webhook="https://example.com/webhook",
        )
        self.customer = Customer.objects.create(
            project=self.project,
            uid="native-scanner-customer",
        )
        VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=self.slot.address,
            salt=b"\x01" * 32,
        )
        self.payer = Web3.to_checksum_address("0x" + "bb" * 20)
        self.watch_set = EvmWatchSet(
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
                Web3.keccak(text="XcashNativeReceived(address,uint256)"),
                self._address_topic(payer or self.payer),
            ],
            "data": hex(value),
            "blockNumber": block_number,
            "blockHash": bytes.fromhex("22" * 32),
            "logIndex": log_index,
            "transactionHash": bytes.fromhex("cd" * 32),
        }

    @patch("chains.service.TransferService.enqueue_processing")
    def test_scan_range_creates_native_transfer_from_deposit_event(
        self,
        _enqueue_processing_mock,
    ):
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: [self._build_native_log()],
                "get_transaction": lambda *_args, **_kwargs: {"to": self.slot.address},
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        created = EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            watch_set=self.watch_set,
            from_block=120,
            to_block=120,
        )

        transfer = Transfer.objects.get()
        self.assertIsNone(created)
        self.assertEqual(transfer.crypto, self.native)
        self.assertEqual(transfer.from_address, self.payer)
        self.assertEqual(transfer.to_address, self.slot.address)
        self.assertEqual(transfer.value, Decimal(10**18))
        self.assertEqual(transfer.amount, Decimal("1"))
        self.assertEqual(transfer.hash, "0x" + "cd" * 32)
        self.assertEqual(transfer.block_hash, "0x" + "22" * 32)

    @patch("chains.service.TransferService.enqueue_processing")
    def test_scan_range_skips_native_event_from_system_observed_address(
        self,
        enqueue_processing_mock,
    ):
        system_payer = Web3.to_checksum_address("0x" + "bc" * 20)
        system_customer = Customer.objects.create(
            project=self.project,
            uid="native-scanner-system-customer",
        )
        VaultSlot.objects.create(
            customer=system_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=system_payer,
            salt=b"\x02" * 32,
        )
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: [
                    self._build_native_log(payer=system_payer)
                ],
                "get_transaction": lambda *_args, **_kwargs: {"to": self.slot.address},
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            watch_set=self.watch_set,
            from_block=120,
            to_block=120,
        )

        self.assertFalse(Transfer.objects.exists())
        enqueue_processing_mock.assert_not_called()

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
                "get_transaction": lambda *_args, **_kwargs: {"to": self.slot.address},
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        EvmLogScanner.scan_range(
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
        self.assertEqual(observed.source, "evm-scan")

    @patch("evm.scanner.observed_transfers.logger.warning")
    def test_native_scanner_skips_tx_with_multiple_system_inbound_logs(
        self,
        warning_mock,
    ):
        logs = [
            self._build_native_log(log_index=7),
            self._build_native_log(log_index=8),
        ]
        rpc_client = Mock()
        rpc_client.get_logs.return_value = logs
        rpc_client.get_transaction.return_value = {"to": self.slot.address}
        rpc_client.get_block_timestamp.return_value = 1_700_000_000

        created = EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            watch_set=self.watch_set,
            from_block=120,
            to_block=120,
        )

        self.assertIsNone(created)
        self.assertEqual(Transfer.objects.count(), 0)
        warning_mock.assert_called_with(
            "EVM scanner skipped tx with multiple observed inbound events",
            chain=self.chain.code,
            tx_hash="0x" + "cd" * 32,
            log_count=2,
        )

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
        ]
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **_kwargs: logs,
                "get_transaction": lambda *_args, **_kwargs: {"to": self.slot.address},
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        created = EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            watch_set=self.watch_set,
            from_block=120,
            to_block=120,
        )

        self.assertIsNone(created)
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
                "get_transaction": lambda *_args, **_kwargs: {"to": self.slot.address},
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()

        created = EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            watch_set=self.watch_set,
            from_block=120,
            to_block=120,
        )

        self.assertIsNone(created)
        create_observed_transfer_mock.assert_not_called()

    @patch("evm.scanner.logs.load_watch_set")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_fetches_xcash_logs_without_cached_addresses(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        load_watch_set_mock,
    ):
        get_latest_block_number_mock.return_value = 200
        get_logs_mock.return_value = []
        load_watch_set_mock.return_value = EvmWatchSet(
            matched_addresses=frozenset(),
            tokens_by_address={},
        )

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(chain=self.chain)
        self.assertIsNone(result)
        self.assertEqual(cursor.last_scanned_block, 32)
        get_logs_mock.assert_called_once()
        self.assertIsNone(get_logs_mock.call_args.kwargs["addresses"])
