from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import Transfer
from chains.models import TransferType
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from currencies.models import ChainToken
from evm.choices import TxKind
from evm.intents import build_deposit_slot_collect_intent
from evm.models import DepositSlot
from evm.models import EvmTxTask
from evm.models import EvmScanCursor
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_COLLECTED_TOPIC0
from evm.scanner.constants import XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_DEPOSITED_TOPIC0
from evm.scanner.logs import EvmLogScanner
from evm.scanner.observed_transfers import EvmObservedTransferProcessResult
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
        self.slot = DepositSlot.objects.create(
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
                Web3.keccak(text="XcashNativeDeposited(address,uint256)"),
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

    def _collected_log(self, *, tx_hash: str, log_index: int = 6) -> dict:
        return {
            "address": self.slot.address,
            "topics": [
                Web3.keccak(text="XcashCollected(address,uint256)"),
                self._address_topic(self.token_deployment.address),
            ],
            "data": hex(3 * 10**18),
            "blockNumber": 99,
            "blockHash": bytes.fromhex("11" * 32),
            "logIndex": log_index,
            "transactionHash": bytes.fromhex(tx_hash.removeprefix("0x")),
        }

    def _slot_to_vault_transfer_log(self, *, tx_hash: str, log_index: int = 7) -> dict:
        return {
            "address": self.token_deployment.address,
            "topics": [
                Web3.keccak(text="Transfer(address,address,uint256)"),
                self._address_topic(self.slot.address),
                self._address_topic(self.vault.address),
            ],
            "data": hex(3 * 10**18),
            "blockNumber": 99,
            "blockHash": bytes.fromhex("11" * 32),
            "logIndex": log_index,
            "transactionHash": bytes.fromhex(tx_hash.removeprefix("0x")),
        }

    def _deployed_log(self, *, tx_hash: str, log_index: int = 8) -> dict:
        return {
            "address": Web3.to_checksum_address("0x" + "de" * 20),
            "topics": [
                Web3.keccak(text="XcashDepositSlotDeployed(address,address,bytes32)"),
                self._address_topic(self.slot.address),
                self._address_topic(self.vault.address),
                "0x" + self.slot.salt.hex(),
            ],
            "data": "0x",
            "blockNumber": 99,
            "blockHash": bytes.fromhex("11" * 32),
            "logIndex": log_index,
            "transactionHash": bytes.fromhex(tx_hash.removeprefix("0x")),
        }

    @patch("evm.scanner.logs.EvmObservedTransferProcessor.process")
    @patch("evm.scanner.logs.EvmContractEventObserver.observe_logs")
    def test_process_logs_delegates_contract_events_and_transfer_observation(
        self,
        observe_logs_mock,
        transfer_processor_mock,
    ):
        native_log = self._native_log()
        tx_hash = "0x" + "12" * 32
        observe_logs_mock.return_value = {tx_hash}
        transfer_processor_mock.return_value = EvmObservedTransferProcessResult(
            raw_logs=[native_log],
            native_observed=0,
            erc20_observed=0,
            native_created=0,
            erc20_created=0,
        )
        rpc_client = Mock()
        watch_set = EvmWatchSet(
            watched_addresses=frozenset({self.slot.address}),
            tokens_by_address={self.token_deployment.address: self.token_deployment},
        )

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[native_log],
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=99,
            to_block=99,
        )

        observe_logs_mock.assert_called_once_with(
            chain=self.chain,
            logs=[native_log],
            rpc_client=rpc_client,
        )
        transfer_processor_mock.assert_called_once()
        processor_kwargs = transfer_processor_mock.call_args.kwargs
        self.assertEqual(processor_kwargs["chain"], self.chain)
        self.assertEqual(processor_kwargs["rpc_client"], rpc_client)
        self.assertEqual(processor_kwargs["from_block"], 99)
        self.assertEqual(processor_kwargs["to_block"], 99)
        self.assertEqual(processor_kwargs["raw_logs"], [native_log])
        self.assertEqual(processor_kwargs["watch_set"], watch_set)
        self.assertEqual(processor_kwargs["ignored_tx_hashes"], {tx_hash})
        self.assertEqual(result.native_observed, 0)
        self.assertEqual(result.native_created, 0)

    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    @patch("chains.service.TransferService.enqueue_processing")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_block_timestamp")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_fetches_contract_and_erc20_logs_with_scalable_filters(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        get_block_timestamp_mock,
        _enqueue_processing_mock,
        _mark_pending_confirm_mock,
    ):
        get_latest_block_number_mock.return_value = 100
        get_block_timestamp_mock.return_value = 1_700_000_000
        get_logs_mock.side_effect = [[self._native_log()], [self._erc20_log()]]

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        self.assertEqual(EvmScanCursor.objects.filter(chain=self.chain).count(), 1)
        cursor = EvmScanCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 32)
        self.assertEqual(result.native.created_transfers, 1)
        self.assertEqual(result.erc20.created_transfers, 1)
        self.assertEqual(Transfer.objects.count(), 2)
        self.assertEqual(
            set(Transfer.objects.values_list("event_id", flat=True)),
            {"native:3", "erc20:4"},
        )
        log_calls = [call.kwargs for call in get_logs_mock.call_args_list]
        self.assertEqual(len(log_calls), 2)
        self.assertIn(
            {
                "from_block": 1,
                "to_block": 32,
                "addresses": None,
                "topic0": [
                    XCASH_NATIVE_DEPOSITED_TOPIC0,
                    XCASH_COLLECTED_TOPIC0,
                    XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0,
                ],
                "summary": "获取 EVM Xcash 合约日志失败",
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
        for call in log_calls:
            self.assertNotIn(self.slot.address, call["addresses"] or [])

    @patch("evm.scanner.logs.load_watch_set")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_logs")
    @patch("evm.scanner.logs.EvmScannerRpcClient.get_latest_block_number")
    def test_scan_chain_advances_unified_cursor_when_watch_set_empty(
        self,
        get_latest_block_number_mock,
        get_logs_mock,
        load_watch_set_mock,
    ):
        get_latest_block_number_mock.return_value = 120
        load_watch_set_mock.return_value = type(
            "WatchSet",
            (),
            {"watched_addresses": frozenset(), "tokens_by_address": {}},
        )()

        result = EvmLogScanner.scan_chain(chain=self.chain, batch_size=32)

        cursor = EvmScanCursor.objects.get(chain=self.chain)
        self.assertEqual(cursor.last_scanned_block, 120)
        self.assertEqual(result.native.observed_logs, 0)
        self.assertEqual(result.erc20.observed_logs, 0)
        get_logs_mock.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_system_collected_event_routes_to_internal_tx_before_erc20_transfer(
        self,
        _enqueue_processing_mock,
    ):
        tx_hash = "0x" + "34" * 32
        intent = build_deposit_slot_collect_intent(
            address=self.vault,
            chain=self.chain,
            deposit_slot_address=self.slot.address,
            token_address=self.token_deployment.address,
        )
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.vault,
            tx_type=TxTaskType.DepositSlotCollect,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            address=self.vault,
            chain=self.chain,
            nonce=0,
            to=intent.to,
            value=intent.value,
            data=intent.data,
            gas=intent.gas,
            tx_kind=intent.tx_kind,
        )
        collected_log = self._collected_log(tx_hash=tx_hash)
        transfer_log = self._slot_to_vault_transfer_log(tx_hash=tx_hash)
        receipt = {
            "status": 1,
            "blockNumber": 99,
            "blockHash": "0x" + "11" * 32,
            "logs": [collected_log, transfer_log],
        }
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.vault.address,
            "to": self.slot.address,
            "input": intent.data,
        }
        rpc_client.get_transaction_receipt.return_value = receipt
        rpc_client.get_block_timestamp.return_value = 1_700_000_000

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[collected_log, transfer_log],
            rpc_client=rpc_client,
            watch_set=EvmWatchSet(
                watched_addresses=frozenset({self.slot.address}),
                tokens_by_address={self.token_deployment.address: self.token_deployment},
            ),
            from_block=99,
            to_block=99,
        )

        base_task.refresh_from_db()
        transfer = Transfer.objects.get(hash=tx_hash)
        transfer.process()
        transfer.refresh_from_db()
        self.assertEqual(result.erc20_created, 0)
        self.assertEqual(Transfer.objects.count(), 1)
        self.assertEqual(transfer.event_id, "collect:6")
        self.assertEqual(transfer.type, TransferType.Collect)
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CONFIRM)
        rpc_client.get_transaction.assert_called_once_with(tx_hash=tx_hash)
        rpc_client.get_transaction_receipt.assert_called_once_with(tx_hash=tx_hash)

    def test_external_collected_event_does_not_route_to_internal_tx(self):
        tx_hash = "0x" + "35" * 32
        collected_log = self._collected_log(tx_hash=tx_hash)
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.payer,
            "to": self.slot.address,
        }

        result = EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[collected_log],
            rpc_client=rpc_client,
            watch_set=EvmWatchSet(
                watched_addresses=frozenset({self.slot.address}),
                tokens_by_address={self.token_deployment.address: self.token_deployment},
            ),
            from_block=99,
            to_block=99,
        )

        self.assertEqual(result.native_observed, 0)
        self.assertEqual(result.erc20_observed, 0)
        self.assertFalse(Transfer.objects.filter(hash=tx_hash).exists())
        rpc_client.get_transaction.assert_called_once_with(tx_hash=tx_hash)
        rpc_client.get_transaction_receipt.assert_not_called()

    def test_system_deposit_slot_deployed_event_finalizes_deploy_task(self):
        tx_hash = "0x" + "36" * 32
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.vault,
            tx_type=TxTaskType.DepositSlotDeploy,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            address=self.vault,
            chain=self.chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "de" * 20),
            value=0,
            data="0x1234",
            gas=300_000,
            tx_kind=TxKind.CONTRACT_CALL,
        )
        deployed_log = self._deployed_log(tx_hash=tx_hash)
        rpc_client = Mock()
        rpc_client.get_transaction.return_value = {
            "hash": tx_hash,
            "from": self.vault.address,
            "to": Web3.to_checksum_address("0x" + "de" * 20),
            "input": "0x1234",
        }
        rpc_client.get_transaction_receipt.return_value = {
            "status": 1,
            "blockNumber": 99,
            "blockHash": "0x" + "11" * 32,
            "logs": [deployed_log],
        }
        rpc_client.get_block_timestamp.return_value = 1_700_000_000

        EvmLogScanner._process_logs(
            chain=self.chain,
            logs=[deployed_log],
            rpc_client=rpc_client,
            watch_set=EvmWatchSet(
                watched_addresses=frozenset({self.slot.address}),
                tokens_by_address={self.token_deployment.address: self.token_deployment},
            ),
            from_block=99,
            to_block=99,
        )

        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.FINALIZED)
        self.assertIs(base_task.success, True)
        self.assertFalse(Transfer.objects.filter(hash=tx_hash).exists())
