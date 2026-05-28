from types import SimpleNamespace
from unittest.mock import ANY
from unittest.mock import Mock
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from chains.models import Wallet
from chains.service import ObservedTransferPayload
from chains.service import TransferService
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from currencies.models import Crypto
from evm.choices import TxKind
from evm.intents import build_native_transfer_intent
from evm.models import EvmScanCursor
from evm.models import EvmTxTask
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.service import EvmScannerService
from evm.scanner.watchers import EvmWatchSet


class EvmChainScannerServiceTests(TestCase):
    def setUp(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        self.native = Crypto.objects.create(
            name="Ethereum Scanner Service",
            symbol="ETHSS",
            coingecko_id="ethereum-scanner-service",
        )
        self.chain = Chain.objects.create(
            code="eth-scanner-service",
            name="Ethereum Scanner Service",
            type=ChainType.EVM,
            chain_id=20001,
            rpc="http://localhost:8545",
            native_coin=self.native,
            active=True,
            latest_block_number=88,
        )

    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    @patch("evm.scanner.service.EvmLogScanner.scan_chain")
    def test_scan_chain_skips_disabled_cursor(
        self,
        scan_chain_mock,
    ):
        EvmScanCursor.objects.create(
            chain=self.chain,
            enabled=False,
        )

        result = EvmScannerService.scan_chain(chain=self.chain)

        scan_chain_mock.assert_not_called()
        self.assertEqual(result, 0)

    @patch("evm.scanner.service.EvmLogScanner.scan_chain")
    def test_scan_chain_scans_native_and_erc20(
        self,
        scan_chain_mock,
    ):
        scan_chain_mock.return_value = 3

        result = EvmScannerService.scan_chain(chain=self.chain)

        scan_chain_mock.assert_called_once_with(chain=self.chain, rpc_client=ANY)
        self.assertEqual(result, 3)

    @patch(
        "evm.scanner.service.EvmLogScanner.scan_chain",
        side_effect=EvmScannerRpcError("rpc timeout"),
    )
    def test_scan_chain_returns_empty_when_rpc_fails(
        self,
        scan_chain_mock,
    ):
        result = EvmScannerService.scan_chain(chain=self.chain)

        scan_chain_mock.assert_called_once_with(chain=self.chain, rpc_client=ANY)
        self.assertEqual(result, 0)

    @patch("evm.scanner.service.load_watch_set")
    @patch("evm.scanner.service.EvmLogScanner.scan_range")
    def test_reconcile_blocks_scans_native_and_erc20_ranges(
        self,
        scan_range_mock,
        load_watch_set_mock,
    ):
        load_watch_set_mock.return_value = EvmWatchSet(
            matched_addresses=frozenset(),
            tokens_by_address={},
        )
        scan_range_mock.return_value = 3

        result = EvmScannerService.reconcile_blocks(
            chain=self.chain,
            block_numbers={10},
        )

        scan_range_mock.assert_called_once_with(
            chain=self.chain,
            rpc_client=ANY,
            watch_set=load_watch_set_mock.return_value,
            from_block=10,
            to_block=10,
        )
        self.assertEqual(result.created_transfers, 3)

    @override_settings(SIGNER_BACKEND="remote")
    def test_broadcast_rejects_local_fallback_when_remote_signer_enabled(self):
        # remote signer 模式下，广播阶段不允许再用本地私钥补签，避免应用进程重新持钥。
        native = Crypto.objects.create(
            name="Ethereum Remote Signer",
            symbol="ETHRS",
            coingecko_id="ethereum-remote-signer",
        )
        chain = Chain.objects.create(
            code="eth-remote-signer",
            name="Ethereum Remote Signer",
            type=ChainType.EVM,
            chain_id=20002,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=2, send_raw_transaction=Mock(), account=Mock()
            ),
        )
        wallet = Wallet.objects.create()
        addr = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000001"
            ),
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x0000000000000000000000000000000000000002"),
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="",
        )

        with self.assertRaisesMessage(Exception, "远端 signer 请求失败"):
            tx_task.broadcast()

        chain.w3.eth.account.sign_transaction.assert_not_called()

    @patch.object(EvmTxTask, "_next_nonce", return_value=0)
    def test_schedule_defers_signing_until_first_broadcast(
        self,
        _next_nonce_mock,
    ):
        # 新 EVM 任务创建时只分配 nonce，不应提前签名或生成 tx_hash。
        native = Crypto.objects.create(
            name="Ethereum Deferred Signing",
            symbol="ETHDS",
            coingecko_id="ethereum-deferred-signing",
        )
        chain = Chain.objects.create(
            code="eth-deferred-sign",
            name="Ethereum Deferred Signing",
            type=ChainType.EVM,
            chain_id=1,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        wallet = Wallet.objects.create()
        addr = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f1"
            ),
        )
        task = EvmTxTask.schedule(
            build_native_transfer_intent(
                address=addr,
                chain=chain,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000f2"
                ),
                value=123,
                tx_type=TxTaskType.Withdrawal,
            )
        )

        self.assertEqual(task.signed_payload, "")
        self.assertIsNone(task.gas_price)
        self.assertIsNone(task.base_task.tx_hash)
        self.assertFalse(TxHash.objects.filter(tx_task=task.base_task).exists())

    @patch("evm.models.get_signer_backend")
    @patch.object(EvmTxTask, "_next_nonce", return_value=0)
    def test_first_broadcast_creates_initial_tx_hash_history(
        self,
        _next_nonce_mock,
        get_signer_backend_mock,
    ):
        native = Crypto.objects.create(
            name="Ethereum TxHash History",
            symbol="ETHTXH",
            coingecko_id="ethereum-txhash-history-evm",
        )
        chain = Chain.objects.create(
            code="eth-txhash-history",
            name="Ethereum TxHash History",
            type=ChainType.EVM,
            chain_id=101,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000fa"
            ),
        )
        chain.__dict__["w3"] = SimpleNamespace(eth=SimpleNamespace(gas_price=9))
        signer_backend = Mock()
        signer_backend.sign_evm_transaction.return_value = SimpleNamespace(
            tx_hash="0x" + "ac" * 32,
            raw_transaction="0xdeadbeef",
        )
        get_signer_backend_mock.return_value = signer_backend

        task = EvmTxTask.schedule(
            build_native_transfer_intent(
                address=addr,
                chain=chain,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000fb"
                ),
                value=123,
                tx_type=TxTaskType.Withdrawal,
            )
        )

        self.assertIsNone(task.base_task.tx_hash)
        self.assertFalse(TxHash.objects.filter(tx_task=task.base_task).exists())

        chain.__dict__["w3"].eth.send_raw_transaction = Mock()
        # 提供 get_balance，让主动阈值通过 pre-flight 进入真实广播。
        chain.__dict__["w3"].eth.get_balance = Mock(return_value=10**18)
        task.broadcast()

        task.refresh_from_db()
        task.base_task.refresh_from_db()
        history = TxHash.objects.get(tx_task=task.base_task, version=1)
        self.assertEqual(history.hash, task.base_task.tx_hash)
        self.assertEqual(history.chain_id, chain.pk)
        self.assertEqual(task.signed_payload, "0xdeadbeef")
        self.assertEqual(task.gas_price, 9)

    @patch("evm.models.get_signer_backend")
    def test_schedule_uses_next_nonce_after_highest_existing_nonce(
        self,
        get_signer_backend_mock,
    ):
        native = Crypto.objects.create(
            name="Ethereum Nonce State",
            symbol="ETHNS",
            coingecko_id="ethereum-nonce-state",
        )
        chain = Chain.objects.create(
            code="eth-nonce-state",
            name="Ethereum Nonce State",
            type=ChainType.EVM,
            chain_id=3,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        wallet = Wallet.objects.create()
        addr = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f5"
            ),
        )
        # 填充 nonce 0-4，满足触发器连续性约束
        for n in range(5):
            filler_base = TxTask.objects.create(
                chain=chain,
                address=addr,
                tx_type=TxTaskType.Withdrawal,
                stage=TxTaskStage.FINALIZED,
                success=True,
            )
            EvmTxTask.objects.create(
                base_task=filler_base,
                address=addr,
                chain=chain,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000f6"
                ),
                value=0,
                nonce=n,
                gas=21_000,
                tx_kind=TxKind.NATIVE_TRANSFER,
                gas_price=1,
            )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "ef" * 32,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            to=Web3.to_checksum_address("0x00000000000000000000000000000000000000f6"),
            value=0,
            nonce=5,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x01",
        )
        chain.__dict__["w3"] = SimpleNamespace(eth=SimpleNamespace(gas_price=9))
        signer_backend = Mock()
        signer_backend.sign_evm_transaction.return_value = SimpleNamespace(
            tx_hash="0x" + "aa" * 32,
            raw_transaction="0xdeadbeef",
        )
        get_signer_backend_mock.return_value = signer_backend

        task = EvmTxTask.schedule(
            build_native_transfer_intent(
                address=addr,
                chain=chain,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000f7"
                ),
                value=123,
                tx_type=TxTaskType.Withdrawal,
            )
        )

        self.assertEqual(task.nonce, 6)

    @patch.object(EvmTxTask, "_next_nonce", return_value=0)
    @patch("evm.models.AddressChainState.acquire_for_update")
    def test_schedule_no_longer_reads_gas_price_before_acquiring_account_chain_state_lock(
        self,
        acquire_state_mock,
        _next_nonce_mock,
    ):
        native = Crypto.objects.create(
            name="Ethereum Gas Price Prefetch",
            symbol="ETHGP",
            coingecko_id="ethereum-gas-price-prefetch",
        )
        chain = Chain.objects.create(
            code="eth-gas-prefetch",
            name="Ethereum Gas Price Prefetch",
            type=ChainType.EVM,
            chain_id=4,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        wallet = Wallet.objects.create()
        addr = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f8"
            ),
        )
        order: list[str] = []

        class EthClient:
            @property
            def gas_price(self):
                order.append("gas_price")
                return 9

        chain.__dict__["w3"] = SimpleNamespace(eth=EthClient())
        acquire_state_mock.side_effect = lambda **kwargs: (
            order.append("lock"),
            SimpleNamespace(),
        )[1]

        EvmTxTask.schedule(
            build_native_transfer_intent(
                address=addr,
                chain=chain,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000f9"
                ),
                value=123,
                tx_type=TxTaskType.Withdrawal,
            )
        )

        self.assertEqual(order[:1], ["lock"])

    @patch("chains.service.Transfer.objects.create")
    @patch("chains.service.TransferService._mark_tx_task_pending_confirm")
    def test_create_observed_transfer_marks_matching_tx_task_pending_confirm(
        self,
        mark_pending_confirm_mock,
        transfer_create_mock,
    ):
        # 只要链上已经观察到该 EVM hash，就应推进统一父任务进入待确认。
        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        crypto = chain.native_coin
        transfer_create_mock.return_value = Mock()
        observed = ObservedTransferPayload(
            chain=chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            tx_hash="0x" + "2" * 64,
            from_address="0x0000000000000000000000000000000000000001",
            to_address="0x0000000000000000000000000000000000000002",
            crypto=crypto,
            value=1,
            amount=1,
            timestamp=1,
            occurred_at=SimpleNamespace(),
        )

        TransferService.create_observed_transfer(observed=observed)

        mark_pending_confirm_mock.assert_called_once_with(
            chain=chain,
            tx_hash="0x" + "2" * 64,
        )
