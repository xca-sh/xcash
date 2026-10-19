"""chain-runner：按链锁互斥、循环启停与链集合对齐、看门狗、锁失效与单轮容错。"""

import threading
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

import psycopg
from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone

from chains.constants import ChainCode
from chains.models import Chain
from chains.runner import SCAN_ROUND_HARD_LIMIT_SECONDS
from chains.runner import ChainLocks
from chains.runner import ChainLoop
from chains.runner import ChainRunner
from chains.runner import scannable_chains
from chains.tests_fixtures import make_evm_chain
from chains.tests_fixtures import make_tron_chain


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
        self.round_started_at = None
        self.alive = False
        self.joined = False

    def start(self):
        self.alive = True

    def stop(self):
        self.stop_event.set()

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
    def test_watchdog_exits_when_a_round_exceeds_hard_limit(self, dump_traceback):
        self.runner.reconcile([self.evm])
        loop = self.runner.loops[self.evm.pk]

        loop.round_started_at = self.now - SCAN_ROUND_HARD_LIMIT_SECONDS
        self.assertFalse(self.runner.check_watchdog())
        loop.round_started_at = None
        self.assertFalse(self.runner.check_watchdog())
        self.exit_process.assert_not_called()

        loop.round_started_at = self.now - SCAN_ROUND_HARD_LIMIT_SECONDS - 1
        self.assertTrue(self.runner.check_watchdog())
        self.exit_process.assert_called_once_with(1)
        dump_traceback.assert_called_once_with(all_threads=True)


class ChainLoopRoundTests(TestCase):
    def setUp(self):
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.stale = timezone.now() - timedelta(hours=1)
        Chain.objects.filter(pk=self.chain.pk).update(last_scanned_at=self.stale)
        self.scan_round = Mock()
        family = patch(
            "chains.runner.get_chain_family",
            return_value=SimpleNamespace(scan_round=self.scan_round),
        )
        family.start()
        self.addCleanup(family.stop)

    def last_scanned_at(self):
        return Chain.objects.get(pk=self.chain.pk).last_scanned_at

    def test_unexpected_error_is_contained_and_still_marks_scanned(self):
        self.scan_round.side_effect = RuntimeError("scanner exploded")
        loop = ChainLoop(self.chain)

        loop.run_once()

        self.assertGreater(self.last_scanned_at(), self.stale)
        self.assertIsNone(loop.round_started_at)

    def test_mark_scanned_failure_does_not_escape_round(self):
        with patch.object(Chain, "mark_scanned", side_effect=DatabaseError("db down")):
            ChainLoop(self.chain).run_once()
        self.scan_round.assert_called_once()

    def test_deactivated_chain_is_not_scanned_nor_marked(self):
        loop = ChainLoop(self.chain)
        Chain.objects.filter(pk=self.chain.pk).update(active=False)

        loop.run_once()

        self.scan_round.assert_not_called()
        self.assertEqual(self.last_scanned_at(), self.stale)

    def test_round_is_given_a_future_deadline(self):
        with patch("chains.runner.scan_round_deadline", return_value=12345.0):
            ChainLoop(self.chain).run_once()
        self.scan_round.assert_called_once()
        self.assertEqual(self.scan_round.call_args.args[1], 12345.0)


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
