from django.db import connection
from django.db import transaction
from django.test import SimpleTestCase
from django.test import TestCase
from django.utils import timezone
from procrastinate.contrib.django import app
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.jobs import Job

from common.jobs import PRIORITY_URGENT
from common.jobs import BackoffTable
from common.jobs import defer_once


@app.task(name="tests.common.jobs.noop")
def noop_task(*, value: int) -> None:
    return None


class DeferOnceTransactionTests(TestCase):
    """入队与业务数据同生共死：同一事务提交、同一事务回滚。"""

    def test_job_is_visible_with_given_kwargs_lock_and_priority(self):
        job_id = defer_once(
            noop_task,
            queueing_lock="tests.noop:1",
            priority=PRIORITY_URGENT,
            value=1,
        )

        job = ProcrastinateJob.objects.get(pk=job_id)
        self.assertEqual(job.task_name, "tests.common.jobs.noop")
        self.assertEqual(job.args, {"value": 1})
        self.assertEqual(job.queueing_lock, "tests.noop:1")
        self.assertEqual(job.priority, PRIORITY_URGENT)
        self.assertEqual(job.status, "todo")

    def test_rolled_back_transaction_discards_job(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            defer_once(noop_task, queueing_lock="tests.noop:2", value=2)
            raise RuntimeError

        self.assertFalse(
            ProcrastinateJob.objects.filter(queueing_lock="tests.noop:2").exists()
        )

    def test_duplicate_queueing_lock_is_skipped_without_aborting_outer_transaction(
        self,
    ):
        with transaction.atomic():
            first = defer_once(noop_task, queueing_lock="tests.noop:3", value=3)
            second = defer_once(noop_task, queueing_lock="tests.noop:3", value=3)
            # 冲突被 savepoint 隔离：外层事务仍可正常执行 SQL。
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                self.assertEqual(cursor.fetchone(), (1,))

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            ProcrastinateJob.objects.filter(queueing_lock="tests.noop:3").count(), 1
        )


def make_job(*, attempts: int) -> Job:
    return Job(
        id=1,
        queue="default",
        lock=None,
        queueing_lock=None,
        task_name="tests.common.jobs.noop",
        attempts=attempts,
    )


class BackoffTableTests(SimpleTestCase):
    def test_attempts_map_to_table_delays(self):
        strategy = BackoffTable((8, 60, 300))

        for attempts, expected in enumerate((8, 60, 300)):
            decision = strategy.get_retry_decision(
                exception=RuntimeError(), job=make_job(attempts=attempts)
            )
            wait = (decision.retry_at - timezone.now()).total_seconds()
            self.assertAlmostEqual(wait, expected, delta=2)

    def test_exhausted_table_stops_retrying(self):
        strategy = BackoffTable((8, 16))

        self.assertIsNone(
            strategy.get_retry_decision(
                exception=RuntimeError(), job=make_job(attempts=2)
            )
        )
        self.assertFalse(strategy.is_final_attempt(1))
        self.assertTrue(strategy.is_final_attempt(2))

    def test_only_listed_exceptions_are_retried(self):
        strategy = BackoffTable((8,), retry_exceptions=(ConnectionError,))

        self.assertIsNotNone(
            strategy.get_retry_decision(
                exception=ConnectionError(), job=make_job(attempts=0)
            )
        )
        self.assertIsNone(
            strategy.get_retry_decision(
                exception=ValueError(), job=make_job(attempts=0)
            )
        )
