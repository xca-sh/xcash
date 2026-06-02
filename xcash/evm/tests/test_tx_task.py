from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.test import TestCase
from web3 import Web3
from web3.exceptions import ContractLogicError
from web3.exceptions import TransactionNotFound

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain


class EvmTxTaskTests(TestCase):
    def test_next_nonce_returns_count_of_existing_tasks(self):
        # nonce 基于已有任务数量推算，事务回滚时自动复用，不会产生空洞。
        chain = make_evm_chain(
            code=ChainCode.BSC,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "a1" * 32,
            status=TxTaskStatus.QUEUED,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            to="0x0000000000000000000000000000000000000002",
            value=0,
            nonce=0,
            gas=21000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x00",
        )
        self.assertEqual(EvmTxTask._next_nonce(addr, chain), 1)

    def test_broadcast_records_last_attempt_without_marking_completion(self):
        # EVM 主执行对象只记录发送尝试；是否上链由统一父任务状态推进。
        chain = make_evm_chain(
            code=ChainCode.Polygon,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to="0x0000000000000000000000000000000000000002",
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        tx_task.refresh_from_db()
        self.assertIsNotNone(tx_task.last_attempt_at)

    def test_broadcast_preflight_skips_send_when_sender_balance_insufficient(self):
        chain = make_evm_chain(
            code=ChainCode.ArbitrumOne,
            rpc="http://localhost:8545",
        )
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
                get_balance=Mock(return_value=1),
                estimate_gas=estimate_gas_mock,
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000200"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "d" * 64,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.QUEUED)
        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_not_called()
        self.assertIsNotNone(tx_task.last_attempt_at)

    def test_broadcast_does_not_estimate_gas_before_send(self):
        chain = make_evm_chain(
            code=ChainCode.Optimism,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "76" * 20),
            value=0,
            data="0xdeadbeef",
            gas=100_000,
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        task.broadcast()

        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()
        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)

    def test_broadcast_preflight_success_proceeds_to_send(self):
        # pre-flight 通过时继续进入 send_raw_transaction 流程，base_task 进入 PENDING_CHAIN。
        chain = make_evm_chain(
            code=ChainCode.Base,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)
        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()
        self.assertIsNotNone(tx_task.last_attempt_at)

    def test_broadcast_preflight_buffer_uses_task_gas_for_contract_call(self):
        # CONTRACT_CALL 的主动余额阈值按任务自身 gas 计算；余额刚好覆盖
        # 2 * task_gas * gas_price 时应通过并进入真实广播。
        chain = make_evm_chain(
            code=ChainCode.Avalanche,
            rpc="http://localhost:8545",
        )
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
        value = 0
        estimate_gas_mock = Mock(return_value=21_000)
        send_raw_mock = Mock()
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=gas_price,
                get_balance=Mock(
                    return_value=2 * 21_000 * gas_price
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=value,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=gas_price,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()

    def test_broadcast_preflight_contract_call_passes_at_exact_task_gas_buffer(self):
        # CONTRACT_CALL 使用任务自定义 gas；余额刚好等于新公式阈值时应通过。
        chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://localhost:8545",
        )
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=contract,
            value=0,
            data="0xdeadbeef",
            gas=task_gas,
            gas_price=gas_price,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        estimate_gas_mock.assert_not_called()
        send_raw_mock.assert_called_once()

    def test_balance_preflight_uses_signed_gas_price_not_current_lower_price(self):
        chain = make_evm_chain(
            code=ChainCode.Linea,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "a2" * 20),
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=10,
            signed_payload="0x7261772d6279746573",
        )

        task.broadcast()

        send_raw_mock.assert_not_called()
        base_task.refresh_from_db()
        assert base_task.status == TxTaskStatus.QUEUED

    @patch.object(EvmTxTask, "is_pipeline_full", return_value=True)
    def test_pending_chain_rebroadcast_ignores_pipeline_full(self, _pipeline_full_mock):
        # 低 nonce 的 PENDING_CHAIN 任务超时重播是为了释放同地址 pipeline；
        # 如果它也被 pipeline_full 阻断，满 pipeline 会无法自愈。
        chain = make_evm_chain(
            code=ChainCode.Scroll,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.PENDING_CHAIN,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast(allow_pending_chain_rebroadcast=True)

        send_raw_mock.assert_called_once()

    @patch.object(Address, "sign_evm_transaction")
    def test_rebroadcast_bumps_gas_price_by_125_percent(self, sign_mock):
        chain = make_evm_chain(
            code=ChainCode.Anvil,
            rpc="http://localhost:8545",
        )
        addr = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address("0x" + "a3" * 20),
        )
        sign_mock.return_value = SimpleNamespace(
            tx_hash="0x" + "a4" * 32,
            raw_transaction="0x02",
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=105,
                get_balance=Mock(return_value=10**18),
                send_raw_transaction=Mock(),
            )
        )
        base_task = TxTask.objects.create(
            chain=chain,
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "a5" * 32,
            status=TxTaskStatus.PENDING_CHAIN,
        )
        task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "a6" * 20),
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=100,
            signed_payload="0x01",
        )

        task.broadcast(allow_pending_chain_rebroadcast=True)

        tx_dict = sign_mock.call_args.kwargs["tx_dict"]
        assert tx_dict["gasPrice"] == 113

    def test_broadcast_keeps_fee_too_low_error_retryable_without_finalizing(self):
        chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "2" * 64,
            status=TxTaskStatus.PENDING_CHAIN,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
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
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)

    def test_broadcast_reraises_nonce_too_low_without_marking_pending(self):
        chain = make_evm_chain(
            code=ChainCode.BSC,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "3" * 64,
            status=TxTaskStatus.PENDING_CHAIN,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with self.assertRaisesMessage(RuntimeError, "nonce too low"):
            tx_task.broadcast(allow_pending_chain_rebroadcast=True)

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)

    def test_broadcast_blocks_higher_nonce_until_lower_nonce_settles(self):
        chain = make_evm_chain(
            code=ChainCode.Polygon,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        EvmTxTask.objects.create(
            base_task=lower_base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=lower_recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000109"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=1,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        send_raw_transaction_mock.assert_not_called()
        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.QUEUED)
        self.assertIsNone(tx_task.last_attempt_at)

    def test_broadcast_treats_already_known_as_idempotent_success(self):
        chain = make_evm_chain(
            code=ChainCode.ArbitrumOne,
            rpc="http://localhost:8545",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "4" * 64,
            status=TxTaskStatus.QUEUED,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        tx_task.broadcast()

        base_task.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)

    def test_queued_task_with_existing_hash_recovers_from_confirmed_receipt(self):
        """首播已被节点接受但阶段仍是 QUEUED 时，应先查 receipt 自愈而不是重发。"""
        chain = Chain.objects.create(
            code=ChainCode.Anvil,
            rpc="",
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
                get_balance=Mock(return_value=0),
                estimate_gas=Mock(return_value=21_000),
                send_raw_transaction=send_raw_mock,
            )
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000110"
        )
        base_task = TxTask.objects.create(
            chain=chain,
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash=tx_hash,
            status=TxTaskStatus.QUEUED,
        )
        TxHash.objects.create(
            tx_task=base_task,
            chain=chain,
            hash=tx_hash,
            version=0,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with patch(
            "evm.poller.EvmTaskPoller.process_succeeded_receipt"
        ) as process_mock:
            tx_task.broadcast()

        send_raw_mock.assert_not_called()
        process_mock.assert_called_once()
        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)

    def test_nonce_too_low_checks_existing_hash_before_reraising(self):
        """nonce too low 时若历史 hash 已有 receipt，应自动恢复而不是继续卡 QUEUED。"""
        chain = Chain.objects.create(
            code=ChainCode.Anvil,
            rpc="",
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
            sender=addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash=tx_hash,
            status=TxTaskStatus.QUEUED,
        )
        TxHash.objects.create(
            tx_task=base_task,
            chain=chain,
            hash=tx_hash,
            version=0,
        )
        tx_task = EvmTxTask.objects.create(
            base_task=base_task,
            sender=addr,
            chain=chain,
            nonce=0,
            to=recipient,
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
            signed_payload="0x7261772d6279746573",
        )

        with patch(
            "evm.poller.EvmTaskPoller.process_succeeded_receipt"
        ) as process_mock:
            tx_task.broadcast()

        send_raw_mock.assert_called_once()
        process_mock.assert_called_once()
        base_task.refresh_from_db()
        self.assertEqual(base_task.status, TxTaskStatus.PENDING_CHAIN)
