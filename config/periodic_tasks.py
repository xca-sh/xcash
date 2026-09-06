"""可合并周期入口的唯一清单；登记后由路由和 Redis transport 实施合并。

入口使用普通 shared_task(ignore_result=True)，不需要自定义任务基类。
参数化子任务和独立业务消息不能加入。
"""

WORKER_HEALTH_TASK_GROUPS = {
    "core.tasks.report_business_worker_health": "celery",
    "core.tasks.report_scan_worker_health": "scan",
}

PERIODIC_TASK_GROUPS = {
    "evm.tasks.dispatch_evm_tx_tasks": "celery",
    "tron.tasks.dispatch_tron_tx_tasks": "celery",
    "evm.tasks.poll_active_evm_chains": "celery",
    "evm.tasks.scan_active_evm_chains": "scan",
    "tron.tasks.scan_active_tron_chains": "scan",
    **WORKER_HEALTH_TASK_GROUPS,
}

PERIODIC_TASK_QUEUES = {name: f"periodic.{name}" for name in PERIODIC_TASK_GROUPS}
PERIODIC_QUEUE_TASKS = {queue: name for name, queue in PERIODIC_TASK_QUEUES.items()}
