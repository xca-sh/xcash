"""worker 进程的运行时组件：worker 专用 App 的拼装，与任务硬时限看门狗。

为什么 worker 要单独拼一个 App：
    全局 app（procrastinate.contrib.django.app）的连接器是 DjangoConnector，入队 SQL 跑在
    调用方的 Django 连接上、与业务同事务（见 common.jobs）。但 DjangoConnector 不支持
    LISTEN，worker 取任务必须用 PsycopgConnector。官方 `manage.py procrastinate worker`
    的做法是把全局 app 的连接器整个换掉，于是任务代码里的 defer() 改走 worker 的异步
    连接池、自动提交，脱离任务自身的 ORM 事务——process() 里入队的确认任务可能在
    process() 提交前就开始执行。所以 worker 另建一个 App：只换连接器，任务注册表与
    周期注册表直接共享全局 app 的同一份。worker 用它取任务、回写状态、LISTEN；任务对象
    仍回指全局 app，任务内入队与周期投递都走 Django 连接。

为什么需要看门狗：
    同步任务在线程里执行，线程无法从外部终止，Procrastinate 也没有任务超时。批处理靠
    协作式预算、商户 Webhook 靠函数内总时限各自收尾，剩下的只有真正的挂死（典型如
    getaddrinfo 在 C 层阻塞、DB 锁等待）。看门狗发现任一任务超过 JOB_HARD_LIMIT_SECONDS，
    就打印全部线程栈、记 error 并整体退出进程（crash-only，与 chain-runner 一致），由容器
    restart 拉起；被中断的在途任务由 retry_stalled_jobs 回收重跑。代价是同进程其他在途
    任务被一并中断，它们同样会被回收，所以任务必须幂等。
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

import structlog
from procrastinate import App

if TYPE_CHECKING:
    from procrastinate.connector import BaseAsyncConnector
    from procrastinate.job_context import JobContext
    from procrastinate.worker import Worker

logger = structlog.get_logger()

# 单个任务的硬上限。最慢的批处理是「预算 + 一个单元」：reap 200s + Tron 终验约 50s；
# scan_operational_risks 按「每条链每个 sender 一组」打资源 RPC：EVM 一组一两次，Tron
# 一组至多 1 + 16 次（core.monitoring.TRON_RESOURCE_ESTIMATE_SAMPLE），与待发任务积压
# 笔数无关。300s 覆盖全部现实路径，只有真正的挂死才会触发。compose 的
# stop_grace_period 必须大于它。
JOB_HARD_LIMIT_SECONDS = 300
WATCHDOG_INTERVAL_SECONDS = 5
WORKER_NAME = "xcash-worker"


def terminate_process(code: int) -> None:
    """立即结束进程：线程无法被外部终止，只能整体退出。先刷出日志再 os._exit。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


class RunningJobs:
    """进程内在途任务的登记表：job_id → (任务名, 开始时刻)。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.jobs: dict[int, tuple[str, float]] = {}

    def start(self, job_id: int, task_name: str) -> None:
        with self.lock:
            self.jobs[job_id] = (task_name, self.clock())

    def finish(self, job_id: int) -> None:
        with self.lock:
            self.jobs.pop(job_id, None)

    def snapshot(self) -> list[tuple[int, str, float]]:
        with self.lock:
            return [
                (job_id, task_name, started)
                for job_id, (task_name, started) in self.jobs.items()
            ]


running_jobs = RunningJobs()


async def watch_job(
    call_next: Callable[[], Any],
    context: JobContext,
    worker: Worker,  # noqa: ARG001
) -> Any:
    """worker 中间件：任务执行期间登记到 running_jobs，结束（含抛异常）即注销。

    用 worker 中间件而不是任务中间件：它在事件循环上包住整个任务执行，同步与异步任务
    都覆盖；同步任务的线程挂死时，这里的 await 一直不返回，登记就一直在。
    """
    job_id = context.job.id
    running_jobs.start(job_id, context.job.task_name)
    try:
        return await call_next()
    finally:
        running_jobs.finish(job_id)


class JobWatchdog(threading.Thread):
    """后台线程：每隔 interval 检查一次在途任务，有任务超过硬上限即退出进程。"""

    def __init__(
        self,
        *,
        jobs: RunningJobs = running_jobs,
        hard_limit: float = JOB_HARD_LIMIT_SECONDS,
        interval: float = WATCHDOG_INTERVAL_SECONDS,
        exit_process: Callable[[int], None] = terminate_process,
    ):
        super().__init__(name="job-watchdog", daemon=True)
        self.jobs = jobs
        self.hard_limit = hard_limit
        self.interval = interval
        self.exit_process = exit_process

    def run(self) -> None:
        while True:
            time.sleep(self.interval)
            self.check()

    def check(self) -> bool:
        """有任务超过硬上限即判定挂死并退出进程；返回是否已触发。"""
        now = self.jobs.clock()
        for job_id, task_name, started in self.jobs.snapshot():
            elapsed = now - started
            if elapsed <= self.hard_limit:
                continue
            logger.error(
                "任务执行超过硬上限，判定线程挂死，退出进程等待重启",
                job_id=job_id,
                task=task_name,
                elapsed_seconds=round(elapsed),
                hard_limit_seconds=self.hard_limit,
            )
            # 打印全部线程栈，事后据此定位卡在哪个调用上。
            faulthandler.dump_traceback(all_threads=True)
            self.exit_process(1)
            return True
        return False


class WorkerApp(App):
    """worker 专用 App：任务与周期注册表取自全局 app，不自行登记任何任务。"""

    def _register_builtin_tasks(self) -> None:  # noqa: PLW3201
        # 覆盖 Procrastinate 的构造钩子：内置任务的 blueprint 是模块级单例，再登记一遍会
        # 给共享的任务对象重复加命名空间。注册表整体来自全局 app，其中已含内置任务。
        return


def build_worker_app(source_app: App, connector: BaseAsyncConnector) -> App:
    """拼装 worker 专用 App：共享 source_app 的任务、周期注册表与默认配置，只换连接器。

    worker 默认配置在 source_app 的基础上追加看门狗中间件。DjangoApp 已在 task_middleware
    里放入每个任务前后 close_old_connections 的清理中间件，这里只追加、不覆盖；启动 worker
    时也不能再传 task_middleware / worker_middleware，否则会整体替换掉这些默认值。
    成功的任务完成即删除（表里只留 todo / doing / failed），与此前只保留失败结果一致。
    """
    defaults = dict(source_app.worker_defaults)
    defaults["worker_middleware"] = [watch_job, *defaults.get("worker_middleware", [])]
    defaults["delete_jobs"] = "successful"
    defaults["name"] = WORKER_NAME
    worker_app = WorkerApp(
        connector=connector,
        worker_defaults=defaults,
        periodic_defaults=source_app.periodic_defaults,
    )
    worker_app.tasks = source_app.tasks
    worker_app.periodic_registry = source_app.periodic_registry
    return worker_app
