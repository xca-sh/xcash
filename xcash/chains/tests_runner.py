"""chain-runner：按链锁互斥、循环启停与链集合对齐、看门狗、锁失效、单步容错与唤醒。"""

import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import psycopg
from django.db import DatabaseError
from django.db import transaction
from django.test import TestCase
from django.test import TransactionTestCase
from django.utils import timezone

from chains.constants import ChainCode
from chains.models import Chain
from chains.runner import CHAIN_STEP_HARD_LIMIT_SECONDS
from chains.runner import ChainLocks
from chains.runner import ChainLoop
from chains.runner import ChainRunner
from chains.runner import scannable_chains
from chains.scanning import SCAN_ROUND_BUDGET_SECONDS
from chains.scanning import TX_DISPATCH_BUDGET_SECONDS
from chains.scanning import TX_POLL_BUDGET_SECONDS
from chains.tests_fixtures import make_evm_chain
from chains.tests_fixtures import make_tron_chain
from chains.wakeup import notify_chain_tx_pending


class FakeLocks:
    def __init__(self, *, held_elsewhere=()):
        self.held = set()
        self.held_elsewhere = set(held_elsewhere)
        self.ping = Mock()
        self.closed = False

    def try_lock(self, chain_pk):
        if chain_pk in self.held_elsewhere:
            return False
        self.held.add(chain_pk)
        return True

    def unlock(self, chain_pk):
        self.held.discard(chain_pk)

    def close(self):
        self.closed = True


class FakeLoop:
    def __init__(self, chain):
        self.chain_pk = chain.pk
        self.chain_code = chain.code
        self.stop_event = threading.Event()
        self.step_started_at = None
        self.alive = False
        self.joined = False
        self.woken = 0

    def start(self):
        self.alive = True

    def stop(self):
        self.stop_event.set()

    def wake(self):
        self.woken += 1

    def is_alive(self):
        return self.alive

    def join(self):
        self.joined = True
        self.alive = False


class ChainLocksTests(TestCase):
    def test_same_chain_can_only_be_held_by_one_connection(self):
        first = ChainLocks()
        second = ChainLocks()
        try:
            self.assertTrue(first.try_lock(1))
            self.assertFalse(second.try_lock(1))
            # 不同链互不影响。
            self.assertTrue(second.try_lock(2))

            first.unlock(1)
            self.assertTrue(second.try_lock(1))
        finally:
            first.close()
            second.close()

    def test_closing_connection_releases_all_its_locks(self):
        """进程退出（连接断开）即释放，接管实例无需等待任何 TTL。"""
        first = ChainLocks()
        second = ChainLocks()
        try:
            self.assertTrue(first.try_lock(1))
            first.close()
            # 客户端断开后，PG 后端进程异步退出才释放锁；runner 靠下一个 tick 重试吸收
            # 这段延迟，这里同样轮询等待。
            deadline = time.monotonic() + 5
            while not second.try_lock(1):
                self.assertLess(time.monotonic(), deadline, "锁未随连接断开释放")
                time.sleep(0.05)
        finally:
            second.close()

    def test_ping_raises_once_connection_is_gone(self):
        locks = ChainLocks()
        locks.ping()
        locks.raw_connection.close()
        with self.assertRaises(psycopg.OperationalError):
            locks.ping()
        locks.close()


class ScannableChainsTests(TestCase):
    def test_only_active_chains_with_runtime_config_are_scanned(self):
        evm = make_evm_chain(code=ChainCode.Ethereum)
        tron = make_tron_chain(code=ChainCode.Tron)
        inactive = make_evm_chain(code=ChainCode.BSC, active=False)

        self.assertEqual(set(scannable_chains()), {evm, tron})

        Chain.objects.filter(pk=tron.pk).update(active=False, tron_api_key="")
        self.assertEqual(set(scannable_chains()), {evm})
        self.assertNotIn(inactive, set(scannable_chains()))


class ChainRunnerReconcileTests(TestCase):
    def setUp(self):
        self.evm = make_evm_chain(code=ChainCode.Ethereum)
        self.tron = make_tron_chain(code=ChainCode.Tron)
        self.locks = FakeLocks()
        self.runner = ChainRunner(loop_factory=FakeLoop)
        self.runner.locks = self.locks

    def test_starts_one_loop_per_chain_after_taking_its_lock(self):
        self.runner.reconcile([self.evm, self.tron])

        self.assertEqual(set(self.runner.loops), {self.evm.pk, self.tron.pk})
        self.assertTrue(all(loop.alive for loop in self.runner.loops.values()))
        self.assertEqual(self.locks.held, {self.evm.pk, self.tron.pk})

        # 重复对齐不会重复拉起。
        loops_before = dict(self.runner.loops)
        self.runner.reconcile([self.evm, self.tron])
        self.assertEqual(self.runner.loops, loops_before)

    def test_chain_locked_by_another_instance_is_not_started(self):
        self.locks.held_elsewhere = {self.tron.pk}

        self.runner.reconcile([self.evm, self.tron])

        self.assertEqual(set(self.runner.loops), {self.evm.pk})

        # 另一实例退出、锁被释放后，下一个 tick 接管。
        self.locks.held_elsewhere.clear()
        self.runner.reconcile([self.evm, self.tron])
        self.assertEqual(set(self.runner.loops), {self.evm.pk, self.tron.pk})

    def test_deactivated_chain_is_stopped_without_blocking_then_unlocked(self):
        self.runner.reconcile([self.evm, self.tron])
        tron_loop = self.runner.loops[self.tron.pk]

        self.runner.reconcile([self.evm])
        # 只置停止标志；线程还在跑完当前一轮时继续持锁，避免别的实例抢先接手。
        self.assertTrue(tron_loop.stop_event.is_set())
        self.assertIn(self.tron.pk, self.runner.loops)
        self.assertIn(self.tron.pk, self.locks.held)

        tron_loop.alive = False
        self.runner.reconcile([self.evm])
        self.assertNotIn(self.tron.pk, self.runner.loops)
        self.assertNotIn(self.tron.pk, self.locks.held)
        self.assertFalse(self.runner.loops[self.evm.pk].stop_event.is_set())

    def test_loop_that_died_unexpectedly_is_restarted(self):
        self.runner.reconcile([self.evm])
        dead = self.runner.loops[self.evm.pk]
        dead.alive = False

        self.runner.reconcile([self.evm])

        restarted = self.runner.loops[self.evm.pk]
        self.assertIsNot(restarted, dead)
        self.assertTrue(restarted.alive)
        self.assertIn(self.evm.pk, self.locks.held)

    def test_shutdown_waits_for_every_loop_and_closes_lock_connection(self):
        self.runner.reconcile([self.evm, self.tron])
        loops = list(self.runner.loops.values())

        self.runner.shutdown()

        self.assertTrue(all(loop.stop_event.is_set() for loop in loops))
        self.assertTrue(all(loop.joined for loop in loops))
        self.assertTrue(self.locks.closed)


class ChainRunnerTickTests(TestCase):
    def setUp(self):
        self.evm = make_evm_chain(code=ChainCode.Ethereum)
        self.locks = FakeLocks()
        self.exit_process = Mock()
        self.now = 1000.0
        self.runner = ChainRunner(
            loop_factory=FakeLoop,
            exit_process=self.exit_process,
            clock=lambda: self.now,
        )
        self.runner.locks = self.locks

    @patch("chains.runner.record_chain_runner_heartbeat")
    def test_tick_aligns_loops_and_records_heartbeat(self, heartbeat):
        self.runner.tick()

        self.assertEqual(set(self.runner.loops), {self.evm.pk})
        heartbeat.assert_called_once_with()
        self.exit_process.assert_not_called()

    @patch("chains.runner.record_chain_runner_heartbeat")
    def test_lost_lock_connection_exits_process_before_touching_loops(self, heartbeat):
        self.locks.ping.side_effect = DatabaseError("connection closed")

        self.runner.tick()

        self.exit_process.assert_called_once_with(1)
        self.assertEqual(self.runner.loops, {})
        heartbeat.assert_not_called()

    @patch("chains.runner.record_chain_runner_heartbeat")
    @patch("chains.runner.scannable_chains", side_effect=DatabaseError("db down"))
    def test_db_failure_skips_heartbeat_without_exiting(self, _chains, heartbeat):
        self.runner.tick()

        heartbeat.assert_not_called()
        self.exit_process.assert_not_called()

    @patch("chains.runner.faulthandler.dump_traceback")
    def test_watchdog_exits_when_a_step_exceeds_hard_limit(self, dump_traceback):
        self.runner.reconcile([self.evm])
        loop = self.runner.loops[self.evm.pk]

        loop.step_started_at = self.now - CHAIN_STEP_HARD_LIMIT_SECONDS
        self.assertFalse(self.runner.check_watchdog())
        loop.step_started_at = None
        self.assertFalse(self.runner.check_watchdog())
        self.exit_process.assert_not_called()

        loop.step_started_at = self.now - CHAIN_STEP_HARD_LIMIT_SECONDS - 1
        self.assertTrue(self.runner.check_watchdog())
        self.exit_process.assert_called_once_with(1)
        dump_traceback.assert_called_once_with(all_threads=True)


class ChainLoopRoundTests(TestCase):
    def setUp(self):
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.stale = timezone.now() - timedelta(hours=1)
        Chain.objects.filter(pk=self.chain.pk).update(last_scanned_at=self.stale)
        self.calls = []
        self.scan_round = Mock(side_effect=lambda *_: self.calls.append("scan"))
        self.poll_round = Mock(side_effect=lambda *_: self.calls.append("poll"))
        self.dispatch_round = Mock(side_effect=lambda *_: self.calls.append("dispatch"))
        family = patch(
            "chains.runner.get_chain_family",
            return_value=SimpleNamespace(
                scan_round=self.scan_round,
                poll_round=self.poll_round,
                dispatch_round=self.dispatch_round,
            ),
        )
        family.start()
        self.addCleanup(family.stop)

    def last_scanned_at(self):
        return Chain.objects.get(pk=self.chain.pk).last_scanned_at

    def test_round_runs_scan_then_poll_then_dispatch(self):
        ChainLoop(self.chain).run_once()

        self.assertEqual(self.calls, ["scan", "poll", "dispatch"])

    def test_failing_step_never_skips_the_steps_after_it(self):
        # 终局轮询曾挂在扫描之后、随扫描异常整段跳过，部署/归集因此永不终局。
        self.scan_round.side_effect = RuntimeError("scanner exploded")
        self.poll_round.side_effect = RuntimeError("poller exploded")
        self.dispatch_round.side_effect = RuntimeError("dispatcher exploded")
        loop = ChainLoop(self.chain)

        loop.run_once()

        self.scan_round.assert_called_once()
        self.poll_round.assert_called_once()
        self.dispatch_round.assert_called_once()
        self.assertGreater(self.last_scanned_at(), self.stale)
        self.assertIsNone(loop.step_started_at)

    def test_each_step_gets_its_own_budget_from_its_own_start(self):
        # 扫描落后链头时会把自己的预算用满，轮询与派发不能因此拿到已过期的截止时刻。
        with patch(
            "chains.runner.step_deadline",
            side_effect=lambda *, budget_seconds: budget_seconds,
        ) as deadline:
            ChainLoop(self.chain).run_once()

        self.assertEqual(
            [call.kwargs["budget_seconds"] for call in deadline.call_args_list],
            [
                SCAN_ROUND_BUDGET_SECONDS,
                TX_POLL_BUDGET_SECONDS,
                TX_DISPATCH_BUDGET_SECONDS,
            ],
        )
        self.assertEqual(self.scan_round.call_args.args[1], SCAN_ROUND_BUDGET_SECONDS)
        self.assertEqual(self.poll_round.call_args.args[1], TX_POLL_BUDGET_SECONDS)
        self.assertEqual(
            self.dispatch_round.call_args.args[1], TX_DISPATCH_BUDGET_SECONDS
        )

    def test_watchdog_clock_restarts_at_every_step(self):
        loop = ChainLoop(self.chain)
        started = {}

        def record(name):
            def step(*_):
                started[name] = loop.step_started_at
                time.sleep(0.01)

            return step

        self.scan_round.side_effect = record("scan")
        self.poll_round.side_effect = record("poll")
        self.dispatch_round.side_effect = record("dispatch")

        loop.run_once()

        # 看门狗按步计时：三步各自从零起算，整轮再长也不会被误判为挂死。
        self.assertLess(started["scan"], started["poll"])
        self.assertLess(started["poll"], started["dispatch"])
        self.assertIsNone(loop.step_started_at)

    def test_last_scanned_at_advances_before_poll_and_dispatch_run(self):
        seen = []
        self.dispatch_round.side_effect = lambda *_: seen.append(self.last_scanned_at())

        ChainLoop(self.chain).run_once()

        # last_scanned_at 表示「扫描完成于」，不能被同链派发的耗时推迟。
        self.assertGreater(seen[0], self.stale)

    def test_mark_scanned_failure_does_not_escape_round(self):
        with patch.object(Chain, "mark_scanned", side_effect=DatabaseError("db down")):
            ChainLoop(self.chain).run_once()
        self.assertEqual(self.calls, ["scan", "poll", "dispatch"])

    def test_deactivated_chain_runs_no_step_and_is_not_marked(self):
        loop = ChainLoop(self.chain)
        Chain.objects.filter(pk=self.chain.pk).update(active=False)

        loop.run_once()
        loop.run_dispatch_only()

        self.assertEqual(self.calls, [])
        self.assertEqual(self.last_scanned_at(), self.stale)

    def test_stop_takes_effect_between_steps(self):
        loop = ChainLoop(self.chain)
        self.scan_round.side_effect = lambda *_: loop.stop()

        loop.run_once()

        self.scan_round.assert_called_once()
        self.poll_round.assert_not_called()
        self.dispatch_round.assert_not_called()

    def test_dispatch_only_runs_just_the_dispatch_step(self):
        loop = ChainLoop(self.chain)

        loop.run_dispatch_only()

        self.assertEqual(self.calls, ["dispatch"])
        self.assertEqual(self.last_scanned_at(), self.stale)


class ChainLoopWakeTests(TestCase):
    def setUp(self):
        self.loop = ChainLoop(make_evm_chain(code=ChainCode.Ethereum))

    def test_wakes_coalesce_into_one(self):
        self.loop.wake()
        self.loop.wake()

        self.assertTrue(self.loop.wait_for_wake(until=time.monotonic() + 5))
        # 第二次唤醒已被合并，接下来只会等到截止时刻。
        self.assertFalse(self.loop.wait_for_wake(until=time.monotonic() + 0.05))

    def test_no_wake_returns_false_at_deadline(self):
        self.assertFalse(self.loop.wait_for_wake(until=time.monotonic() + 0.05))

    def test_stop_interrupts_wait_without_reporting_a_wake(self):
        threading.Timer(0.05, self.loop.stop).start()
        started = time.monotonic()

        self.assertFalse(self.loop.wait_for_wake(until=time.monotonic() + 60))
        self.assertLess(time.monotonic() - started, 5)


class ChainLoopThreadTests(TestCase):
    def test_stop_ends_loop_between_rounds(self):
        chain = make_evm_chain(code=ChainCode.Ethereum)
        loop = ChainLoop(chain)
        rounds = threading.Event()

        with patch.object(ChainLoop, "run_once", side_effect=rounds.set) as run_once:
            loop.start()
            self.assertTrue(rounds.wait(timeout=5))
            # 正在等扫描周期时收到停止：立即醒来退出，不再开下一轮。
            loop.stop()
            loop.thread.join(timeout=5)

        self.assertFalse(loop.is_alive())
        run_once.assert_called_once_with()

    def test_wake_runs_dispatch_only_without_advancing_the_round(self):
        chain = make_evm_chain(code=ChainCode.Ethereum)
        loop = ChainLoop(chain)
        loop.scan_interval_seconds = 60
        rounds = threading.Event()
        dispatched = threading.Event()

        with (
            patch.object(ChainLoop, "run_once", side_effect=rounds.set) as run_once,
            patch.object(
                ChainLoop, "run_dispatch_only", side_effect=dispatched.set
            ) as run_dispatch_only,
        ):
            loop.start()
            self.assertTrue(rounds.wait(timeout=5))
            loop.wake()
            self.assertTrue(dispatched.wait(timeout=5))
            loop.stop()
            loop.thread.join(timeout=5)

        self.assertFalse(loop.is_alive())
        # 唤醒只补跑派发，扫描仍按 scan_interval 节奏，不会被提前。
        run_once.assert_called_once_with()
        run_dispatch_only.assert_called_once_with()


class ChainRunnerWakeTests(TestCase):
    def test_notification_wakes_only_the_loop_of_that_chain(self):
        evm = make_evm_chain(code=ChainCode.Ethereum)
        tron = make_tron_chain(code=ChainCode.Tron)
        runner = ChainRunner(loop_factory=FakeLoop)
        runner.locks = FakeLocks()
        runner.reconcile([evm, tron])

        runner.wake(str(evm.pk))
        # 不在本实例运行的链（别的实例持锁、已停用）与非法 payload 都直接忽略。
        runner.wake("999999")
        runner.wake("not-a-pk")

        self.assertEqual(runner.loops[evm.pk].woken, 1)
        self.assertEqual(runner.loops[tron.pk].woken, 0)


class ChainWakeupNotifyTests(TransactionTestCase):
    """NOTIFY 只在真正提交时投递，必须用 TransactionTestCase：TestCase 的外层事务从不提交。"""

    def setUp(self):
        self.listener = ChainLocks()
        self.addCleanup(self.listener.close)

    def test_notification_is_delivered_only_after_commit(self):
        with transaction.atomic():
            notify_chain_tx_pending(42)
            self.assertEqual(self.listener.wait_for_notifies(0.2), [])

        self.assertEqual(self.listener.wait_for_notifies(5), ["42"])

    def test_rolled_back_notifications_are_discarded(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            notify_chain_tx_pending(1)
            raise RuntimeError("rollback")

        with transaction.atomic():
            notify_chain_tx_pending(2)
            savepoint = transaction.savepoint()
            notify_chain_tx_pending(3)
            transaction.savepoint_rollback(savepoint)

        self.assertEqual(self.listener.wait_for_notifies(5), ["2"])
        self.assertEqual(self.listener.wait_for_notifies(0.2), [])

    def test_notification_arriving_during_other_queries_is_not_lost(self):
        notify_chain_tx_pending(7)
        time.sleep(0.1)
        # 通知在锁连接执行别的语句时到达：psycopg 暂存，下次等待时最先返回。
        self.listener.ping()
        self.listener.try_lock(7)

        self.assertEqual(self.listener.wait_for_notifies(5), ["7"])

    def test_supervisor_wakes_the_notified_loop(self):
        runner = ChainRunner()
        runner.locks = self.listener
        loop = SimpleNamespace(wake=Mock())
        runner.loops = {42: loop}

        notify_chain_tx_pending(42)
        runner.wait_for_wakeups(0.5)

        loop.wake.assert_called_once_with()
