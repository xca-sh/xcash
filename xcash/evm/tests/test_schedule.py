import time
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase
from django.test import TransactionTestCase
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from chains.runner import ChainLocks
from currencies.models import Crypto
from evm.intents import EvmTxIntent
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain
from evm.tx_loop import dispatch_evm_chain_round


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

    def test_schedule_wakes_chain_loop_without_publishing_to_celery(self):
        # 首播不再经 Celery：在建任务的同一事务内发 PG 通知，唤醒该链的 chain-runner 线程。
        with (
            patch("evm.models.notify_chain_tx_pending") as notify,
            patch("celery.app.task.Task.apply_async") as apply_async,
        ):
            EvmTxTask.schedule(self._intent())

        notify.assert_called_once_with(self.chain.pk)
        apply_async.assert_not_called()

    def test_back_to_back_schedules_are_broadcast_in_nonce_order(self):
        # 同地址连续建的两笔任务由链线程按 nonce 顺序发出，高 nonce 不会抢先进入 mempool。
        first = EvmTxTask.schedule(self._intent())
        second = EvmTxTask.schedule(self._intent())
        broadcasted = []

        def fake_broadcast(task):
            broadcasted.append(task.nonce)
            TxTask.objects.filter(pk=task.base_task_id).update(
                status=TxTaskStatus.SUBMITTED
            )

        with patch.object(
            EvmTxTask, "broadcast", autospec=True, side_effect=fake_broadcast
        ):
            dispatch_evm_chain_round(self.chain, time.monotonic() + 60)

        self.assertEqual(broadcasted, [first.nonce, second.nonce])

    def test_schedule_rejects_non_zero_value(self):
        with self.assertRaisesRegex(ValueError, "value must be 0"):
            EvmTxTask.schedule(self._intent(value=1))

        self.assertEqual(TxTask.objects.count(), 0)
        self.assertEqual(EvmTxTask.objects.count(), 0)


class EvmTxTaskScheduleWakeupTests(TransactionTestCase):
    """唤醒通知必须与建任务同事务提交：TestCase 的外层事务从不提交，只能用 TransactionTestCase。"""

    def setUp(self):
        Crypto.objects.create(
            name="Wakeup Ether", symbol="ETHWK", coingecko_id="wakeup-ether"
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum, rpc="http://localhost:8545"
        )
        self.address = Address.objects.create(
            wallet=Wallet.objects.create(),
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a01"
            ),
        )
        self.listener = ChainLocks()
        self.addCleanup(self.listener.close)

    def intent(self):
        return EvmTxIntent(
            sender=self.address,
            chain=self.chain,
            to=Web3.to_checksum_address("0x0000000000000000000000000000000000000b02"),
            value=0,
            data="0x",
            gas=21_000,
            tx_type=TxTaskType.VaultSlotCollect,
            verify_fn=None,
        )

    def test_committed_schedule_wakes_the_chain_loop(self):
        EvmTxTask.schedule(self.intent())

        self.assertEqual(self.listener.wait_for_notifies(5), [str(self.chain.pk)])

    def test_schedule_rolled_back_with_outer_transaction_sends_nothing(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            EvmTxTask.schedule(self.intent())
            raise RuntimeError("business rollback")

        self.assertEqual(self.listener.wait_for_notifies(0.3), [])
        self.assertFalse(EvmTxTask.objects.exists())
