"""任务队列（Procrastinate，任务存在 PostgreSQL）的入队与重试约定。

入队一律走 defer_once，规则：

1. 在业务事务内调用，不用 on_commit。任务与业务数据同一个事务提交、同一个事务回滚：
   提交前 worker 看不到（插入触发的 NOTIFY 也只在提交时发出），回滚时一起消失，
   不存在「业务已提交、任务没发出去」的崩溃窗口。savepoint 回滚同样会丢弃任务。
2. 放在事务末尾、业务行锁之后调用。带 queueing_lock 的并发插入会在唯一索引上等待
   对方事务结束；若一个事务先锁业务行再插任务、另一个顺序相反，就可能死锁。
3. 任务内部互投（兜底派发、清理补派等）处于 autocommit，同样走 defer_once，
   每次入队都是一个独立的短事务。

全局 app 的连接器是 DjangoConnector：入队 SQL 跑在调用方线程的 Django 连接上，
这正是第 1 条成立的前提。worker 进程另建只用于取任务、回写状态的 App 实例
（见 common.job_runtime.build_worker_app），任务代码里的入队仍走任务自身的连接与事务。

所有任务必须幂等：worker 崩溃或被看门狗退出后，在途任务会被 retry_stalled_jobs
重新排队执行（at-least-once）。新增任务时同样要满足这一点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from django.db import transaction
from procrastinate.exceptions import AlreadyEnqueued
from procrastinate.retry import BaseRetryStrategy
from procrastinate.retry import RetryDecision

if TYPE_CHECKING:
    from procrastinate.jobs import Job
    from procrastinate.tasks import Task

# 取任务按 (priority desc, id asc) 排序。优先级只决定空出来的执行槽位先给谁，
# 不会抢占正在运行的任务。
# 健康探针：不能排在积压后面，否则积压期间心跳过期会被误判为 worker 停摆。
PRIORITY_HEALTH = 20
# 影响付款确认时延的事件任务（QUICK 确认、转账处理、Webhook / SaaS 回调）。
PRIORITY_URGENT = 10
PRIORITY_DEFAULT = 0
# 批处理与巡检：慢且可以晚一点做。
PRIORITY_BULK = -10


def defer_once(
    task: Task[Any, Any, Any],
    *,
    queueing_lock: str | None = None,
    priority: int | None = None,
    schedule_at: datetime | None = None,
    **task_kwargs: Any,
) -> int | None:
    """在调用方当前事务内入队，返回任务 id；同一 queueing_lock 已有待执行任务时跳过并返回 None。

    queueing_lock 是 status='todo' 上的部分唯一索引：同名至多一个待执行任务（含等待中的
    重试），执行中的不算。冲突时 INSERT 抛唯一约束错误，会把外层事务打成 aborted、后续
    SQL 全部失败，所以必须包一层 savepoint 把冲突隔离在内。

    priority / queueing_lock 为 None 时沿用任务定义上的默认值。
    """
    try:
        with transaction.atomic():
            return task.configure(
                queueing_lock=queueing_lock,
                priority=priority,
                schedule_at=schedule_at,
            ).defer(**task_kwargs)
    except AlreadyEnqueued:
        return None


@dataclass(frozen=True)
class BackoffTable(BaseRetryStrategy):
    """按表退避的重试策略：第 n 次失败（attempts 从 0 计）后等待 delays[n] 秒再试。

    表用尽或异常类型不在 retry_exceptions 内即不再重试，任务落为 failed。
    Procrastinate 自带的 RetryStrategy 只能表达 base^(attempts+1) 形式的指数退避，
    表达不了现有的 8·2ⁿ 与 SaaS 回调的阶梯表，故用查表。
    """

    delays: tuple[int, ...]
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,)

    def is_final_attempt(self, attempts: int) -> bool:
        """本次执行（已失败 attempts 次）失败后是否不会再重试。"""
        return attempts >= len(self.delays)

    def get_retry_decision(
        self,
        *,
        exception: BaseException,
        job: Job,
    ) -> RetryDecision | None:
        if self.is_final_attempt(job.attempts):
            return None
        if not isinstance(exception, self.retry_exceptions):
            return None
        return RetryDecision(retry_in={"seconds": self.delays[job.attempts]})
