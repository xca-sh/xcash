import structlog
from django.db.models import Exists
from django.db.models import OuterRef
from procrastinate.contrib.django import app

from chains.models import TxTaskStatus
from common.time import ago
from evm.models import EvmTxTask

logger = structlog.get_logger()

# EVM 主动交易的派发、广播与终局轮询由 chain-runner 的按链循环负责（见 evm.tx_loop）。
# 这里只留跨进程的独立观察者（跑在 worker 进程）：链循环自身停摆时，卡在 nonce 队首的任务
# 仍能被告警出来。
EVM_QUEUED_STUCK_ALERT_AFTER_MINUTES = 30


@app.task(name="evm.scan_stuck_queued_evm_tx_tasks")
def scan_stuck_queued_evm_tx_tasks(
    *,
    timestamp: int | None = None,  # noqa: ARG001
    limit: int = 32,
) -> int:
    """告警长期卡在 QUEUED 队首的 EVM 任务，避免 nonce 队列无声停摆。"""
    lower_queued = EvmTxTask.objects.filter(
        sender_id=OuterRef("sender_id"),
        chain_id=OuterRef("chain_id"),
        nonce__lt=OuterRef("nonce"),
        base_task__status=TxTaskStatus.QUEUED,
    )
    tasks = (
        EvmTxTask.objects.select_related("base_task", "sender", "chain")
        .annotate(has_lower_queued=Exists(lower_queued))
        .filter(
            base_task__status=TxTaskStatus.QUEUED,
            created_at__lt=ago(minutes=EVM_QUEUED_STUCK_ALERT_AFTER_MINUTES),
            has_lower_queued=False,
        )
        .order_by("created_at")[:limit]
    )
    alerted = 0
    for task in tasks:
        alerted += 1
        logger.warning(
            "EVM QUEUED 任务长期停留在 nonce 队首",
            evm_task_id=task.pk,
            tx_task_id=task.base_task_id,
            chain=task.chain.code,
            sender=task.sender.address,
            nonce=task.nonce,
            tx_type=task.base_task.tx_type,
            tx_hash=task.base_task.tx_hash,
            created_at=task.created_at,
            last_attempt_at=task.last_attempt_at,
        )
    return alerted
