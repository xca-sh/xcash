from unittest.mock import Mock
from unittest.mock import call
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import Transfer
from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from currencies.models import ChainToken
from evm.models import EvmScanCursor
from evm.models import VaultSlot
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_RECEIVED_TOPIC0
from evm.scanner.logs import EvmLogScanner
from evm.scanner.watchers import EvmWatchSet
from evm.tests._fixtures import make_crypto
from evm.tests._fixtures import make_evm_chain
from evm.tests._fixtures import make_evm_system_address
from evm.tests._fixtures import make_wallet
from projects.models import Project
from users.models import Customer


@override_settings(DEBUG=False)
class EvmLogScannerTests(TestCase):
    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.native = make_crypto(symbol="LOG-NATIVE", name="Log Native")
        self.native.decimals = 18
        self.native.save(update_fields=["decimals"])
        self.chain = make_evm_chain(
            code="deposit-log-scan",
            chain_id=991001,
            native_coin=self.native,
        )
        self.token = make_crypto(symbol="LOG-USDT", name="Log USDT")
        self.token.decimals = 18
        self.token.save(update_fields=["decimals"])
        self.token_deployment = ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=Web3.to_checksum_address("0x" + "aa" * 20),
            decimals=18,
        )
        self.vault = make_evm_system_address(suffix="bb")
        self.project = Project.objects.create(
            name="Deposit Log Project",
            wallet=make_wallet(),
            webhook="https://example.com/webhook",
        )
        self.customer = Customer.objects.create(
            project=self.project,
            uid="deposit-log-customer",
        )
        self.slot = VaultSlot.objects.create(
            customer=self.customer,
            chain=self.chain,
            address=Web3.to_checksum_address("0x" + "bd" * 20),
            vault_address=self.vault.address,
            salt=b"\x01" * 32,
        )
        self.payer = Web3.to_checksum_address("0x" + "cc" * 20)

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @staticmethod
    def _address_topic(address: str) -> str:
        normalized = Web3.to_checksum_address(address)
        return "0x" + "0" * 24 + normalized[2:].lower()

    def _native_log(self) -> dict:
        return {
            "address": self.slot.address,
            "topics": [
                Web3.keccak(text="XcashNativeReceived(address,uint256)"),
                self._address_topic(self.payer),
            ],
            "data": hex(10**18),
            "blockNumber": 99,
            "blockHash": bytes.fromhex("11" * 32),
            "logIndex": 3,
            "transactionHash": bytes.fromhex("12" * 32),
        }

    def _erc20_log(self) -> dict:
        return {
            "address": self.token_deployment.address,
            "topics": [
                Web3.keccak(text="Transfer(address,address,uint256)"),
                self._address_topic(self.payer),
                self._address_topic(self.slot.address),
            ],
            "data": hex(2 * 10**18),
            "blockNumber": 99,
            "blockHash": bytes.fromhex("11" * 32),
            "logIndex": 4,
            "transactionHash": bytes.fromhex("23" * 32),
        }

    @patch("evm.scanner.logs.EvmObservedTransferProcessor.process")
    def test_process_logs_delegates_external_logs_to_transfer_observation(
        self,
        transfer_processor_mock,
    ):
        native_log = self._native_log()
        transfer_processor_mock.return_value = 0
        rpc_client = Mock()
        watch_set = EvmWatchSet(
            matched_addresses=frozenset({self.slot.address}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[native_log],
            rpc_client=rpc_client,
            watch_set=watch_set,
        )

        transfer_processor_mock.assert_called_once()
        processor_kwargs = transfer_processor_mock.call_args.kwargs
        self.assertEqual(processor_kwargs["chain"], self.chain)
        self.assertEqual(processor_kwargs["rpc_client"], rpc_client)
        self.assertEqual(processor_kwargs["raw_logs"], [native_log])
        self.assertEqual(processor_kwargs["watch_set"], watch_set)
        self.assertEqual(result, 0)

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_transaction")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_fetches_native_and_erc20_logs_with_scalable_filters(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        get_transaction_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_transaction_mock.side_effect = lambda *, tx_hash: {
            "0x" + "12" * 32: {"to": self.slot.address},
            "0x" + "23" * 32: {"to": self.token_deployment.address},
        }[tx_hash]
        get_logs_mock.side_effect = [[self._native_log()], [self._erc20_log()]]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        self.assertEqual(EvmScanCursor.objects.filter(chain=self.chain).count(), 1)
        cursor = EvmScanCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 32)
        self.assertIsNone(result)
        self.assertEqual(Transfer.objects.count(), 2)
        log_calls = [call.kwargs for call in get_logs_mock.call_args_list]
        self.assertEqual(len(log_calls), 2)
        self.assertIn(
            {
                "from_block": 1,
                "to_block": 32,
                "addresses": None,
                "topic0": XCASH_NATIVE_RECEIVED_TOPIC0,
                "summary": "获取 EVM Xcash 原生币入账日志失败",
            },
            log_calls,
        )
        self.assertIn(
            {
                "from_block": 1,
                "to_block": 32,
                "addresses": [self.token_deployment.address],
                "topic0": ERC20_TRANSFER_TOPIC0,
                "summary": "获取 EVM ERC20 Transfer 日志失败",
            },
            log_calls,
        )
        for log_call in log_calls:
            self.assertNotIn(self.slot.address, log_call["addresses"] or [])

    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_still_fetches_xcash_logs_without_cached_addresses(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
    ):
        get_latest_block_number_mock.return_value = 120
        get_logs_mock.return_value = []

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 32)
        self.assertIsNone(result)
        get_logs_mock.assert_has_calls(
            [
                call(
                    from_block=1,
                    to_block=32,
                    addresses=None,
                    topic0=XCASH_NATIVE_RECEIVED_TOPIC0,
                    summary="获取 EVM Xcash 原生币入账日志失败",
                ),
                call(
                    from_block=1,
                    to_block=32,
                    addresses=[self.token_deployment.address],
                    topic0=ERC20_TRANSFER_TOPIC0,
                    summary="获取 EVM ERC20 Transfer 日志失败",
                ),
            ],
            any_order=True,
        )

    @patch("evm.scanner.logs.EvmObservedTransferProcessor.process")
    def test_process_logs_batches_watched_address_lookup_from_log_candidates(
        self,
        transfer_processor_mock,
    ):
        native_log = self._native_log()
        erc20_log = self._erc20_log()
        unknown_recipient = Web3.to_checksum_address("0x" + "ef" * 20)
        unknown_log = {
            **erc20_log,
            "topics": [
                erc20_log["topics"][0],
                self._address_topic(self.payer),
                self._address_topic(unknown_recipient),
            ],
            "logIndex": 5,
            "transactionHash": bytes.fromhex("24" * 32),
        }
        transfer_processor_mock.return_value = 0
        rpc_client = Mock()

        EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[native_log, erc20_log, unknown_log],
            rpc_client=rpc_client,
            watch_set=EvmWatchSet(
                matched_addresses=frozenset(),
                tokens_by_address={self.token_deployment.address: self.token_deployment},
            ),
        )

        processor_watch_set = transfer_processor_mock.call_args.kwargs["watch_set"]
        self.assertEqual(processor_watch_set.matched_addresses, {self.slot.address})

    def test_scanner_skips_logs_from_known_internal_tx_hash(self):
        tx_hash = "0x" + "23" * 32
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.vault,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
        )
        TxHash.objects.create(
            tx_task=base_task,
            chain=self.chain,
            hash=tx_hash,
            version=1,
        )
        rpc_client = Mock()

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[self._erc20_log()],
            rpc_client=rpc_client,
            watch_set=EvmWatchSet(
                matched_addresses=frozenset({self.slot.address}),
                tokens_by_address={self.token_deployment.address: self.token_deployment},
            ),
        )

        self.assertEqual(result, 0)
        self.assertFalse(Transfer.objects.filter(hash=tx_hash).exists())
        rpc_client.get_block_timestamp.assert_not_called()
