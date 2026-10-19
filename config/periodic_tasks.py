"""可合并周期入口的唯一清单；登记后由路由和 Redis transport 实施合并。

入口使用普通 shared_task(ignore_result=True)，不需要自定义任务基类。
参数化子任务和独立业务消息不能加入。
"""

WORKER_HEALTH_TASK_GROUPS = {
    "core.tasks.report_business_worker_health": "celery",
}

PERIODIC_TASK_GROUPS = {
    **WORKER_HEALTH_TASK_GROUPS,
}

PERIODIC_TASK_QUEUES = {name: f"periodic.{name}" for name in PERIODIC_TASK_GROUPS}
PERIODIC_QUEUE_TASKS = {queue: name for name, queue in PERIODIC_TASK_QUEUES.items()}
