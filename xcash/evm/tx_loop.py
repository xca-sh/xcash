"""EVM 主动交易在 chain-runner 链循环中的两步：终局轮询与派发。

同一条链只有一个链线程在发交易（见 chains.runner），nonce 按序进入 mempool 由「单线程内
按 nonce 取队首、顺序推进」直接保证：不再需要 Celery 广播任务、单例锁、广播成功后的
接力投递与 beat 周期补偿。新建任务由 EvmTxTask.schedule 在事务内发 PG 通知，链线程
被唤醒后立即补跑派发；派发永远按库里的任务状态工作，通知只决定何时醒来。
"""

from datetime import timedelta

import structlog
from django.utils import timezone

from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.scanning import deadline_passed
from evm.models import EvmTxTask
from evm.poller import EvmTaskPoller

logger = structlog.get_logger()

# 队首广播失败（余额不足、节点报错）后的退避：期间该发送地址整体停发，不顺延放行更高
# nonce——前序 nonce 未进入 mempool 时，更高 nonce 进去也只会卡在节点队列里。
EVM_QUEUED_HEAD_RETRY_AFTER = timedelta(minutes=4)


def poll_evm_chain_round(chain: Chain, deadline: float) -> None:
    """轮询本链在途主动交易的链上终局，长时间无回执的按 mempool 丢弃路径重播。"""
    EvmTaskPoller.poll_chain(chain=chain, deadline=deadline)


def dispatch_evm_chain_round(chain: Chain, deadline: float) -> None:
    """按 nonce 顺序派发本链各发送地址的 QUEUED 交易。

    每个发送地址从最低 QUEUED nonce 开始，队首离开 QUEUED（进入 SUBMITTED，或按已有回执
    直接收口）就接着发下一笔，积压在一轮内按广播速度排空。遇到以下情况，该地址本轮停手：
    - 队首在退避期内（上次尝试失败）；
    - pipeline 已满（同地址 SUBMITTED 达到 EVM_PIPELINE_DEPTH）；
    - 队首广播后仍是 QUEUED（pre-flight 未过，等补 gas）或广播抛错。
    本步预算耗尽即整体停手，剩余的下一轮或下次唤醒再发；第一笔之前不检查预算。
    """
    sender_ids = list(
        EvmTxTask.objects.filter(chain=chain, base_task__status=TxTaskStatus.QUEUED)
        .order_by("sender_id")
        .values_list("sender_id", flat=True)
        .distinct()
    )
    attempted = False
    for sender_id in sender_ids:
        while True:
            if attempted and deadline_passed(deadline):
                return
            head = (
                EvmTxTask.objects.select_related("base_task", "sender")
                .filter(
                    chain=chain,
                    sender_id=sender_id,
                    base_task__status=TxTaskStatus.QUEUED,
                )
                .order_by("nonce")
                .first()
            )
            if head is None or not head_retry_due(head) or head.is_pipeline_full():
                break
            # 复用本轮已加载的链，避免每笔任务首次访问 head.chain 再查一次库。
            head.chain = chain
            attempted = True
            if not broadcast_head(head):
                break


def head_retry_due(task: EvmTxTask) -> bool:
    """从未尝试过，或距上次尝试已超过退避间隔。"""
    if task.last_attempt_at is None:
        return True
    return task.last_attempt_at <= timezone.now() - EVM_QUEUED_HEAD_RETRY_AFTER


def broadcast_head(task: EvmTxTask) -> bool:
    """广播 nonce 队首，返回该 nonce 是否已离开 QUEUED（可以接着发下一笔）。"""
    try:
        task.broadcast()
    except Exception:  # noqa: BLE001
        logger.exception(
            "EVM 主动交易广播失败",
            chain=task.chain.code,
            sender=task.sender.address,
            nonce=task.nonce,
        )
        return False
    status = (
        TxTask.objects.filter(pk=task.base_task_id)
        .values_list("status", flat=True)
        .get()
    )
    return status != TxTaskStatus.QUEUED
