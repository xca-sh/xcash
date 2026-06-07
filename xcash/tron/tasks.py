import time

import structlog
from celery import shared_task
from django.db import transaction as db_transaction
from django.db.models import Q
from tron.client import TronClientError
from tron.models import TronTxTask
from tron.saas_gas_billing import notify_vault_slot_collect_gas_fee
from tron.saas_gas_billing import notify_vault_slot_deploy_gas_fee
from tron.scanner import TronTrc20Scanner

from chains.adapters import AdapterFactory
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.constants import ChainType
from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.vault_slot_balances import refresh_vault_slot_balance_for_collect_task
from chains.vault_slots import mark_deployed_by_task
from chains.vault_slots import mark_deployed_if_on_chain_for_task
from common.decorators import singleton_task
from common.time import ago

logger = structlog.get_logger()


def tx_check_status(result: TxCheckStatus | TxCheckResult) -> TxCheckStatus:
    return result.status if isinstance(result, TxCheckResult) else result


def has_required_confirmations(*, chain: Chain, result: TxCheckResult | None) -> bool:
    if result is None or result.block_number is None:
        return False
    confirmed_at_or_before = chain.latest_block_number - chain.confirm_block_count
    return int(result.block_number) <= confirmed_at_or_before


@shared_task(ignore_result=True)
@singleton_task(timeout=30, use_params=True)
def broadcast_tron_task(pk: int) -> None:
    tx_task = TronTxTask.objects.select_related("base_task", "chain", "sender").get(pk=pk)
    if not tx_task.can_rebroadcast:
        return
    try:
        tx_task.broadcast()
    except TronClientError as exc:
        logger.warning(
            "Tron 任务广播失败",
            task_pk=tx_task.pk,
            chain=tx_task.chain.code,
            error=str(exc),
        )


@shared_task(ignore_result=True)
@singleton_task(timeout=64)
@db_transaction.atomic
def dispatch_tron_tx_tasks() -> None:
    now_ms = int(time.time() * 1000)
    tasks = (
        TronTxTask.objects.select_for_update()
        .select_related("base_task")
        .filter(
            Q(base_task__status=TxTaskStatus.QUEUED)
            | Q(
                base_task__status=TxTaskStatus.PENDING_CHAIN,
                expiration__lte=now_ms,
            ),
            Q(last_attempt_at__isnull=True) | Q(last_attempt_at__lt=ago(minutes=2)),
            created_at__lt=ago(seconds=2),
        )
        .order_by("created_at")[:16]
    )
    for task in tasks:
        db_transaction.on_commit(lambda pk=task.pk: broadcast_tron_task.delay(pk))


def notify_gas_fee_for_receipt_task(task: TxTask) -> None:
    """按任务类型把已确认终局的链上成本回调给 SaaS 计费。"""
    if task.tx_type == TxTaskType.VaultSlotDeploy:
        notify_vault_slot_deploy_gas_fee(tx_task=task)
    elif task.tx_type == TxTaskType.VaultSlotCollect:
        notify_vault_slot_collect_gas_fee(tx_task=task)


@shared_task(ignore_result=True)
@singleton_task(timeout=55)
def confirm_tron_receipt_tx_tasks() -> None:
    """按回执收口 Tron 主动发起的链上任务(部署 / 归集)。

    部署不产生 TRC20 入账,归集是 slot→vault(收款方为系统外 vault),二者都不会被
    扫描器当作「打入系统观察地址」的入账观测,无法靠扫描器确认;统一在此用
    adapter.tx_result 查回执推进终局,并在成功终局时按类型回调 SaaS 计费。
    """
    tasks = (
        TxTask.objects.select_related("chain")
        .filter(
            chain__type=ChainType.TRON,
            tx_type__in=(TxTaskType.VaultSlotDeploy, TxTaskType.VaultSlotCollect),
            status=TxTaskStatus.PENDING_CHAIN,
            tx_hash__isnull=False,
        )
        .exclude(tx_hash="")
        .order_by("updated_at")[:32]
    )
    for task in tasks:
        adapter = AdapterFactory.get_adapter(task.chain.type)
        raw_result = adapter.tx_result(chain=task.chain, tx_hash=task.tx_hash)
        if isinstance(raw_result, Exception):
            logger.warning(
                "Tron 主动交易回执确认查询失败",
                chain=task.chain.code,
                tx_task_id=task.pk,
                tx_hash=task.tx_hash,
                error=str(raw_result),
            )
            continue

        result_meta = raw_result if isinstance(raw_result, TxCheckResult) else None
        status = tx_check_status(raw_result)
        if status == TxCheckStatus.SUCCEEDED:
            if not has_required_confirmations(chain=task.chain, result=result_meta):
                continue
            updated = TxTask.mark_finalized_success(
                chain=task.chain,
                tx_hash=task.tx_hash,
            )
            if updated:
                if task.tx_type == TxTaskType.VaultSlotDeploy:
                    mark_deployed_by_task(task)
                elif task.tx_type == TxTaskType.VaultSlotCollect:
                    refresh_vault_slot_balance_for_collect_task(task)
                notify_gas_fee_for_receipt_task(task)
        elif status == TxCheckStatus.MISSING:
            continue
        elif status == TxCheckStatus.FAILED:
            updated = TxTask.mark_finalized_failed(
                task_id=task.pk,
                expected_status=TxTaskStatus.PENDING_CHAIN,
            )
            if updated and task.tx_type == TxTaskType.VaultSlotDeploy:
                mark_deployed_if_on_chain_for_task(task)


@shared_task(ignore_result=True)
@singleton_task(timeout=48, use_params=True)
def scan_tron_chain(chain_pk: int) -> None:
    chain = Chain.objects.get(pk=chain_pk)
    if not chain.active:
        return
    if chain.type == ChainType.TRON and not chain.tron_api_key:
        logger.warning("Tron TRC20 扫描跳过，缺少 API Key", chain=chain.code)
        return

    try:
        try:
            summary = TronTrc20Scanner.scan_chain(chain=chain)
        except TronClientError:
            logger.warning("Tron TRC20 扫描 RPC 失败", chain=chain.code)
            return

        logger.info(
            "Tron TRC20 扫描完成",
            chain=chain.code,
            filter_addresses=summary.filter_addresses,
            blocks_scanned=summary.blocks_scanned,
            events_seen=summary.events_seen,
        )
    finally:
        # 无论成功还是 RPC 失败都推进 last_scanned_at，按固定周期重试。
        chain.mark_scanned()


@shared_task(ignore_result=True)
@singleton_task(timeout=64)
def scan_active_tron_chains() -> None:
    """每 2 秒巡检活跃 Tron 链，仅调度到期（now - last_scanned_at ≥ 扫描周期）的链。"""
    chains = (
        Chain.objects.filter(active=True, type=ChainType.TRON)
        .exclude(tron_api_key="")
    )
    for chain in chains:
        if chain.is_due_for_scan:
            scan_tron_chain.delay(chain.pk)
