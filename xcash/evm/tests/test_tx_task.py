from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.db import IntegrityError
from django.db import transaction
from django.test import TestCase
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.exceptions import TransactionNotFound

from chains.models import Address
from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTaskType
from chains.models import TxHash
from chains.models import Wallet
from currencies.models import Crypto
from evm.choices import TxKind
from evm.models import EvmTxTask


class EvmTxTaskTests(TestCase):
    def test_create_without_tx_kind_is_rejected_by_database(self):
        native = Crypto.objects.create(
            name="Ethereum TxKind Constraint",
            symbol="ETHTKC",
            coingecko_id="ethereum-tx-kind-constraint",
        )
        chain = Chain.objects.create(
            code="eth-tx-kind-constraint",
            name="Ethereum TxKind Constraint",
            type=ChainType.EVM,
            chain_id=999_401,
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
            address="0x0000000000000000000000000000000000000C01",
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.QUEUED,
            success=None,
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            EvmTxTask.objects.create(
                base_task=base_task,
                address=addr,
                chain=chain,
                to="0x0000000000000000000000000000000000000002",
                value=0,
                nonce=0,
                gas=21000,
                gas_price=1,
                signed_payload="0x00",
            )

    def test_next_nonce_returns_count_of_existing_tasks(self):
        # nonce 基于已有任务数量推算，事务回滚时自动复用，不会产生空洞。
        native = Crypto.objects.create(
            name="Ethereum Nonce",
            symbol="ETHN",
            coingecko_id="ethereum-nonce",
        )
        chain = Chain.objects.create(
            code="eth-nonce",
            name="Ethereum Nonce",
            type=ChainType.EVM,
            chain_id=999,
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
            address="0x0000000000000000000000000000000000000F01",
        )

        # 无任何任务时 nonce 应从 0 开始
        self.assertEqual(EvmTxTask._next_nonce(addr, chain), 0)

        # 创建一个任务后 nonce 应为 1
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "a1" * 32,
            stage=TxTaskStage.QUEUED,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            to="0x0000000000000000000000000000000000000002",
            value=0,
            nonce=0,
            gas=21000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x00",
        )
        self.assertEqual(EvmTxTask._next_nonce(addr, chain), 1)

    def test_broadcast_records_last_attempt_without_marking_completion(self):
        # EVM 主执行对象只记录发送尝试；是否上链由统一父任务状态推进。
        native = Crypto.objects.create(
            name="Ethereum Last Attempt",
            symbol="ETHLA",
            coingecko_id="ethereum-last-attempt",
        )
        chain = Chain.objects.create(
            code="eth-last-attempt",
            name="Ethereum Last Attempt",
            type=ChainType.EVM,
            chain_id=999_402,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                # 余额覆盖 2 * erc20_transfer_gas 阈值即可通过主动检查
                get_balance=Mock(return_value=10**18),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=Mock(),
            ),
        )
        wallet = Wallet.objects.create()
        addr = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address="0x0000000000000000000000000000000000000001",
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
            to="0x0000000000000000000000000000000000000002",
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        tx_task.refresh_from_db()
        self.assertIsNotNone(tx_task.last_attempt_at)

    def test_broadcast_preflight_skips_send_when_withdrawal_balance_insufficient(self):
        native = Crypto.objects.create(
            name="Ethereum Vault Reraise",
            symbol="ETHVR",
            coingecko_id="ethereum-vault-reraise",
        )
        chain = Chain.objects.create(
            code="eth-vault-reraise",
            name="Ethereum Vault Reraise",
            type=ChainType.EVM,
            chain_id=20199,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        chain.base_transfer_gas = 21_000
        chain.erc20_transfer_gas = 60_000
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000199"
            ),
        )
        estimate_gas_mock = Mock()
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=10**17),
                estimate_gas=estimate_gas_mock,
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000200"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "d" * 64,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=10**18,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.QUEUED)
        self.assertIsNone(base_task.success)
        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_not_called()
        self.assertIsNotNone(tx_task.last_attempt_at)

    def test_broadcast_does_not_estimate_gas_before_send(self):
        native = Crypto.objects.create(
            name="Ethereum No Estimate",
            symbol="ETHNE",
            coingecko_id="ethereum-no-estimate",
        )
        chain = Chain.objects.create(
            code="eth-no-estimate",
            name="Ethereum No Estimate",
            type=ChainType.EVM,
            chain_id=20501,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address("0x" + "75" * 20),
        )
        estimate_gas_mock = Mock(side_effect=ContractLogicError("execution reverted"))
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=10**18),
                estimate_gas=estimate_gas_mock,
                send_raw_transaction=send_raw_mock,
            )
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "76" * 20),
            value=0,
            data="0xdeadbeef",
            gas=100_000,
            tx_kind=TxKind.CONTRACT_CALL,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        task.broadcast()

        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()
        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)

    def test_broadcast_preflight_success_proceeds_to_send(self):
        # pre-flight 通过时继续进入 send_raw_transaction 流程，base_task 进入 PENDING_CHAIN。
        native = Crypto.objects.create(
            name="Ethereum Preflight Ok",
            symbol="ETHPOK",
            coingecko_id="ethereum-preflight-ok",
        )
        chain = Chain.objects.create(
            code="eth-preflight-ok",
            name="Ethereum Preflight Ok",
            type=ChainType.EVM,
            chain_id=20401,
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
                "0x0000000000000000000000000000000000000401"
            ),
        )
        estimate_gas_mock = Mock(return_value=21_000)
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                # 余额充足：主动阈值通过
                get_balance=Mock(return_value=10**19),
                estimate_gas=estimate_gas_mock,
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000402"
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
            to=recipient,
            value=10**18,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(base_task.success)
        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()
        self.assertIsNotNone(tx_task.last_attempt_at)

    def test_broadcast_preflight_buffer_uses_task_gas_for_native_transfer(self):
        # NATIVE_TRANSFER 的主动余额阈值按任务自身 gas 计算；余额刚好覆盖
        # value + 2 * base_transfer_gas * gas_price 时应通过并进入真实广播。
        native = Crypto.objects.create(
            name="Ethereum Preflight Native Gas",
            symbol="ETHPNG",
            coingecko_id="ethereum-preflight-native-gas",
        )
        chain = Chain.objects.create(
            code="eth-preflight-native-gas",
            name="Ethereum Preflight Native Gas",
            type=ChainType.EVM,
            chain_id=20411,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        chain.base_transfer_gas = 21_000
        chain.erc20_transfer_gas = 60_000
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000411"
            ),
        )
        gas_price = 1_000
        value = 10**18
        estimate_gas_mock = Mock(return_value=21_000)
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=gas_price,
                get_balance=Mock(
                    return_value=value + 2 * chain.base_transfer_gas * gas_price
                ),
                estimate_gas=estimate_gas_mock,
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000412"
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
            to=recipient,
            value=value,
            gas=chain.base_transfer_gas,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=gas_price,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()

    def test_broadcast_preflight_contract_call_passes_at_exact_task_gas_buffer(self):
        # CONTRACT_CALL 使用任务自定义 gas；余额刚好等于新公式阈值时应通过。
        native = Crypto.objects.create(
            name="Ethereum Preflight Contract Gas",
            symbol="ETHPCG",
            coingecko_id="ethereum-preflight-contract-gas",
        )
        chain = Chain.objects.create(
            code="eth-preflight-contract-gas",
            name="Ethereum Preflight Contract Gas",
            type=ChainType.EVM,
            chain_id=20412,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        chain.base_transfer_gas = 21_000
        chain.erc20_transfer_gas = 60_000
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000421"
            ),
        )
        gas_price = 1_000
        task_gas = 45_000
        estimate_gas_mock = Mock(return_value=task_gas)
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=gas_price,
                get_balance=Mock(return_value=2 * task_gas * gas_price),
                estimate_gas=estimate_gas_mock,
                send_raw_transaction=send_raw_mock,
            )
        )
        contract = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000422"
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
            to=contract,
            value=0,
            data="0xdeadbeef",
            gas=task_gas,
            tx_kind=TxKind.CONTRACT_CALL,
            gas_price=gas_price,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()

    def test_balance_preflight_uses_signed_gas_price_not_current_lower_price(self):
        native = Crypto.objects.create(
            name="Ethereum Signed Gas",
            symbol="ETHSG",
            coingecko_id="ethereum-signed-gas",
        )
        chain = Chain.objects.create(
            code="eth-signed-gas",
            name="Ethereum Signed Gas",
            type=ChainType.EVM,
            chain_id=20502,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address("0x" + "a1" * 20),
        )
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=42_000),
                send_raw_transaction=send_raw_mock,
            )
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "a2" * 20),
            value=0,
            data="",
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=10,
            signed_payload="0x7261772d6279746573",
        )

        task.broadcast()

        send_raw_mock.assert_not_called()
        base_task.refresh_from_db()
        assert base_task.stage == TxTaskStage.QUEUED

    @patch.object(EvmTxTask, "is_pipeline_full", return_value=True)
    def test_pending_chain_rebroadcast_ignores_pipeline_full(self, _pipeline_full_mock):
        # 低 nonce 的 PENDING_CHAIN 任务超时重播是为了释放同地址 pipeline；
        # 如果它也被 pipeline_full 阻断，满 pipeline 会无法自愈。
        native = Crypto.objects.create(
            name="Ethereum Rebroadcast Pipeline",
            symbol="ETHRBP",
            coingecko_id="ethereum-rebroadcast-pipeline",
        )
        chain = Chain.objects.create(
            code="eth-rebroadcast-pipeline",
            name="Ethereum Rebroadcast Pipeline",
            type=ChainType.EVM,
            chain_id=20403,
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
                "0x0000000000000000000000000000000000000404"
            ),
        )
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=10**19),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000405"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=10**18,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast(allow_pending_chain_rebroadcast=True)

        send_raw_mock.assert_called_once()

    @patch("evm.models.get_signer_backend")
    def test_rebroadcast_bumps_gas_price_by_125_percent(self, get_signer_backend_mock):
        native = Crypto.objects.create(
            name="Ethereum Fee Bump",
            symbol="ETHFB",
            coingecko_id="ethereum-fee-bump",
        )
        chain = Chain.objects.create(
            code="eth-fee-bump",
            name="Ethereum Fee Bump",
            type=ChainType.EVM,
            chain_id=20503,
            rpc="http://localhost:8545",
            native_coin=native,
            active=True,
        )
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address("0x" + "a3" * 20),
        )
        signer = Mock()
        signer.sign_evm_transaction.return_value = SimpleNamespace(
            tx_hash="0x" + "a4" * 32,
            raw_transaction="0x02",
        )
        get_signer_backend_mock.return_value = signer
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=105,
                get_balance=Mock(return_value=10**18),
                send_raw_transaction=Mock(),
            )
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "a5" * 32,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "a6" * 20),
            value=0,
            data="",
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=100,
            signed_payload="0x01",
        )

        task.broadcast(allow_pending_chain_rebroadcast=True)

        tx_dict = signer.sign_evm_transaction.call_args.kwargs["tx_dict"]
        assert tx_dict["gasPrice"] == 113

    def test_broadcast_keeps_fee_too_low_error_retryable_without_finalizing(self):
        native = Crypto.objects.create(
            name="Ethereum Fee Too Low",
            symbol="ETHFTL",
            coingecko_id="ethereum-fee-too-low",
        )
        chain = Chain.objects.create(
            code="eth-fee-too-low",
            name="Ethereum Fee Too Low",
            type=ChainType.EVM,
            chain_id=20102,
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
                "0x0000000000000000000000000000000000000103"
            ),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=10**18),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=Mock(
                    side_effect=RuntimeError("replacement transaction underpriced")
                ),
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000104"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "2" * 64,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with self.assertRaisesMessage(
            RuntimeError,
            "replacement transaction underpriced",
        ):
            tx_task.broadcast(allow_pending_chain_rebroadcast=True)

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(base_task.success)

    def test_broadcast_reraises_nonce_too_low_without_marking_pending(self):
        native = Crypto.objects.create(
            name="Ethereum Nonce Too Low",
            symbol="ETHNTL",
            coingecko_id="ethereum-nonce-too-low",
        )
        chain = Chain.objects.create(
            code="eth-nonce-too-low",
            name="Ethereum Nonce Too Low",
            type=ChainType.EVM,
            chain_id=20103,
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
                "0x0000000000000000000000000000000000000105"
            ),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=10**18),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=Mock(side_effect=RuntimeError("nonce too low")),
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000106"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "3" * 64,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with self.assertRaisesMessage(RuntimeError, "nonce too low"):
            tx_task.broadcast(allow_pending_chain_rebroadcast=True)

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(base_task.success)

    def test_broadcast_blocks_higher_nonce_until_lower_nonce_settles(self):
        native = Crypto.objects.create(
            name="Ethereum Nonce Block",
            symbol="ETHNB",
            coingecko_id="ethereum-nonce-block",
        )
        chain = Chain.objects.create(
            code="eth-nonce-block",
            name="Ethereum Nonce Block",
            type=ChainType.EVM,
            chain_id=20104,
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
                "0x0000000000000000000000000000000000000107"
            ),
        )
        send_raw_transaction_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                send_raw_transaction=send_raw_transaction_mock,
            )
        )
        lower_recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000108"
        )
        lower_base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        EvmTxTask.objects.create(
            base_task=lower_base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=lower_recipient,
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000109"
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
            nonce=1,
            to=recipient,
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        send_raw_transaction_mock.assert_not_called()
        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.QUEUED)
        self.assertIsNone(base_task.success)
        self.assertIsNone(tx_task.last_attempt_at)

    def test_broadcast_treats_already_known_as_idempotent_success(self):
        native = Crypto.objects.create(
            name="Ethereum Already Known",
            symbol="ETHAK",
            coingecko_id="ethereum-already-known",
        )
        chain = Chain.objects.create(
            code="eth-already-known",
            name="Ethereum Already Known",
            type=ChainType.EVM,
            chain_id=20104,
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
                "0x0000000000000000000000000000000000000107"
            ),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_balance=Mock(return_value=10**18),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=Mock(side_effect=RuntimeError("already known")),
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000108"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "4" * 64,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(base_task.success)

    def test_queued_task_with_existing_hash_recovers_from_confirmed_receipt(self):
        """首播已被节点接受但阶段仍是 QUEUED 时，应先查 receipt 自愈而不是重发。"""
        native = Crypto.objects.create(
            name="Ethereum Queued Receipt Recovery",
            symbol="ETHQRR",
            coingecko_id="ethereum-queued-receipt-recovery",
        )
        chain = Chain.objects.create(
            code="eth-queued-receipt-recovery",
            name="Ethereum Queued Receipt Recovery",
            type=ChainType.EVM,
            chain_id=20105,
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
                "0x0000000000000000000000000000000000000109"
            ),
        )
        tx_hash = "0x" + "5" * 64
        send_raw_mock = Mock()
        receipt = {"status": 1, "blockNumber": 100, "logs": []}
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_transaction_receipt=Mock(return_value=receipt),
                get_balance=Mock(return_value=10**18),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000110"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        TxHash.objects.create(
            tx_task=base_task,
            chain=chain,
            hash=tx_hash,
            version=0,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=10**18,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with patch(
            "evm.poller.EvmTaskPoller._observe_confirmed_transaction"
        ) as observe_mock:
            tx_task.broadcast()

        send_raw_mock.assert_not_called()
        observe_mock.assert_called_once()
        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)

    def test_nonce_too_low_checks_existing_hash_before_reraising(self):
        """nonce too low 时若历史 hash 已有 receipt，应自动恢复而不是继续卡 QUEUED。"""
        native = Crypto.objects.create(
            name="Ethereum Nonce Too Low Recovery",
            symbol="ETHNTLR",
            coingecko_id="ethereum-nonce-too-low-recovery",
        )
        chain = Chain.objects.create(
            code="eth-nonce-too-low-recovery",
            name="Ethereum Nonce Too Low Recovery",
            type=ChainType.EVM,
            chain_id=20106,
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
                "0x0000000000000000000000000000000000000111"
            ),
        )
        tx_hash = "0x" + "6" * 64
        receipt = {"status": 1, "blockNumber": 100, "logs": []}
        get_receipt_mock = Mock(
            side_effect=[TransactionNotFound(tx_hash), receipt],
        )
        send_raw_mock = Mock(side_effect=RuntimeError("nonce too low"))
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=1,
                get_transaction_receipt=get_receipt_mock,
                get_balance=Mock(return_value=10**19),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000112"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        TxHash.objects.create(
            tx_task=base_task,
            chain=chain,
            hash=tx_hash,
            version=0,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=10**18,
            gas=21_000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with patch(
            "evm.poller.EvmTaskPoller._observe_confirmed_transaction"
        ) as observe_mock:
            tx_task.broadcast()

        send_raw_mock.assert_called_once()
        observe_mock.assert_called_once()
        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
