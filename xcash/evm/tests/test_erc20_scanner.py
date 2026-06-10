from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from chains.models import Wallet
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from evm.models import EvmScanCursor
from evm.models import EvmTxTask
from evm.scanner.logs import EvmLogScanner
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import load_owned_addresses_for_candidates
from evm.tasks import _scan_evm_chain
from evm.tasks import scan_active_evm_chains
from projects.models import Customer
from projects.models import Project


def create_active_evm_test_chain(*, code=ChainCode.BSC) -> Chain:
    chain = Chain.objects.create(code=code, rpc="", active=False)
    Chain.objects.filter(pk=chain.pk).update(
        rpc="http://scanner-chain.invalid",
        active=True,
    )
    chain.refresh_from_db()
    return chain


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
        self.assertEqual(to_block, 1999)

    def test_erc20_compute_scan_window_replay_never_goes_below_first_block(self):
        cursor = EvmScanCursor(last_scanned_block=1)
        from_block, to_block = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=10,
            batch_size=3,
        )

        self.assertEqual(from_block, 1)
        self.assertEqual(to_block, 4)

    def test_erc20_compute_scan_window_returns_none_when_latest_block_is_zero(self):
        cursor = EvmScanCursor(last_scanned_block=0)
        scan_window = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=0,
            batch_size=100,
        )

        self.assertIsNone(scan_window)

    def test_erc20_compute_scan_window_returns_none_when_cursor_is_ahead_of_chain(self):
        cursor = EvmScanCursor(last_scanned_block=1000)
        scan_window = EvmLogScanner._compute_scan_window(
            cursor=cursor,
            latest_block=900,
            batch_size=100,
        )

        self.assertIsNone(scan_window)


@override_settings(DEBUG=False)
class EvmErc20ScannerTests(TestCase):
    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.native = Crypto.objects.create(
            name="Scanner BNB",
            symbol="BNB-SCANNER",
            coingecko_id="binancecoin-scanner",
        )
        self.chain = create_active_evm_test_chain(code=ChainCode.BSC)
        self.token = Crypto.objects.create(
            name="Scanner Tether USD",
            symbol="USDT-SCANNER",
            coingecko_id="tether-scanner",
        )
        self.token_on_chain = CryptoOnChain.objects.create(
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
            webhook="https://example.com/webhook",
        )
        self.customer = Customer.objects.create(
            project=self.project,
            uid="scanner-customer",
        )
        self.vault_slot = VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bc"
            ),
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
            "address": self.token_on_chain.address,
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
            sender=self.addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash=tx_hash,
            status=TxTaskStatus.SUBMITTED,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            sender=self.addr,
            chain=self.chain,
            nonce=0,
            to=self.token_on_chain.address,
            value=0,
            data=f"0xa9059cbb{encoded_args}",
            gas=120_000,
            gas_price=1,
            signed_payload="0x01",
        )
        return base_task, encoded_args

    def _create_scan_dispatch_ignored_chains(self) -> None:
        Chain.objects.create(
            code=ChainCode.ArbitrumOne,
            rpc="",
            active=False,
        )
        Chain.objects.create(
            code=ChainCode.Tron,
            tron_api_key="tron-key",
            active=True,
        )

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_erc20_first_scan_starts_from_latest_tail_window(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        _get_block_timestamp_mock,
        _enqueue_processing_mock,
    ):
        # 首次创建统一日志游标时从首个批次开始推进。
        get_latest_block_number_mock.return_value = 100
        get_logs_mock.return_value = []

        EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertEqual(cursor.last_scanned_block, 32)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_transaction")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_creates_transfer_and_advances_cursor(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        get_transaction_mock,
        _enqueue_processing_mock,
    ):
        # 命中的 ERC20 Transfer 应落到统一 Transfer 表；首扫会直接对齐链头附近窗口。
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_transaction_mock.return_value = {"to": self.token_on_chain.address}
        get_logs_mock.side_effect = [
            [],
            [
                self._build_transfer_log(
                    from_address=Web3.to_checksum_address(
                        "0x00000000000000000000000000000000000000cc"
                    ),
                    to_address=self.vault_slot.address,
                ),
            ],
            [],
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        transfer = Transfer.objects.get()
        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )

        self.assertIsNone(result)
        self.assertEqual(transfer.hash, "0x" + "ab" * 32)
        self.assertEqual(
            transfer.to_address, Web3.to_checksum_address(self.vault_slot.address)
        )
        self.assertEqual(transfer.amount, Decimal("1"))
        self.assertEqual(cursor.last_scanned_block, 32)

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
                "blockNumber": "not-int",
            },
        ]
        rpc_client = type(
            "Rpc",
            (),
            {
                "get_logs": lambda *_args, **kwargs: (
                    malformed_logs
                    if kwargs["topic0"]
                    == Web3.to_hex(
                        Web3.keccak(text="Transfer(address,address,uint256)")
                    )
                    else []
                ),
                "get_transaction": lambda *_args, **_kwargs: {
                    "to": self.token_on_chain.address
                },
                "get_block_timestamp": lambda *_args, **_kwargs: 1_700_000_000,
            },
        )()
        token_registry = {self.token_on_chain.address: self.token_on_chain}

        created = EvmLogScanner.scan_range(
            chain=self.chain,
            rpc_client=rpc_client,
            token_registry=token_registry,
            from_block=100,
            to_block=100,
        )

        self.assertIsNone(created)
        create_observed_transfer_mock.assert_not_called()

    def test_erc20_scanner_persists_multiple_system_inbound_logs_from_same_tx(self):
        second_customer = Customer.objects.create(
            project=self.project,
            uid="scanner-customer-2",
        )
        second_slot = VaultSlot.objects.create(
            customer=second_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bd"
            ),
            salt=b"\x02" * 32,
        )
        sender = Web3.to_checksum_address("0x" + "cc" * 20)
        logs = [
            self._build_transfer_log(
                from_address=sender,
                to_address=self.vault_slot.address,
                log_index=5,
            ),
            self._build_transfer_log(
                from_address=sender,
                to_address=second_slot.address,
                log_index=6,
            ),
        ]
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {"to": self.token_on_chain.address}
        rpc_client.get_block_timestamp.return_value = 1_700_000_000

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=logs,
            rpc_client=rpc_client,
            token_registry={self.token_on_chain.address: self.token_on_chain},
        )

        self.assertIsNone(created)
        transfers = list(Transfer.objects.order_by("event_index"))
        self.assertEqual(len(transfers), 2)
        self.assertEqual([transfer.event_index for transfer in transfers], [5, 6])
        self.assertEqual(
            [transfer.to_address for transfer in transfers],
            [self.vault_slot.address, second_slot.address],
        )
        self.assertEqual({transfer.hash for transfer in transfers}, {"0x" + "ab" * 32})
        rpc_client.get_block_timestamp.assert_called_once_with(block_number=100)

    @patch("evm.scanner.observed_transfers.TransferService.create_observed_transfer")
    def test_erc20_scanner_skips_oversized_value_without_blocking_valid_event(
        self,
        create_observed_transfer_mock,
    ):
        second_customer = Customer.objects.create(
            project=self.project,
            uid="scanner-customer-oversized",
        )
        second_slot = VaultSlot.objects.create(
            customer=second_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000be"
            ),
            salt=b"\x03" * 32,
        )
        sender = Web3.to_checksum_address("0x" + "cc" * 20)
        logs = [
            self._build_transfer_log(
                from_address=sender,
                to_address=self.vault_slot.address,
                value=10**32,
                log_index=5,
            ),
            self._build_transfer_log(
                from_address=sender,
                to_address=second_slot.address,
                value=10**18,
                log_index=6,
            ),
        ]
        rpc_client = Mock()
        rpc_client.get_block_timestamp.return_value = 1_700_000_000

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=logs,
            rpc_client=rpc_client,
            token_registry={self.token_on_chain.address: self.token_on_chain},
        )

        self.assertIsNone(created)
        create_observed_transfer_mock.assert_called_once()
        observed = create_observed_transfer_mock.call_args.kwargs["observed"]
        self.assertEqual(observed.event_index, 6)
        self.assertEqual(observed.to_address, second_slot.address)

    @patch("evm.scanner.observed_transfers.TransferService.create_observed_transfer")
    def test_erc20_scanner_continues_after_single_persist_error(
        self,
        create_observed_transfer_mock,
    ):
        second_customer = Customer.objects.create(
            project=self.project,
            uid="scanner-customer-after-error",
        )
        second_slot = VaultSlot.objects.create(
            customer=second_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000bf"
            ),
            salt=b"\x04" * 32,
        )
        sender = Web3.to_checksum_address("0x" + "cc" * 20)
        logs = [
            self._build_transfer_log(
                from_address=sender,
                to_address=self.vault_slot.address,
                log_index=5,
            ),
            self._build_transfer_log(
                from_address=sender,
                to_address=second_slot.address,
                log_index=6,
            ),
        ]
        rpc_client = Mock()
        rpc_client.get_block_timestamp.return_value = 1_700_000_000
        create_observed_transfer_mock.side_effect = [
            RuntimeError("numeric field overflow"),
            None,
        ]

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=logs,
            rpc_client=rpc_client,
            token_registry={self.token_on_chain.address: self.token_on_chain},
        )

        self.assertIsNone(created)
        self.assertEqual(create_observed_transfer_mock.call_count, 2)
        observed_events = [
            call.kwargs["observed"].event_index
            for call in create_observed_transfer_mock.call_args_list
        ]
        self.assertEqual(observed_events, [5, 6])

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
            "to": self.token_on_chain.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        rpc_client.get_block_timestamp.return_value = 1_700_000_000
        token_registry = {self.token_on_chain.address: self.token_on_chain}

        with patch(
            "evm.internal_tx.processor.process_internal_transaction"
        ) as processor_mock:
            created = EvmLogScanner._process_logs(
                chain=self.chain,
                logs=[log],
                rpc_client=rpc_client,
                token_registry=token_registry,
            )

        base_task.refresh_from_db()
        processor_mock.assert_not_called()
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        self.assertIsNone(created)
        self.assertFalse(Transfer.objects.filter(hash=tx_hash).exists())
        self.assertEqual(base_task.status, TxTaskStatus.SUBMITTED)

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
            "to": self.token_on_chain.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        token_registry = {self.token_on_chain.address: self.token_on_chain}

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[log],
            rpc_client=rpc_client,
            token_registry=token_registry,
        )

        self.assertIsNone(created)
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
        token_registry = {self.token_on_chain.address: self.token_on_chain}

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[log],
            rpc_client=rpc_client,
            token_registry=token_registry,
        )

        self.assertIsNone(result)
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
            "to": self.token_on_chain.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = None
        token_registry = {self.token_on_chain.address: self.token_on_chain}

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[log],
            rpc_client=rpc_client,
            token_registry=token_registry,
        )

        self.assertIsNone(result)
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
            "to": self.token_on_chain.address,
            "input": f"0xa9059cbb{encoded_args}",
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        token_registry = {self.token_on_chain.address: self.token_on_chain}

        created = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[first_log, second_log],
            rpc_client=rpc_client,
            token_registry=token_registry,
        )

        self.assertIsNone(created)
        rpc_client.get_transaction.assert_not_called()
        rpc_client.get_transaction_receipt.assert_not_called()
        self.assertEqual(Transfer.objects.count(), 0)

    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_transaction")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_manual_rescan_keeps_transfer_idempotent(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        get_transaction_mock,
        _enqueue_processing_mock,
    ):
        # 手动重扫同一区间会重复看到同一日志，但统一唯一键必须保证不会重复落库。
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_transaction_mock.return_value = {"to": self.token_on_chain.address}
        repeated_log = self._build_transfer_log(
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000cc"
            ),
            to_address=self.vault_slot.address,
            block_number=99,
        )
        get_logs_mock.side_effect = [[], [repeated_log], [], [repeated_log]]

        first = EvmLogScanner.scan_chain(chain=self.chain, batch_size=100)
        second = EvmLogScanner.scan_chain(chain=self.chain, batch_size=100)

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(Transfer.objects.count(), 1)
        self.assertEqual(cursor.last_scanned_block, 99)

    @patch(
        "currencies.models.Crypto.get_decimals",
        side_effect=AssertionError("scanner should use prefetched token decimals"),
    )
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_transaction")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_uses_crypto_on_chain_decimals_without_extra_lookup(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        get_transaction_mock,
        _enqueue_processing_mock,
        _crypto_get_decimals_mock,
    ):
        # ERC20 扫描已持有 CryptoOnChain 行数据，应直接复用链特定精度，避免逐条日志额外查库。
        self.token_on_chain.decimals = 6
        self.token_on_chain.save(update_fields=["decimals"])
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_transaction_mock.return_value = {"to": self.token_on_chain.address}
        get_logs_mock.side_effect = [
            [],
            [
                self._build_transfer_log(
                    from_address=Web3.to_checksum_address(
                        "0x00000000000000000000000000000000000000cc"
                    ),
                    to_address=self.vault_slot.address,
                    value=10**6,
                )
            ],
            [],
        ]

        EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        transfer = Transfer.objects.get()
        self.assertEqual(transfer.amount, Decimal("1"))

    @patch("chains.service.TransferService.create_observed_transfer")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_ignores_logs_to_unowned_addresses(
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
                block_number=39,
            )
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=40)

        self.assertIsNone(result)
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

        topic0_values = [call.kwargs["topic0"] for call in get_logs_mock.call_args_list]
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
        self.token_on_chain.delete()
        get_latest_block_number_mock.return_value = 100

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(
            chain=self.chain,
        )
        self.assertIsNone(result)
        self.assertEqual(cursor.last_scanned_block, 32)
        self.assertEqual(get_logs_mock.call_count, 1)
        self.assertIsNone(get_logs_mock.call_args.kwargs["addresses"])

    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_never_rewinds_chain_latest_block_number(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
    ):
        # 扫描链路只接受更高链高，避免异常 RPC / 节点回退把确认进度倒拨。
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=200)
        self.chain.refresh_from_db()
        EvmScanCursor.objects.create(chain=self.chain, last_scanned_block=100)
        get_latest_block_number_mock.return_value = 120
        get_logs_mock.return_value = []

        EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        self.chain.refresh_from_db()
        self.assertEqual(self.chain.latest_block_number, 200)

    @patch("evm.tasks.EvmTaskPoller.poll_chain")
    @patch("evm.tasks.EvmScannerService.scan_chain")
    def test_scan_evm_chain_task_dispatches_combined_scanner(
        self,
        scan_chain_mock,
        poll_chain_mock,
    ):
        scan_chain_mock.return_value = Mock(
            from_block=1,
            to_block=2,
        )

        _scan_evm_chain(self.chain.pk)

        scan_chain_mock.assert_called_once()
        poll_chain_mock.assert_called_once()

    @patch("chains.tasks.block_number_updated.delay")
    @patch("evm.tasks.EvmTaskPoller.poll_chain")
    @patch("evm.tasks.EvmScannerService.scan_chain")
    def test_scan_evm_chain_dispatches_confirmation_checks_after_block_advance(
        self,
        scan_chain_mock,
        poll_chain_mock,
        block_number_updated_delay_mock,
    ):
        # EVM 链高由扫描链路刷新；一旦高度前进，已完成业务归类的 CONFIRMING
        # 转账必须继续进入统一确认检查，替代旧的 update_latest_block beat。
        Transfer.objects.create(
            chain=self.chain,
            block=10,
            block_hash="0x" + "11" * 32,
            hash="0x" + "12" * 32,
            crypto=self.token,
            from_address=self.addr.address,
            to_address=self.vault_slot.address,
            value=1,
            amount=Decimal("1"),
            timestamp=1,
            datetime=timezone.now(),
            processed_at=timezone.now(),
        )

        def advance_block(chain):
            Chain.objects.filter(pk=chain.pk).update(latest_block_number=20)

        scan_chain_mock.side_effect = advance_block

        _scan_evm_chain(self.chain.pk)

        block_number_updated_delay_mock.assert_called_once_with(self.chain.pk)
        poll_chain_mock.assert_called_once()

    @patch("evm.tasks._scan_evm_chain.delay")
    def test_scan_active_evm_chains_dispatches_due_chain(
        self,
        delay_mock,
    ):
        self._create_scan_dispatch_ignored_chains()
        # 把 last_scanned_at 推到远早于扫描周期，使本链到期；同时验证
        # 非活跃链与非 EVM 链不会被本调度器放行。
        self._mark_chain_due(self.chain)

        scan_active_evm_chains()

        delay_mock.assert_called_once_with(self.chain.pk)

    @patch("evm.tasks._scan_evm_chain.delay")
    def test_scan_active_evm_chains_skips_chain_not_yet_due(
        self,
        delay_mock,
    ):
        # 刚扫描过（last_scanned_at 接近当前时间）的链未到扫描周期，应被跳过。
        Chain.objects.filter(pk=self.chain.pk).update(last_scanned_at=timezone.now())

        scan_active_evm_chains()

        delay_mock.assert_not_called()

    @staticmethod
    def _mark_chain_due(chain) -> None:
        Chain.objects.filter(pk=chain.pk).update(
            last_scanned_at=timezone.now() - timedelta(hours=1)
        )

    def test_candidate_lookup_includes_vault_slots_and_excludes_system_addresses(self):
        # scanner 只观察本轮日志候选中的 VaultSlot 等入账地址，热钱包 Address 不承接外部入账。
        owned_addresses = load_owned_addresses_for_candidates(
            chain=self.chain,
            addresses={self.vault_slot.address, self.addr.address},
        )

        self.assertIn(self.vault_slot.address, owned_addresses)
        self.assertNotIn(self.addr.address, owned_addresses)

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
                to_address=self.vault_slot.address,
                value=0,
                block_number=39,
            )
        ]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=40)

        self.assertIsNone(result)
        create_observed_transfer_mock.assert_not_called()
