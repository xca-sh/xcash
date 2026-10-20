"""业务任务 worker 进程入口：执行 PG 队列里的任务并投递周期任务，直到收到 SIGTERM/SIGINT。

不用官方的 `manage.py procrastinate worker`：它会替换全局 app 的连接器，使任务内入队
脱离任务事务（见 common.job_runtime）。

进程结构（单进程）：
- 事件循环：LISTEN/轮询取任务、心跳、周期任务投递、中止轮询；
- 线程池：执行同步任务，并发由 --concurrency 的信号量控制；
- 看门狗线程：任一任务超过硬上限即打印线程栈并退出进程。

收到第一次 SIGTERM/SIGINT 后停止取新任务，等在途任务做完再退出；docker 的
stop_grace_period 到期后的 SIGKILL 是实际上限，被杀掉的任务由 retry_stalled_jobs 回收。
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import structlog
from django.core.management.base import BaseCommand
from procrastinate import PsycopgConnector
from procrastinate.contrib.django import app
from procrastinate.contrib.django.utils import connector_params

from common.job_runtime import JobWatchdog
from common.job_runtime import build_worker_app

logger = structlog.get_logger()

# worker 自己的连接池：取任务、回写状态、心跳。空闲时只占 1 条；LISTEN 另占 1 条独立连接。
# 任务代码的 ORM 与入队走各自线程的 Django 连接，不占这个池。
WORKER_POOL_MIN_SIZE = 1
WORKER_POOL_MAX_SIZE = 4
# 事件循环自身也会用默认线程池（如新建连接时的 DNS 解析），在任务线程之外留出余量，
# 任务把并发占满时循环的这些调用不至于排队。
LOOP_SPARE_THREADS = 2


async def serve(*, concurrency: int) -> None:
    loop = asyncio.get_running_loop()
    # 同步任务经 sync_to_async(thread_sensitive=False) 跑在事件循环的默认线程池里。默认池
    # 大小是 min(32, cpu+4)，小机器上可能低于并发数，造成「已取出（doing）却在排队」。
    loop.set_default_executor(
        ThreadPoolExecutor(
            max_workers=concurrency + LOOP_SPARE_THREADS,
            thread_name_prefix="job",
        )
    )
    # 导入 PROCRASTINATE_IMPORT_PATHS 里的模块，确保全部任务都已登记到全局 app。
    app.perform_import_paths()
    worker_app = build_worker_app(
        app,
        PsycopgConnector(
            kwargs=connector_params(),
            min_size=WORKER_POOL_MIN_SIZE,
            max_size=WORKER_POOL_MAX_SIZE,
        ),
    )
    JobWatchdog().start()
    async with worker_app.open_async():
        # Procrastinate 自身的 INFO 日志已按级别过滤，启动与退出在这里留痕。
        logger.info("业务任务 worker 已启动", concurrency=concurrency)
        await worker_app.run_worker_async(concurrency=concurrency)
    logger.info("业务任务 worker 已退出")


class Command(BaseCommand):
    help = "运行业务任务 worker（Procrastinate，PostgreSQL 任务队列）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--concurrency",
            type=int,
            required=True,
            help="同时执行的任务数（线程数）",
        )

    def handle(self, *args, **options):
        asyncio.run(serve(concurrency=options["concurrency"]))
