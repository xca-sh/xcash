"""EVM 主动交易派发步骤：nonce 顺序、pipeline 深度、队首退避、预算与链隔离。"""

import time
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from currencies.models import Crypto
from evm.constants import EVM_PIPELINE_DEPTH
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain
from evm.tx_loop import dispatch_evm_chain_round


class EvmTaskQueueTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.native = Crypto.objects.create(
            name="Ethereum Queue",
            symbol="ETHQ",
            coingecko_id="ethereum-queue",
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://ethq.local",
        )
        self.addr = self._create_sender(address_index=0, suffix="f1")

    def _create_sender(self, *, address_index: int, suffix: str) -> Address:
        return Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=address_index,
            address=Web3.to_checksum_address("0x" + "0" * 38 + suffix),
        )

    def _create_evm_task(
        self,
        *,
        tx_hash: str,
        status: str,
        nonce: int | None = None,
        address: Address | None = None,
        chain: Chain | None = None,
    ) -> EvmTxTask:
        # 任务级测试直接手工落库，聚焦"派发如何挑任务"和"终局任务是否被错误重播"。
        task_address = address or self.addr
        task_chain = chain or self.chain
        next_nonce = self._next_test_nonce(task_address, task_chain)
        if nonce is not None and nonce > next_nonce:
            # 触发器要求 nonce 连续，自动填充中间的空洞
            self._fill_nonce_gap(task_address, task_chain, next_nonce, nonce)
        target_nonce = next_nonce if nonce is None else nonce
        base_task = TxTask.objects.create(
            chain=task_chain,
            sender=task_address,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash=tx_hash,
            status=status,
        )
        return EvmTxTask.objects.create(
            base_task=base_task,
            sender=task_address,
            chain=task_chain,
            nonce=target_nonce,
            to=Web3.to_checksum_address("0x00000000000000000000000000000000000000f2"),
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
        )

    def _next_test_nonce(self, address: Address, chain: Chain) -> int:
        from django.db.models import Max

        max_nonce = EvmTxTask.objects.filter(sender=address, chain=chain).aggregate(
            m=Max("nonce")
        )["m"]
        return 0 if max_nonce is None else max_nonce + 1

    def _fill_nonce_gap(
        self, address: Address, chain: Chain, start: int, end: int
    ) -> None:
        """填充 [start, end) 区间的 nonce，满足触发器连续性约束。"""
        for n in range(start, end):
            filler_base = TxTask.objects.create(
                chain=chain,
                sender=address,
                tx_type=TxTaskType.VaultSlotCollect,
                status=TxTaskStatus.SUCCEEDED,
            )
            EvmTxTask.objects.create(
                base_task=filler_base,
                sender=address,
                chain=chain,
                nonce=n,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000f2"
                ),
                value=0,
                gas=21_000,
                data="0xdeadbeef",
                gas_price=1,
            )

    def _queued(self, count: int, **kwargs) -> list[EvmTxTask]:
        tasks = []
        for _ in range(count):
            self.hash_seq = getattr(self, "hash_seq", 0x9000) + 1
            tasks.append(
                self._create_evm_task(
                    tx_hash=f"0x{self.hash_seq:064x}",
                    status=TxTaskStatus.QUEUED,
                    **kwargs,
                )
            )
        return tasks

    def dispatch(
        self,
        *,
        chain: Chain | None = None,
        deadline: float | None = None,
        lands_in: str | None = TxTaskStatus.SUBMITTED,
        error: Exception | None = None,
    ) -> list[int]:
        """跑一次派发步骤，返回按顺序被广播的任务 pk。

        broadcast() 被替换为：记录调用，并把任务推进到 lands_in（None 表示保持 QUEUED，
        即 pre-flight 未过）；给了 error 则直接抛出。
        """
        broadcasted = []

        def fake_broadcast(task):
            broadcasted.append(task.pk)
            if error is not None:
                raise error
            if lands_in is not None:
                TxTask.objects.filter(pk=task.base_task_id).update(status=lands_in)

        with patch.object(
            EvmTxTask, "broadcast", autospec=True, side_effect=fake_broadcast
        ):
            dispatch_evm_chain_round(
                chain or self.chain,
                time.monotonic() + 60 if deadline is None else deadline,
            )
        return broadcasted

    def test_dispatches_queued_nonces_in_order_within_one_round(self):
        # 一轮内按 nonce 连续发出，积压不必等任何接力或下一轮。
        tasks = self._queued(4)

        self.assertEqual(self.dispatch(), [task.pk for task in tasks])

    def test_submitted_and_finalized_tasks_are_never_dispatched(self):
        self._create_evm_task(tx_hash="0x" + "c" * 64, status=TxTaskStatus.SUBMITTED)
        self._create_evm_task(tx_hash="0x" + "e" * 64, status=TxTaskStatus.SUCCEEDED)
        queued = self._create_evm_task(
            tx_hash="0x" + "b" * 64, status=TxTaskStatus.QUEUED
        )

        # SUBMITTED 的重播只走轮询步骤的超时路径，派发不能绕过重播间隔。
        self.assertEqual(self.dispatch(), [queued.pk])

    def test_lower_submitted_nonce_counts_as_consumed(self):
        self._create_evm_task(
            tx_hash="0x" + "13" * 32, status=TxTaskStatus.SUBMITTED, nonce=1
        )
        higher = self._create_evm_task(
            tx_hash="0x" + "14" * 32, status=TxTaskStatus.QUEUED, nonce=2
        )

        self.assertEqual(self.dispatch(), [higher.pk])

    def test_head_left_queued_blocks_higher_nonces_this_round(self):
        # pre-flight 未过（等补 gas）时队首仍是 QUEUED：更高 nonce 本轮一律不发，
        # 否则会在前序缺口仍在时把高 nonce 送进 mempool。
        head, _, _ = self._queued(3)

        self.assertEqual(self.dispatch(lands_in=None), [head.pk])

    def test_head_within_retry_backoff_blocks_whole_sender(self):
        head, higher = self._queued(2)
        EvmTxTask.objects.filter(pk=head.pk).update(last_attempt_at=timezone.now())

        self.assertEqual(self.dispatch(), [])

        EvmTxTask.objects.filter(pk=head.pk).update(
            last_attempt_at=timezone.now() - timedelta(minutes=5)
        )
        self.assertEqual(self.dispatch(), [head.pk, higher.pk])

    def test_broadcast_error_stops_the_sender_without_escaping(self):
        head, _ = self._queued(2)

        broadcasted = self.dispatch(error=RuntimeError("node down"))

        self.assertEqual(broadcasted, [head.pk])

    def test_each_sender_is_dispatched_from_its_own_head(self):
        other = self._create_sender(address_index=1, suffix="f3")
        own = self._queued(2)
        others = self._queued(2, address=other)

        self.assertCountEqual(self.dispatch(), [task.pk for task in own + others])

    def test_pipeline_full_blocks_and_resumes_after_a_slot_is_freed(self):
        submitted = [
            self._create_evm_task(
                tx_hash=f"0x{index:064x}", status=TxTaskStatus.SUBMITTED
            )
            for index in range(EVM_PIPELINE_DEPTH)
        ]
        queued = self._create_evm_task(
            tx_hash="0x" + "b1" * 32, status=TxTaskStatus.QUEUED
        )

        self.assertEqual(self.dispatch(), [])

        TxTask.objects.filter(pk=submitted[0].base_task_id).update(
            status=TxTaskStatus.SUCCEEDED
        )
        self.assertEqual(self.dispatch(), [queued.pk])

    def test_pipeline_filling_up_mid_round_stops_the_sender(self):
        for index in range(EVM_PIPELINE_DEPTH - 1):
            self._create_evm_task(
                tx_hash=f"0x{index + 0xF0:064x}", status=TxTaskStatus.SUBMITTED
            )
        head, _ = self._queued(2)

        self.assertEqual(self.dispatch(), [head.pk])

    def test_budget_is_checked_only_between_broadcasts(self):
        # 截止时刻已过也至少发出一笔，剩下的下一轮（或下次唤醒）续发。
        first, second = self._queued(2)

        self.assertEqual(self.dispatch(deadline=time.monotonic() - 1), [first.pk])
        self.assertEqual(self.dispatch(), [second.pk])

    def test_only_tasks_of_the_given_chain_are_dispatched(self):
        other_chain = make_evm_chain(code=ChainCode.BSC, rpc="http://bsc.local")
        own = self._queued(1)
        self._queued(1, chain=other_chain)

        self.assertEqual(self.dispatch(), [own[0].pk])

    def test_model_broadcast_still_refuses_to_jump_a_lower_queued_nonce(self):
        # 派发只挑队首，但模型入口自身仍须守住 nonce 顺序，防止其他调用方越过缺口。
        self._create_evm_task(tx_hash="0x" + "1" * 64, status=TxTaskStatus.QUEUED)
        higher = self._create_evm_task(
            tx_hash="0x" + "2" * 64, status=TxTaskStatus.QUEUED
        )

        with (
            patch.object(
                EvmTxTask, "_recover_queued_receipt_if_any", return_value=False
            ),
            patch.object(EvmTxTask, "_execute_broadcast") as execute,
        ):
            higher.broadcast()

        execute.assert_not_called()

    @patch("evm.tasks.logger.warning")
    def test_scan_stuck_queued_evm_tx_tasks_logs_only_nonce_head(self, warning_mock):
        from evm.tasks import scan_stuck_queued_evm_tx_tasks

        stuck_head = self._create_evm_task(
            tx_hash="0x" + "8a" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=0,
        )
        blocked_higher = self._create_evm_task(
            tx_hash="0x" + "8b" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=1,
        )
        fresh_head = self._create_evm_task(
            tx_hash="0x" + "8c" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=2,
        )
        old_time = timezone.now() - timedelta(minutes=31)
        EvmTxTask.objects.filter(pk__in=[stuck_head.pk, blocked_higher.pk]).update(
            created_at=old_time,
            last_attempt_at=old_time,
        )
        EvmTxTask.objects.filter(pk=fresh_head.pk).update(created_at=timezone.now())

        alerted = scan_stuck_queued_evm_tx_tasks.run()

        self.assertEqual(alerted, 1)
        warning_mock.assert_called_once()
        self.assertEqual(warning_mock.call_args.kwargs["evm_task_id"], stuck_head.pk)
