"""Celery worker 启动时的任务注册；worker 收到未注册的任务名会直接丢弃消息。"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

SAAS_GAS_FEE_RETRY_TASKS = [
    "evm.saas_gas_billing.retry_vault_slot_collect_gas_fee",
    "evm.saas_gas_billing.retry_vault_slot_deploy_gas_fee",
    "tron.saas_gas_billing.retry_vault_slot_collect_gas_fee",
    "tron.saas_gas_billing.retry_vault_slot_deploy_gas_fee",
]


def test_worker_default_modules_register_saas_gas_fee_retry_tasks():
    """worker 加载默认模块后必须注册全部 gas 计费重试任务，否则重试被丢弃、计费永久漏报。

    必须在全新子进程里检查：pytest 进程早已被其他测试导入过 saas_gas_billing，
    在本进程里看 app.tasks 永远是全的，测不出 worker 启动时的真实注册情况。
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django;"
                "django.setup();"
                "from config.celery import app;"
                "app.loader.import_default_modules();"
                "print('\\n'.join(sorted(app.tasks)))"
            ),
        ],
        check=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"},
        capture_output=True,
        text=True,
    )

    registered = set(completed.stdout.splitlines())
    missing = [name for name in SAAS_GAS_FEE_RETRY_TASKS if name not in registered]
    assert missing == []
