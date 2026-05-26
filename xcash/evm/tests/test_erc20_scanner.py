from decimal import Decimal
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from chains.models import Wallet
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.choices import TxKind
from evm.models import DepositSlot
from evm.models import EvmScanCursor
from evm.models import EvmTxTask
from evm.scanner.logs import EvmLogScanner
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import EvmWatchSet
from evm.scanner.watchers import load_watch_set
from evm.tasks import scan_active_evm_chains
from evm.tasks import _scan_evm_chain
from projects.models import Project
from users.models import Customer


class EvmErc20ScanWindowTests(SimpleTestCase):
    def test_erc20_compute_scan_window_initial_cursor_starts_from_first_batch(self):
        cursor = EvmScanCursor(last_scanned_block=0)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=2000,
            batch_size=100,
        )

        self.assertEqual(from_block, 1)
        self.assertEqual(to_block, 100)

    def test_erc20_compute_scan_window_batch_size_is_net_forward_progress(self):
        cursor = EvmScanCursor(last_scanned_block=1000)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=2000,
            batch_size=100,
        )

        self.assertEqual(from_block, 999)
        self.assertEqual(to_block, 1100)

    def test_erc20_compute_scan_window_caps_to_latest_when_near_chain_head(self):
        cursor = EvmScanCursor(last_scanned_block=1990)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=2000,
            batch_size=100,
        )

        self.assertEqual(from_block, 1989)
        self.assertEqual(to_block, 2000)

    def test_erc20_compute_scan_window_returns_empty_when_latest_block_is_zero(self):
        cursor = EvmScanCursor(last_scanned_block=0)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=0,
            batch_size=100,
        )

        self.assertGreater(from_block, to_block)


@override_settings(DEBUG=False)
class EvmErc20ScannerTests(TestCase):
    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.native = Crypto.objects.create(
            name="Scanner BNB",
            symbol="BNB-SCANNER",
            coingecko_id="binancecoin-scanner",
        )
        self.chain = Chain.objects.create(
            code="bsc",
            name="BSC",
            type=ChainType.EVM,
            chain_id=56,
            rpc="http://bsc.local",
            native_coin=self.native,
            confirm_block_count=6,
            active=True,
        )
        self.chain.erc20_transfer_gas = 65_000
        self.token = Crypto.objects.create(
            name="Scanner Tether USD",
            symbol="USDT-SCANNER",
            coingecko_id="tether-scanner",
            decimals=18,
        )
        self.token_deployment = ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000aa"
            ),
            decimals=18,
        )
        self.wallet = Wallet.objects.create()
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bb"
            ),
        )
        self.project = Project.objects.create(
            name="Scanner Project",
            wallet=self.wallet,
            webhook="https://example.com/webhook",
        )
        self.customer = Customer.objects.create(
            project=self.project,
            uid="scanner-customer",
        )
        self.deposit_slot = DepositSlot.objects.create(
            customer=self.customer,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bc"
            ),
            vault_address=self.addr.address,
            salt=b"\x01" * 32,
        )

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @staticmethod
    def _address_topic(address: str) -> str:
        normalized = Web3.to_checksum_address(address)
        return "0x" + "0" * 24 + normalized[2:].lower()

    def _build_transfer_log(
        self,
        *,
        from_address: str,
        to_address: str,
        log_index: int = 5,
        value: int = 10**18,
        block_number: int = 100,
    ) -> dict:
        return {
            "address": self.token_deployment.address,
            "topics": [
                Web3.keccak(text="Transfer(address,address,uint256)"),
                self._address_topic(from_address),
                self._address_topic(to_address),
            ],
            "data": hex(value),
            "blockNumber": block_number,
            "blockHash": bytes.fromhex("10" * 32),
            "logIndex": log_index,
            "transactionHash": bytes.fromhex("ab" * 32),
        }

    def _build_internal_erc20_task(
        self,
        *,
        tx_hash: str,
        recipient: str | None = None,
        value_raw: int = 123_000_000,
    ) -> tuple[TxTask, str]:
        recipient = recipient or Web3.to_checksum_address("0x" + "52" * 20)
        encoded_args = recipient.removeprefix("0x").rjust(64, "0") + hex(value_raw)[
            2:
        ].rjust(64, "0")
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            address=self.addr,
            chain=self.chain,
            nonce=0,
            to=self.token_deployment.address,
            value=0,
            data=f"0xa9059cbb{encoded_args}",
            gas=self.chain.erc20_transfer_gas,
            tx_kind=TxKind.CONTRACT_CALL,
            gas_price=1,
            signed_payload="0x01",
        )
        return base_task, encoded_args

    def _create_scan_dispatch_ignored_chains(self) -> None:
        Chain.objects.create(
            code="bsc-inactive",
            name="BSC Inactive",
            type=ChainType.EVM,
            chain_id=57,
            rpc="http://inactive-bsc.local",
            native_coin=self.native,
            active=False,
        )
        Chain.objects.create(
            code="tron-active",
            name="Tron Active",
            type=ChainType.TRON,
            native_coin=self.native,
            active=True,
        )

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_erc20_first_scan_without_cursor_starts_from_latest_tail_window(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        _get_block_timestamp_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        # 首次创建统一日志游标时从首个批次开始推进。
        get_latest_block_number_mock.return_value = 100
        get_logs_mock.return_value = []

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32).erc20

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertEqual(result.from_block, 1)
        self.assertEqual(result.to_block, 32)
        self.assertEqual(cursor.last_scanned_block, 32)

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_creates_transfer_and_advances_cursor(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        # 命中的 ERC20 Transfer 应落到统一 Transfer 表；首扫会直接对齐链头附近窗口。
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_logs_mock.side_effect = [
            [],
            [
                self._build_transfer_log(
                    from_address=Web3.to_checksum_address(
                        "0x00000000000000000000000000000000000000cc"
                    ),
                    to_address=self.deposit_slot.address,
                ),
            ],
            [],
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32).erc20

        transfer = Transfer.objects.get()
        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )

        self.assertEqual(result.created_transfers, 1)
        self.assertEqual(result.observed_logs, 1)
        self.assertEqual(transfer.event_id, "erc20:5")
        self.assertEqual(transfer.hash, "0x" + "ab" * 32)
        self.assertEqual(
            transfer.to_address, Web3.to_checksum_address(self.deposit_slot.address)
        )
        self.assertEqual(transfer.amount, Decimal("1"))
        self.assertEqual(cursor.last_scanned_block, 32)

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_erc20_replay_drops_unconfirmed_transfer_when_block_hash_changes(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "99" * 32,
            hash="0x" + "ab" * 32,
            event_id="erc20:5",
            crypto=self.token,
            from_address=Web3.to_checksum_address("0x" + "cc" * 20),
            to_address=self.deposit_slot.address,
            value=Decimal(10**18),
            amount=Decimal("1"),
            timestamp=1_700_000_000,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
        )
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_100
        get_logs_mock.return_value = [
            self._build_transfer_log(
                from_address=old_transfer.from_address,
                to_address=old_transfer.to_address,
                block_number=100,
            )
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32).erc20

        replacement = Transfer.objects.get(
            chain=self.chain,
            hash=old_transfer.hash,
            event_id=old_transfer.event_id,
        )
        self.assertEqual(result.created_transfers, 1)
        self.assertNotEqual(replacement.pk, old_transfer.pk)
        self.assertEqual(replacement.block_hash, "0x" + "10" * 32)

    def test_scan_range_drops_reorged_erc20_transfer_when_replay_logs_empty(self):
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "99" * 32,
            hash="0x" + "ab" * 32,
            event_id="erc20:5",
            crypto=self.token,
            from_address=Web3.to_checksum_address("0x" + "cc" * 20),
            to_address=self.deposit_slot.address,
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
                "get_block_hash": lambda *_args, **_kwargs: "0x" + "10" * 32,
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        logs, _native_observed, _erc20_observed, _created_native, created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=watch_set,
                from_block=99,
                to_block=101,
            )
        )

        self.assertEqual(logs, [])
        self.assertEqual(created_erc20, 0)
        self.assertFalse(Transfer.objects.filter(pk=old_transfer.pk).exists())

    @patch("chains.service.TransferService.create_observed_transfer")
    def test_scan_range_skips_malformed_erc20_logs_without_blocking_batch(
        self,
        create_observed_transfer_mock,
    ):
        sender = Web3.to_checksum_address("0x" + "cc" * 20)
        malformed_logs = [
            {
                **self._build_transfer_log(
                    from_address=sender,
                    to_address=self.addr.address,
                ),
                "data": "0xnope",
            },
            {
                key: value
                for key, value in self._build_transfer_log(
                    from_address=sender, to_address=self.addr.address
                ).items()
                if key != "transactionHash"
            },
            {
                **self._build_transfer_log(
                    from_address=sender,
                    to_address=self.addr.address,
                ),
                "logIndex": "not-int",
            },
        ]
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **kwargs: (
                    malformed_logs
                    if kwargs["topic0"]
                    == Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)"))
                    else []
                ),
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        logs, _native_observed, _erc20_observed, _created_native, created_erc20 = (
            EvmLogScanner.scan_range_without_cursor(
                chain=self.chain,
                rpc_client=rpc_client,
                watch_set=watch_set,
                from_block=100,
                to_block=100,
            )
        )

        self.assertEqual(logs, malformed_logs)
        self.assertEqual(created_erc20, 0)
        create_observed_transfer_mock.assert_not_called()

    def test_erc20_scanner_does_not_route_known_internal_hash_to_processor(self):
        tx_hash = "0x" + "51" * 32
        recipient = Web3.to_checksum_address("0x" + "52" * 20)
        wrong_recipient = Web3.to_checksum_address("0x" + "53" * 20)
        value_raw = 123_000_000
        base_task, encoded_args = self._build_internal_erc20_task(
            tx_hash=tx_hash,
            recipient=recipient,
            value_raw=value_raw,
        )
        log = self._build_transfer_log(
            from_address=self.addr.address,
            to_address=wrong_recipient,
            value=value_raw,
            log_index=4,
            block_number=100,
        )
        log["transactionHash"] = bytes.fromhex("51" * 32)
        receipt = {
            "status": 1,
            "blockNumber": 100,
            "blockHash": "0x" + "61" * 32,
            "logs": [log],
        }
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.addr.address,
            "to": self.token_deployment.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        rpc_client.get_block_timestamp.return_value = 1_700_000_000
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address, wrong_recipient}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        with patch(
            "evm.internal_tx.processor.process_internal_transaction"
        ) as processor_mock:
            created = EvmLogScanner._process_logs(
                chain=self.chain,
                logs=[log],
                rpc_client=rpc_client,
                watch_set=watch_set,
                from_block=100,
                to_block=100,
            ).erc20_created

        base_task.refresh_from_db()
        processor_mock.assert_not_called()
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        self.assertEqual(created, 0)
        self.assertFalse(Transfer.objects.filter(hash=tx_hash).exists())
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(base_task.success)

    def test_erc20_scanner_skips_known_internal_hash_even_when_receipt_would_match(
        self,
    ):
        tx_hash = "0x" + "5a" * 32
        recipient = Web3.to_checksum_address("0x" + "5b" * 20)
        value_raw = 123_000_000
        _, encoded_args = self._build_internal_erc20_task(
            tx_hash=tx_hash,
            recipient=recipient,
            value_raw=value_raw,
        )
        log = self._build_transfer_log(
            from_address=self.addr.address,
            to_address=recipient,
            value=value_raw,
            log_index=11,
            block_number=100,
        )
        log["transactionHash"] = bytes.fromhex("5a" * 32)
        receipt = {
            "status": 1,
            "blockNumber": 100,
            "blockHash": "0x" + "61" * 32,
            "logs": [log],
        }
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.addr.address,
            "to": self.token_deployment.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address, recipient}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[log],
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=100,
            to_block=100,
        ).erc20_created

        self.assertEqual(created, 0)
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        self.assertFalse(Transfer.objects.filter(hash=tx_hash).exists())

    def test_erc20_scanner_does_not_require_internal_tx_details(self):
        tx_hash = "0x" + "54" * 32
        self._build_internal_erc20_task(tx_hash=tx_hash)
        log = self._build_transfer_log(
            from_address=self.addr.address,
            to_address=Web3.to_checksum_address("0x" + "55" * 20),
            log_index=7,
        )
        log["transactionHash"] = bytes.fromhex("54" * 32)
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = None
        rpc_client.get_transaction_receipt.return_value = {"status": 1}
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[log],
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=100,
            to_block=100,
        )

        self.assertEqual(result.erc20_created, 0)
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        rpc_client.get_block_timestamp.assert_not_called()
        self.assertEqual(Transfer.objects.count(), 0)

    def test_erc20_scanner_does_not_require_internal_tx_receipt(self):
        tx_hash = "0x" + "56" * 32
        _, encoded_args = self._build_internal_erc20_task(tx_hash=tx_hash)
        log = self._build_transfer_log(
            from_address=self.addr.address,
            to_address=Web3.to_checksum_address("0x" + "57" * 20),
            log_index=8,
        )
        log["transactionHash"] = bytes.fromhex("56" * 32)
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.addr.address,
            "to": self.token_deployment.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = None
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[log],
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=100,
            to_block=100,
        )

        self.assertEqual(result.erc20_created, 0)
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        rpc_client.get_block_timestamp.assert_not_called()
        self.assertEqual(Transfer.objects.count(), 0)

    def test_erc20_scanner_processes_duplicate_internal_hash_once(self):
        tx_hash = "0x" + "58" * 32
        wrong_recipient = Web3.to_checksum_address("0x" + "59" * 20)
        _, encoded_args = self._build_internal_erc20_task(tx_hash=tx_hash)
        first_log = self._build_transfer_log(
            from_address=self.addr.address,
            to_address=wrong_recipient,
            value=123_000_000,
            log_index=9,
        )
        second_log = self._build_transfer_log(
            from_address=self.addr.address,
            to_address=wrong_recipient,
            value=456_000_000,
            log_index=10,
        )
        first_log["transactionHash"] = bytes.fromhex("58" * 32)
        second_log["transactionHash"] = bytes.fromhex("58" * 32)
        receipt = {
            "status": 1,
            "blockNumber": 100,
            "blockHash": "0x" + "61" * 32,
            "logs": [first_log],
        }
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.addr.address,
            "to": self.token_deployment.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.addr.address, wrong_recipient}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[first_log, second_log],
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=100,
            to_block=100,
        ).erc20_created

        self.assertEqual(created, 0)
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        self.assertEqual(Transfer.objects.count(), 0)

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_rewind_window_keeps_transfer_idempotent(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        # 近端重扫会重复看到同一日志，但统一唯一键必须保证不会重复落库。
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        repeated_log = self._build_transfer_log(
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000cc"
            ),
            to_address=self.deposit_slot.address,
            block_number=100,
        )
        get_logs_mock.return_value = [repeated_log]

        first = EvmLogScanner.scan_chain(chain=self.chain, batch_size=100).erc20
        second = EvmLogScanner.scan_chain(chain=self.chain, batch_size=100).erc20

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertEqual(first.created_transfers, 1)
        self.assertEqual(second.created_transfers, 0)
        self.assertEqual(Transfer.objects.count(), 1)
        self.assertEqual(cursor.last_scanned_block, 100)

    @patch(
        "currencies.models.Crypto.get_decimals",
        side_effect=AssertionError("scanner should use prefetched token decimals"),
    )
    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_uses_chain_token_decimals_without_extra_lookup(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
        _crypto_get_decimals_mock,
    ):
        # ERC20 扫描已持有 ChainToken 行数据，应直接复用链特定精度，避免逐条日志额外查库。
        self.token_deployment.decimals = 6
        self.token_deployment.save(update_fields=["decimals"])
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_logs_mock.return_value = [
            self._build_transfer_log(
                from_address=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000cc"
                ),
                to_address=self.deposit_slot.address,
                value=10**6,
            )
        ]

        EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        transfer = Transfer.objects.get()
        self.assertEqual(transfer.amount, Decimal("1"))

    @patch("chains.service.TransferService.create_observed_transfer")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_ignores_logs_outside_watch_set(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        create_observed_transfer_mock,
    ):
        # 非系统地址相关的日志必须在扫描层被过滤，避免把全链事件都送进业务入口。
        get_latest_block_number_mock.return_value = 40
        get_logs_mock.return_value = [
            self._build_transfer_log(
                from_address=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000cc"
                ),
                to_address=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000dd"
                ),
                block_number=40,
            )
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=40).erc20

        self.assertEqual(result.created_transfers, 0)
        self.assertEqual(result.observed_logs, 0)
        create_observed_transfer_mock.assert_not_called()
        self.assertEqual(Transfer.objects.count(), 0)

    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_uses_prefixed_transfer_topic_for_rpc_logs(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
    ):
        # 部分 RPC（如 NodeReal）要求日志 topic 必须是 0x 前缀 hex；少前缀会直接报 -32602。
        get_latest_block_number_mock.return_value = 100
        get_logs_mock.return_value = []

        EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        topic0_values = [
            call.kwargs["topic0"] for call in get_logs_mock.call_args_list
        ]
        self.assertIn(
            Web3.to_hex(Web3.keccak(text="Transfer(address,address,uint256)")),
            topic0_values,
        )

    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_advances_cursor_when_no_tokens_configured(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
    ):
        # 即使链上尚未配置 ERC20 合约，统一日志扫描仍会扫描 Xcash 合约事件。
        self.token_deployment.delete()
        get_latest_block_number_mock.return_value = 100

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32).erc20

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertEqual(result.created_transfers, 0)
        self.assertEqual(result.observed_logs, 0)
        self.assertEqual(cursor.last_scanned_block, 32)
        self.assertEqual(get_logs_mock.call_count, 1)
        self.assertIsNone(get_logs_mock.call_args.kwargs["addresses"])

    @patch("evm.tasks.EvmTaskPoller.poll_chain")
    @patch("evm.tasks.EvmChainScannerService.scan_chain")
    def test_scan_evm_chain_task_dispatches_combined_scanner(
        self,
        scan_chain_mock,
        poll_chain_mock,
    ):
        scan_chain_mock.return_value = Mock(
            native=Mock(
                from_block=1,
                to_block=2,
                observed_logs=3,
                created_transfers=1,
            ),
            erc20=Mock(
                from_block=1,
                to_block=2,
                observed_logs=4,
                created_transfers=2,
            ),
        )

        _scan_evm_chain(self.chain.pk)

        scan_chain_mock.assert_called_once()
        poll_chain_mock.assert_called_once()

    @patch("evm.tasks._scan_evm_chain.delay")
    def test_scan_active_evm_chains_dispatches_combined_task(
        self,
        delay_mock,
    ):
        self._create_scan_dispatch_ignored_chains()

        scan_active_evm_chains()

        delay_mock.assert_called_once_with(self.chain.pk)

    def test_watch_set_includes_deposit_slots_and_excludes_system_addresses(self):
        # scanner 只观察 DepositSlot 等入账合约地址，热钱包 Address 不再承接外部入账。
        watch_set = load_watch_set(chain=self.chain)

        self.assertIn(self.deposit_slot.address, watch_set.watched_addresses)
        self.assertNotIn(self.addr.address, watch_set.watched_addresses)

    def test_erc20_cursor_advance_never_rewinds_database_value(self):
        cursor = EvmScanCursor.objects.create(
            chain=self.chain,
            last_scanned_block=100,
        )
        stale_cursor = EvmScanCursor.objects.get(pk=cursor.pk)
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=150,
        )

        EvmLogScanner._advance_cursor(
            cursor=stale_cursor,
            scanned_to_block=120,
        )

        cursor.refresh_from_db()
        self.assertEqual(cursor.last_scanned_block, 150)

    @patch(
        "evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number",
        side_effect=EvmScannerRpcError("rpc timeout"),
    )
    def test_erc20_scan_records_cursor_error_when_rpc_fails(
        self, _get_latest_block_number_mock
    ):
        # RPC 失败后必须把错误留在游标上，方便后台与运维定位扫描停滞原因。
        with self.assertRaises(EvmScannerRpcError):
            EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertEqual(cursor.last_scanned_block, 0)
        self.assertEqual(cursor.last_error, "rpc timeout")
        self.assertIsNotNone(cursor.last_error_at)

    def test_erc20_scan_records_full_cursor_error_when_rpc_error_is_long(self):
        # RPC 供应商返回的长错误通常包含限制规则和建议查询范围，游标必须完整保留。
        long_error = "rpc limit exceeded: " + "x" * 360

        with (
            patch(
                "evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number",
                side_effect=EvmScannerRpcError(long_error),
            ),
            self.assertRaises(EvmScannerRpcError),
        ):
            EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertEqual(cursor.last_error, long_error)

    @patch("chains.service.TransferService.create_observed_transfer")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_erc20_scan_ignores_zero_value_transfer(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        create_observed_transfer_mock,
    ):
        # ERC20 Transfer 事件 value=0 无业务意义（如某些代币的 approve 触发），应在扫描层过滤。
        get_latest_block_number_mock.return_value = 40
        get_logs_mock.return_value = [
            self._build_transfer_log(
                from_address=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000cc"
                ),
                to_address=self.deposit_slot.address,
                value=0,
                block_number=40,
            )
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=40).erc20

        self.assertEqual(result.created_transfers, 0)
        create_observed_transfer_mock.assert_not_called()
