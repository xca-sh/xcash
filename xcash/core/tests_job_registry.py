"""worker 启动时的任务注册与周期登记；worker 不认识的任务名会让任务以 TaskNotFound 落为失败。"""

import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 不在 tasks 模块里、只靠 PROCRASTINATE_IMPORT_PATHS 登记的任务最容易漏注册。
EXPECTED_TASKS = {
    "aml.screen_transfer_aml",
    "chains.confirm_transfer",
    "chains.process_transfer",
    "chains.schedule_vault_slot_deploy",
    "common.deliver_saas_callback",
    "evm.report_vault_slot_collect_gas_fee",
    "evm.report_vault_slot_deploy_gas_fee",
    "tron.report_vault_slot_collect_gas_fee",
    "tron.report_vault_slot_deploy_gas_fee",
    "webhooks.deliver_event",
}


def load_worker_registry() -> dict:
    """在全新子进程里按 worker 的方式加载任务：pytest 进程早已导入过全部模块，测不出真实注册情况。"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, django;"
                "django.setup();"
                "from procrastinate.contrib.django import app;"
                "app.perform_import_paths();"
                "print(json.dumps({"
                "'tasks': sorted(app.tasks),"
                "'periodic': {pt.task.name: pt.configure_kwargs.get('lock')"
                " for pt in app.periodic_registry.periodic_tasks.values()},"
                "'queueing_locks': {pt.task.name: pt.configure_kwargs.get('queueing_lock')"
                " for pt in app.periodic_registry.periodic_tasks.values()},"
                "}))"
            ),
        ],
        check=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"},
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_worker_registers_all_tasks_and_periodic_jobs():
    registry = load_worker_registry()

    assert EXPECTED_TASKS - set(registry["tasks"]) == set()
    # 每个周期任务也必须是已注册任务，否则周期投递出的任务会落为失败。
    assert set(registry["periodic"]) - set(registry["tasks"]) == set()


def test_periodic_jobs_locks_prevent_overlap_without_deadlocking_recovery():
    """周期任务的锁配置直接决定互斥与死锁，属于影响业务行为的配置。

    - 全部周期任务都有同名 queueing_lock：停摆期间不积压。
    - retry_stalled_jobs 不能有 lock：它自己 stalled 时，带 lock 的实例会挡住全部后续实例，
      回收机制自身死锁。健康探针也不加 lock。
    - 其余周期任务都有同名 lock：同一时刻至多一个在执行。
    """
    registry = load_worker_registry()
    unlocked = {"core.retry_stalled_jobs", "core.report_worker_health"}

    assert registry["queueing_locks"] == {name: name for name in registry["periodic"]}
    assert registry["periodic"]["core.retry_stalled_jobs"] is None
    assert {
        name: lock
        for name, lock in registry["periodic"].items()
        if name not in unlocked
    } == {name: name for name in registry["periodic"] if name not in unlocked}
