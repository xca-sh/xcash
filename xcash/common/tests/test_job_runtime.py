import asyncio
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.db import connection
from django.db import connections
from django.db import transaction
from django.test import SimpleTestCase
from django.test import TransactionTestCase
from procrastinate import PsycopgConnector
from procrastinate.contrib.django import app
from procrastinate.contrib.django import connector_params
from procrastinate.contrib.django.models import ProcrastinateJob
from procrastinate.periodic import PeriodicRegistry

from common.job_runtime import JobWatchdog
from common.job_runtime import RunningJobs
from common.job_runtime import build_worker_app
from common.job_runtime import running_jobs
from common.job_runtime import watch_job


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class JobWatchdogTests(SimpleTestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.jobs = RunningJobs(clock=self.clock)
        self.exit_process = Mock()
        self.watchdog = JobWatchdog(
            jobs=self.jobs, hard_limit=300, exit_process=self.exit_process
        )

    @patch("common.job_runtime.faulthandler.dump_traceback")
    def test_overdue_job_dumps_threads_and_exits(self, dump_traceback):
        self.jobs.start(1, "tests.slow")
        self.clock.now += 301

        self.assertTrue(self.watchdog.check())

        dump_traceback.assert_called_once_with(all_threads=True)
        self.exit_process.assert_called_once_with(1)

    def test_job_within_limit_does_not_trigger(self):
        self.jobs.start(1, "tests.slow")
        self.clock.now += 300

        self.assertFalse(self.watchdog.check())
        self.exit_process.assert_not_called()

    def test_idle_worker_does_not_trigger(self):
        self.clock.now += 10_000

        self.assertFalse(self.watchdog.check())
        self.exit_process.assert_not_called()

    def test_finished_job_is_no_longer_watched(self):
        self.jobs.start(1, "tests.slow")
        self.jobs.finish(1)
        self.clock.now += 301

        self.assertFalse(self.watchdog.check())

    def test_middleware_registers_while_running_and_unregisters_on_error(self):
        context = SimpleNamespace(job=SimpleNamespace(id=4242, task_name="tests.x"))
        seen = []

        async def failing_task():
            seen.extend(job_id for job_id, _, _ in running_jobs.snapshot())
            raise RuntimeError

        with self.assertRaises(RuntimeError):
            asyncio.run(watch_job(failing_task, context, worker=None))

        self.assertIn(4242, seen)
        self.assertNotIn(4242, [job_id for job_id, _, _ in running_jobs.snapshot()])


# marker 任务被 worker 执行时记下标签：成功的任务完成即删除，只能靠它观察是否真的入队并执行过。
executed_markers: list[str] = []


@app.task(name="tests.common.job_runtime.marker")
def marker_task(*, label: str) -> None:
    executed_markers.append(label)


@app.task(name="tests.common.job_runtime.defer_then_fail")
def defer_then_fail_task() -> None:
    try:
        with transaction.atomic():
            marker_task.defer(label="failed")
            raise RuntimeError("业务失败，事务回滚")
    finally:
        # 线程内的连接必须显式关闭，否则 TransactionTestCase 清库时会被占用的连接卡住。
        connections.close_all()


@app.task(name="tests.common.job_runtime.defer_then_succeed")
def defer_then_succeed_task() -> None:
    try:
        with transaction.atomic():
            marker_task.defer(label="succeeded")
    finally:
        connections.close_all()


class WorkerDeferTransactionTests(TransactionTestCase):
    """真实跑一个按 run_worker 方式拼装的 worker，验证任务内入队仍走任务自身的事务。

    官方 `procrastinate worker` 会把全局 app 的连接器换成 worker 的连接池，任务内的
    defer() 随之自动提交、脱离任务事务：任务失败回滚了业务数据，入队的任务却留了下来。
    """

    def setUp(self):
        executed_markers.clear()

    def tearDown(self):
        # Procrastinate 的表是 managed=False，TransactionTestCase 不会替它清空。
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM procrastinate_jobs")
            cursor.execute("DELETE FROM procrastinate_workers")
        super().tearDown()

    def run_worker_until_idle(self) -> None:
        worker_app = build_worker_app(
            app, PsycopgConnector(kwargs=connector_params(), min_size=1, max_size=2)
        )
        # 只验证事务语义，不让真实的周期任务混进来。
        worker_app.periodic_registry = PeriodicRegistry()

        async def run():
            async with worker_app.open_async():
                await worker_app.run_worker_async(
                    concurrency=1,
                    wait=False,
                    install_signal_handlers=False,
                    listen_notify=False,
                )

        asyncio.run(run())

    def test_job_deferred_inside_failed_task_is_rolled_back(self):
        defer_then_fail_task.defer()

        self.run_worker_until_idle()

        self.assertEqual(
            ProcrastinateJob.objects.get(
                task_name="tests.common.job_runtime.defer_then_fail"
            ).status,
            "failed",
        )
        self.assertEqual(executed_markers, [])
        self.assertFalse(
            ProcrastinateJob.objects.filter(
                task_name="tests.common.job_runtime.marker"
            ).exists()
        )

    def test_job_deferred_inside_successful_task_is_committed(self):
        defer_then_succeed_task.defer()

        self.run_worker_until_idle()

        # 外层任务提交后，它入队的 marker 被同一个 worker 取到并执行；成功的任务完成即删除。
        self.assertEqual(executed_markers, ["succeeded"])
        self.assertFalse(ProcrastinateJob.objects.exists())
