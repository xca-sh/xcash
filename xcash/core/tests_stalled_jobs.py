"""worker 失联后遗留的 doing 任务回收：事件任务延迟重排，周期任务与毒任务置为失败。"""

import asyncio
import json
from types import SimpleNamespace

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from procrastinate import PsycopgConnector
from procrastinate.contrib.django import app
from procrastinate.contrib.django import connector_params
from procrastinate.contrib.django.models import ProcrastinateJob

from common.job_runtime import build_worker_app
from core.tasks import STALLED_JOB_MAX_ATTEMPTS
from core.tasks import STALLED_JOB_RETRY_DELAY_SECONDS
from core.tasks import retry_stalled_jobs


def insert_worker(*, seconds_since_heartbeat: int) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO procrastinate_workers (last_heartbeat) "
            "VALUES (now() - make_interval(secs => %s)) RETURNING id",
            [seconds_since_heartbeat],
        )
        return cursor.fetchone()[0]


def insert_job(
    *,
    task_name: str,
    status: str,
    worker_id: int | None = None,
    attempts: int = 0,
    lock: str | None = None,
    queueing_lock: str | None = None,
) -> int:
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO procrastinate_jobs (queue_name, task_name, lock, queueing_lock, "
            "args, status, attempts, worker_id) "
            "VALUES ('default', %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            [
                task_name,
                lock,
                queueing_lock,
                json.dumps({}),
                status,
                attempts,
                worker_id,
            ],
        )
        return cursor.fetchone()[0]


class RetryStalledJobsTests(TransactionTestCase):
    def setUp(self):
        # 周期任务登记在 PROCRASTINATE_IMPORT_PATHS 里，回收任务据此区分周期与事件任务。
        app.perform_import_paths()

    def run_recovery(self) -> None:
        worker_app = build_worker_app(
            app, PsycopgConnector(kwargs=connector_params(), min_size=1, max_size=2)
        )

        async def run():
            async with worker_app.open_async():
                await retry_stalled_jobs(SimpleNamespace(app=worker_app))

        asyncio.run(run())

    def test_jobs_of_lost_worker_are_recovered_by_kind(self):
        dead = insert_worker(seconds_since_heartbeat=3600)
        alive = insert_worker(seconds_since_heartbeat=0)
        event = insert_job(
            task_name="chains.process_transfer",
            status="doing",
            worker_id=dead,
            queueing_lock="chains.process_transfer:1",
        )
        periodic = insert_job(
            task_name="chains.fallback_process_transfer",
            status="doing",
            worker_id=dead,
            lock="chains.fallback_process_transfer",
            queueing_lock="chains.fallback_process_transfer",
        )
        poison = insert_job(
            task_name="chains.confirm_transfer",
            status="doing",
            worker_id=dead,
            attempts=STALLED_JOB_MAX_ATTEMPTS,
        )
        replaced = insert_job(
            task_name="webhooks.deliver_event",
            status="doing",
            worker_id=dead,
            queueing_lock="webhooks.deliver_event:7",
        )
        replacement = insert_job(
            task_name="webhooks.deliver_event",
            status="todo",
            queueing_lock="webhooks.deliver_event:7",
        )
        running = insert_job(
            task_name="chains.process_transfer", status="doing", worker_id=alive
        )

        before = timezone.now()
        self.run_recovery()
        jobs = {job.pk: job for job in ProcrastinateJob.objects.all()}

        # 事件任务：延迟重新排队，计一次尝试。
        self.assertEqual(jobs[event].status, "todo")
        self.assertEqual(jobs[event].attempts, 1)
        delay = (jobs[event].scheduled_at - before).total_seconds()
        self.assertGreaterEqual(delay, STALLED_JOB_RETRY_DELAY_SECONDS - 1)
        # 周期任务：置为失败以释放执行锁，下个周期照常执行。
        self.assertEqual(jobs[periodic].status, "failed")
        # 反复 stalled 的毒任务不再重排。
        self.assertEqual(jobs[poison].status, "failed")
        # 已有新的待执行任务接替：旧任务置为失败，新任务不受影响。
        self.assertEqual(jobs[replaced].status, "failed")
        self.assertEqual(jobs[replacement].status, "todo")
        # 心跳正常的 worker 上的任务不动。
        self.assertEqual(jobs[running].status, "doing")
