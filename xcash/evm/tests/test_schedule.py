from unittest.mock import patch

from django.test import TestCase
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from currencies.models import Crypto
from evm.intents import EvmTxIntent
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain


class EvmTxTaskScheduleTests(TestCase):
    def setUp(self):
        self.native = Crypto.objects.create(
            name="Schedule Ether",
            symbol="ETHSCH",
            coingecko_id="schedule-ether",
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://localhost:8545",
        )
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a01"
            ),
        )
        self.recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000b02"
        )

    def _intent(self, **overrides):
        values = {
            "sender": self.address,
            "chain": self.chain,
            "to": self.recipient,
            "value": 0,
            "data": "0xdeadbeef",
            "gas": 21_000,
            "tx_type": TxTaskType.VaultSlotCollect,
            "verify_fn": None,
        }
        values.update(overrides)
        return EvmTxIntent(**values)

    def test_schedule_persists_base_and_evm_fields_from_intent(self):
        intent = self._intent(
            to=Web3.to_checksum_address("0x0000000000000000000000000000000000000c03"),
            data="0x1234",
            gas=88_000,
        )

        task = EvmTxTask.schedule(intent)

        self.assertEqual(task.sender, intent.sender)
        self.assertEqual(task.chain, intent.chain)
        self.assertEqual(task.to, intent.to)
        self.assertEqual(task.value, intent.value)
        self.assertEqual(task.data, intent.data)
        self.assertEqual(task.gas, intent.gas)
        self.assertEqual(task.nonce, 0)

        base_task = task.base_task
        self.assertEqual(base_task.chain, intent.chain)
        self.assertEqual(base_task.sender, intent.sender)
        self.assertEqual(base_task.tx_type, intent.tx_type)
        self.assertEqual(base_task.status, TxTaskStatus.QUEUED)

        self.assertEqual(Address.objects.get(pk=self.address.pk), self.address)

    def test_schedule_runs_verify_fn_inside_lock_before_nonce_allocation(self):
        events = []
        original_acquire = Address.acquire_for_update

        def acquire_for_update(*, address):
            events.append("lock")
            return original_acquire(address=address)

        def verify():
            events.append("verify")

        def next_nonce(address, chain):
            events.append("nonce")
            return 0

        intent = self._intent(verify_fn=verify)

        with (
            patch.object(
                Address,
                "acquire_for_update",
                side_effect=acquire_for_update,
            ),
            patch.object(EvmTxTask, "_next_nonce", side_effect=next_nonce),
        ):
            task = EvmTxTask.schedule(intent)

        self.assertEqual(events, ["lock", "verify", "nonce"])
        self.assertEqual(task.nonce, 0)
        self.assertEqual(Address.objects.get(pk=self.address.pk), self.address)

    def test_schedule_rolls_back_when_verify_fn_raises(self):
        def reject():
            raise RuntimeError("balance changed")

        with self.assertRaisesRegex(RuntimeError, "balance changed"):
            EvmTxTask.schedule(self._intent(verify_fn=reject))

        self.assertEqual(TxTask.objects.count(), 0)
        self.assertEqual(EvmTxTask.objects.count(), 0)

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_schedule_delivers_first_broadcast_only_after_commit(self, delay_mock):
        # 首播在事务提交后直接投递，不再等 dispatch 轮询；提交前投递会让 worker
        # 读到尚未提交的任务（DoesNotExist），或在外层回滚后广播悬空任务。
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            task = EvmTxTask.schedule(self._intent())

        delay_mock.assert_not_called()
        for callback in callbacks:
            callback()
        delay_mock.assert_called_once_with(task.pk)

    def test_direct_delivery_keeps_nonce_order_for_back_to_back_schedules(self):
        # 同地址连续建两笔任务会各自投递；高 nonce 那笔必须被广播入口阻断，等低 nonce
        # 首播成功后由链式接力放行，不能抢先进入 mempool。
        from evm.tasks import _broadcast_evm_task

        with patch("evm.tasks._broadcast_evm_task.delay") as delay_mock:
            with self.captureOnCommitCallbacks(execute=True):
                first = EvmTxTask.schedule(self._intent())
            with self.captureOnCommitCallbacks(execute=True):
                second = EvmTxTask.schedule(self._intent())

        self.assertEqual(
            [call.args[0] for call in delay_mock.call_args_list],
            [first.pk, second.pk],
        )
        with patch.object(EvmTxTask, "broadcast") as broadcast_mock:
            _broadcast_evm_task.run(second.pk)
        broadcast_mock.assert_not_called()

    def test_schedule_rejects_non_zero_value(self):
        with self.assertRaisesRegex(ValueError, "value must be 0"):
            EvmTxTask.schedule(self._intent(value=1))

        self.assertEqual(TxTask.objects.count(), 0)
        self.assertEqual(EvmTxTask.objects.count(), 0)
