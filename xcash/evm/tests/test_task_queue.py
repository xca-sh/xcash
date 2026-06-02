from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
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
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain


class EvmTaskQueueTests(TestCase):
    queue_lock_key = "dispatch_evm_tx_tasks-locked"

    def setUp(self):
        self._clear_singleton_locks()
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
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f1"
            ),
        )

    def _clear_singleton_locks(self):
        cache.delete(self.queue_lock_key)

    def _create_evm_task(
        self,
        *,
        tx_hash: str,
        status: str,
        nonce: int | None = None,
        address: Address | None = None,
    ) -> EvmTxTask:
        # 任务级测试直接手工落库，聚焦"队列如何挑任务"和"终局任务是否被错误重播"。
        task_address = address or self.addr
        next_nonce = self._next_test_nonce(task_address)
        if nonce is not None and nonce > next_nonce:
            # 触发器要求 nonce 连续，自动填充中间的空洞
            self._fill_nonce_gap(task_address, next_nonce, nonce)
        target_nonce = next_nonce if nonce is None else nonce
        base_task = TxTask.objects.create(
            chain=self.chain,
            sender=task_address,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash=tx_hash,
            status=status,
        )
        return EvmTxTask.objects.create(
            base_task=base_task,
            sender=task_address,
            chain=self.chain,
            nonce=target_nonce,
            to=Web3.to_checksum_address("0x00000000000000000000000000000000000000f2"),
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
        )

    def _next_test_nonce(self, address: Address) -> int:
        from django.db.models import Max

        max_nonce = EvmTxTask.objects.filter(
            sender=address, chain=self.chain
        ).aggregate(m=Max("nonce"))["m"]
        return 0 if max_nonce is None else max_nonce + 1

    def _fill_nonce_gap(self, address: Address, start: int, end: int) -> None:
        """填充 [start, end) 区间的 nonce，满足触发器连续性约束。"""
        for n in range(start, end):
            filler_base = TxTask.objects.create(
                chain=self.chain,
                sender=address,
                tx_type=TxTaskType.VaultSlotCollect,
                status=TxTaskStatus.CONFIRMED,
            )
            EvmTxTask.objects.create(
                base_task=filler_base,
                sender=address,
                chain=self.chain,
                nonce=n,
                to=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000f2"
                ),
                value=0,
                gas=21_000,
                data="0xdeadbeef",
                gas_price=1,
            )

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_tx_task_skips_finalized_tx_task(self, broadcast_mock):
        # 已终局的链上任务不应再次广播，否则会把成功/失败终态重新拉回执行面。
        from evm.tasks import _broadcast_evm_task

        tx_task = self._create_evm_task(
            tx_hash="0x" + "a" * 64,
            status=TxTaskStatus.CONFIRMED,
        )

        _broadcast_evm_task.run(tx_task.pk)

        broadcast_mock.assert_not_called()

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_tx_task_skips_pending_chain_to_avoid_immediate_rebroadcast(
        self, broadcast_mock
    ):
        # 普通 Celery 广播入口只负责 QUEUED 首次发送；PENDING_CHAIN 重播必须走
        # poller 的超时收口路径，避免重复消息绕过重播间隔。
        from evm.tasks import _broadcast_evm_task

        tx_task = self._create_evm_task(
            tx_hash="0x" + "aa" * 32,
            status=TxTaskStatus.PENDING_CHAIN,
        )

        _broadcast_evm_task.run(tx_task.pk)

        broadcast_mock.assert_not_called()

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_dispatch_due_evm_tx_tasks_dispatches_only_queued_unknown_tasks(
        self, delay_mock
    ):
        # dispatch 只放行 QUEUED 任务；PENDING_CHAIN / recent / finalized 不应被选中。
        from evm.tasks import dispatch_evm_tx_tasks

        other_addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=2,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f4"
            ),
        )

        due_queued = self._create_evm_task(
            tx_hash="0x" + "b" * 64,
            status=TxTaskStatus.QUEUED,
        )
        # PENDING_CHAIN 任务不应被 dispatch 重新选中（已在 mempool 中等待确认）。
        self._create_evm_task(
            tx_hash="0x" + "c" * 64,
            status=TxTaskStatus.PENDING_CHAIN,
            address=other_addr,
        )
        recent_task = self._create_evm_task(
            tx_hash="0x" + "d" * 64,
            status=TxTaskStatus.QUEUED,
        )
        finalized_task = self._create_evm_task(
            tx_hash="0x" + "e" * 64,
            status=TxTaskStatus.CONFIRMED,
        )

        stale_created_at = timezone.now() - timedelta(seconds=8)
        fresh_created_at = timezone.now()
        EvmTxTask.objects.filter(pk=due_queued.pk).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )
        EvmTxTask.objects.filter(pk=recent_task.pk).update(
            created_at=fresh_created_at,
            last_attempt_at=None,
        )
        EvmTxTask.objects.filter(pk=finalized_task.pk).update(
            created_at=stale_created_at,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        self.assertEqual(
            {call.args[0] for call in delay_mock.call_args_list},
            {due_queued.pk},
        )

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_tx_task_skips_when_lower_queued_nonce_exists(
        self,
        broadcast_mock,
    ):
        # 同账户更高 nonce 在更低 QUEUED nonce 存在时不应越过广播，保证 nonce 按顺序进入 mempool。
        from evm.tasks import _broadcast_evm_task

        self._create_evm_task(
            tx_hash="0x" + "1" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=1,
        )
        higher_task = self._create_evm_task(
            tx_hash="0x" + "2" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=2,
        )

        _broadcast_evm_task.run(higher_task.pk)

        broadcast_mock.assert_not_called()

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_tx_task_allows_higher_nonce_after_lower_task_enters_pending_confirm(
        self,
        broadcast_mock,
    ):
        # 一旦更低 nonce 已被链上观察到并进入 PENDING_CONFIRM，说明该 nonce 已消费，不应继续阻断后续 nonce。
        from evm.tasks import _broadcast_evm_task

        self._create_evm_task(
            tx_hash="0x" + "11" * 32,
            status=TxTaskStatus.PENDING_CONFIRM,
            nonce=1,
        )
        higher_task = self._create_evm_task(
            tx_hash="0x" + "12" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=2,
        )

        _broadcast_evm_task.run(higher_task.pk)

        broadcast_mock.assert_called_once()

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_dispatch_due_evm_tx_tasks_dispatches_only_lowest_queued_nonce_per_account(
        self, delay_mock
    ):
        # 队列层只应放行每个账户当前最小 QUEUED nonce，避免高 nonce 在前序缺口存在时被反复重试。
        from evm.tasks import dispatch_evm_tx_tasks

        other_addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=1,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f3"
            ),
        )
        lower_task = self._create_evm_task(
            tx_hash="0x" + "3" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=5,
        )
        blocked_higher_task = self._create_evm_task(
            tx_hash="0x" + "4" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=6,
        )
        other_account_task = self._create_evm_task(
            tx_hash="0x" + "5" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=1,
            address=other_addr,
        )

        stale_created_at = timezone.now() - timedelta(seconds=8)
        EvmTxTask.objects.filter(
            pk__in=[lower_task.pk, blocked_higher_task.pk, other_account_task.pk]
        ).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        self.assertEqual(
            {call.args[0] for call in delay_mock.call_args_list},
            {lower_task.pk, other_account_task.pk},
        )

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_dispatch_due_evm_tx_tasks_treats_pending_confirm_as_nonce_consumed(
        self,
        delay_mock,
    ):
        # SQL 选取最小阻塞 nonce 时，不应把已进入 PENDING_CONFIRM 的前序任务继续当作缺口。
        from evm.tasks import dispatch_evm_tx_tasks

        lower_confirming_task = self._create_evm_task(
            tx_hash="0x" + "13" * 32,
            status=TxTaskStatus.PENDING_CONFIRM,
            nonce=1,
        )
        higher_task = self._create_evm_task(
            tx_hash="0x" + "14" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=2,
        )

        stale_created_at = timezone.now() - timedelta(seconds=8)
        stale_attempt_at = timezone.now() - timedelta(minutes=5)
        EvmTxTask.objects.filter(pk=lower_confirming_task.pk).update(
            created_at=stale_created_at,
            last_attempt_at=stale_attempt_at,
        )
        EvmTxTask.objects.filter(pk=higher_task.pk).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        self.assertEqual(
            [call.args[0] for call in delay_mock.call_args_list],
            [higher_task.pk],
        )

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_dispatch_due_evm_tx_tasks_avoids_slice_starvation_from_blocked_high_nonces(
        self, delay_mock
    ):
        # SQL 层应直接挑每账户最小未收口 nonce，避免更高 nonce 候选占满 slice 后被 Python 层全部跳过。
        from evm.tasks import dispatch_evm_tx_tasks

        other_addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=3,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f5"
            ),
        )
        lower_task = self._create_evm_task(
            tx_hash="0x" + "6" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=1,
        )
        blocked_tasks = [
            self._create_evm_task(
                tx_hash=f"0x{i:064x}",
                status=TxTaskStatus.QUEUED,
                nonce=i,
            )
            for i in range(2, 10)
        ]
        other_account_task = self._create_evm_task(
            tx_hash="0x" + "7" * 64,
            status=TxTaskStatus.QUEUED,
            nonce=1,
            address=other_addr,
        )

        older_created_at = timezone.now() - timedelta(seconds=12)
        stale_created_at = timezone.now() - timedelta(seconds=8)
        EvmTxTask.objects.filter(pk=lower_task.pk).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )
        EvmTxTask.objects.filter(pk__in=[task.pk for task in blocked_tasks]).update(
            created_at=older_created_at,
            last_attempt_at=None,
        )
        EvmTxTask.objects.filter(pk=other_account_task.pk).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        self.assertEqual(
            {call.args[0] for call in delay_mock.call_args_list},
            {lower_task.pk, other_account_task.pk},
        )

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_clear_singleton_locks_allows_queue_dispatch_after_stale_lock(
        self,
        delay_mock,
    ):
        # singleton 锁残留会让队列任务直接返回；测试夹具必须主动清理，避免用例依赖外部缓存状态。
        from evm.tasks import dispatch_evm_tx_tasks

        cache.set(self.queue_lock_key, "true", 60)
        due_task = self._create_evm_task(
            tx_hash="0x" + "f" * 64,
            status=TxTaskStatus.QUEUED,
        )
        EvmTxTask.objects.filter(pk=due_task.pk).update(
            created_at=timezone.now() - timedelta(seconds=8),
            last_attempt_at=None,
        )

        self._clear_singleton_locks()
        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        self.assertEqual(
            [call.args[0] for call in delay_mock.call_args_list],
            [due_task.pk],
        )

    # ── Nonce 流水线测试 ──────────────────────────────────────────────

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_broadcast_allows_when_lower_nonce_is_pending_chain(
        self,
        broadcast_mock,
    ):
        # 低 nonce 已提交到 mempool (PENDING_CHAIN) 时，高 nonce 允许广播。
        from evm.tasks import _broadcast_evm_task

        self._create_evm_task(
            tx_hash="0x" + "a1" * 32,
            status=TxTaskStatus.PENDING_CHAIN,
            nonce=1,
        )
        higher_task = self._create_evm_task(
            tx_hash="0x" + "a2" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=2,
        )

        _broadcast_evm_task.run(higher_task.pk)

        broadcast_mock.assert_called_once()

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_broadcast_blocks_when_pipeline_full(
        self,
        broadcast_mock,
    ):
        # 同地址同链 PENDING_CHAIN 达到 EVM_PIPELINE_DEPTH 时阻断新广播。
        from evm.constants import EVM_PIPELINE_DEPTH
        from evm.tasks import _broadcast_evm_task

        for i in range(EVM_PIPELINE_DEPTH):
            self._create_evm_task(
                tx_hash=f"0x{i:064x}",
                status=TxTaskStatus.PENDING_CHAIN,
                nonce=i,
            )
        next_task = self._create_evm_task(
            tx_hash="0x" + "b1" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=EVM_PIPELINE_DEPTH,
        )

        _broadcast_evm_task.run(next_task.pk)

        broadcast_mock.assert_not_called()

    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_broadcast_resumes_after_pipeline_slot_freed(
        self,
        broadcast_mock,
    ):
        # pipeline 有空位后恢复广播。
        from evm.constants import EVM_PIPELINE_DEPTH
        from evm.tasks import _broadcast_evm_task

        pending_tasks = [
            self._create_evm_task(
                tx_hash=f"0x{i:064x}",
                status=TxTaskStatus.PENDING_CHAIN,
                nonce=i,
            )
            for i in range(EVM_PIPELINE_DEPTH)
        ]
        next_task = self._create_evm_task(
            tx_hash="0x" + "c1" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=EVM_PIPELINE_DEPTH,
        )

        # 模拟一笔完成，腾出 pipeline 空位
        first = pending_tasks[0]
        TxTask.objects.filter(pk=first.base_task_id).update(
            status=TxTaskStatus.CONFIRMED,
        )

        _broadcast_evm_task.run(next_task.pk)

        broadcast_mock.assert_called_once()

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_dispatch_allows_queued_when_pipeline_has_room(self, delay_mock):
        # 同地址已有 PENDING_CHAIN 但未满时，dispatch 仍放行最低 QUEUED nonce。
        from evm.tasks import dispatch_evm_tx_tasks

        self._create_evm_task(
            tx_hash="0x" + "d1" * 32,
            status=TxTaskStatus.PENDING_CHAIN,
            nonce=0,
        )
        queued_task = self._create_evm_task(
            tx_hash="0x" + "d2" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=1,
        )

        stale_created_at = timezone.now() - timedelta(seconds=8)
        EvmTxTask.objects.filter(pk=queued_task.pk).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        self.assertEqual(
            [call.args[0] for call in delay_mock.call_args_list],
            [queued_task.pk],
        )

    @patch("evm.tasks._broadcast_evm_task.delay")
    def test_dispatch_blocks_when_pipeline_full(self, delay_mock):
        # pipeline 已满时 dispatch 不选该地址的 QUEUED 任务。
        from evm.constants import EVM_PIPELINE_DEPTH
        from evm.tasks import dispatch_evm_tx_tasks

        for i in range(EVM_PIPELINE_DEPTH):
            self._create_evm_task(
                tx_hash=f"0x{0xE0 + i:064x}",
                status=TxTaskStatus.PENDING_CHAIN,
                nonce=i,
            )
        blocked_task = self._create_evm_task(
            tx_hash="0x" + "e1" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=EVM_PIPELINE_DEPTH,
        )

        stale_created_at = timezone.now() - timedelta(seconds=8)
        EvmTxTask.objects.filter(pk=blocked_task.pk).update(
            created_at=stale_created_at,
            last_attempt_at=None,
        )

        with self.captureOnCommitCallbacks(execute=True):
            dispatch_evm_tx_tasks.run()

        delay_mock.assert_not_called()

    @patch("evm.tasks._broadcast_evm_task.delay")
    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_chain_dispatch_triggers_next_queued_after_broadcast(
        self,
        broadcast_mock,
        delay_mock,
    ):
        # 广播成功后应链式调度同地址下一个 QUEUED nonce，无需等待下一轮 dispatch 周期。
        from evm.tasks import _broadcast_evm_task

        current_task = self._create_evm_task(
            tx_hash="0x" + "f1" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=0,
        )
        next_task = self._create_evm_task(
            tx_hash="0x" + "f2" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=1,
        )

        def mark_pending(*args, **kwargs):
            TxTask.objects.filter(pk=current_task.base_task_id).update(
                status=TxTaskStatus.PENDING_CHAIN,
            )

        broadcast_mock.side_effect = mark_pending

        _broadcast_evm_task.run(current_task.pk)

        broadcast_mock.assert_called_once()
        delay_mock.assert_called_once_with(next_task.pk)

    @patch("evm.tasks._broadcast_evm_task.delay")
    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_chain_dispatch_skips_when_current_task_remains_queued(
        self,
        broadcast_mock,
        delay_mock,
    ):
        # pre-flight 阻断会让当前任务保持 QUEUED 并依赖 last_attempt_at 节流；
        # 链式调度不能立刻把同一个最低 nonce 再投递一次。
        from evm.tasks import _broadcast_evm_task

        current_task = self._create_evm_task(
            tx_hash="0x" + "f5" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=0,
        )
        self._create_evm_task(
            tx_hash="0x" + "f6" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=1,
        )

        _broadcast_evm_task.run(current_task.pk)

        broadcast_mock.assert_called_once()
        delay_mock.assert_not_called()

    @patch("evm.tasks._broadcast_evm_task.delay")
    @patch("evm.tasks.EvmTxTask.broadcast")
    def test_chain_dispatch_stops_when_pipeline_full(
        self,
        broadcast_mock,
        delay_mock,
    ):
        # pipeline 满时链式调度不应继续派发。
        from evm.constants import EVM_PIPELINE_DEPTH
        from evm.tasks import _broadcast_evm_task

        # 创建 EVM_PIPELINE_DEPTH - 1 个已在 mempool 的任务
        for i in range(EVM_PIPELINE_DEPTH - 1):
            self._create_evm_task(
                tx_hash=f"0x{0xF0 + i:064x}",
                status=TxTaskStatus.PENDING_CHAIN,
                nonce=i,
            )
        # 当前任务广播后 pipeline 刚好满
        current_task = self._create_evm_task(
            tx_hash="0x" + "f3" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=EVM_PIPELINE_DEPTH - 1,
        )
        # 还有一个排队中的任务
        self._create_evm_task(
            tx_hash="0x" + "f4" * 32,
            status=TxTaskStatus.QUEUED,
            nonce=EVM_PIPELINE_DEPTH,
        )

        def mark_pending(*args, **kwargs):
            TxTask.objects.filter(pk=current_task.base_task_id).update(
                status=TxTaskStatus.PENDING_CHAIN,
            )

        broadcast_mock.side_effect = mark_pending

        _broadcast_evm_task.run(current_task.pk)

        broadcast_mock.assert_called_once()
        # pipeline 满，不应链式调度下一个
        delay_mock.assert_not_called()
