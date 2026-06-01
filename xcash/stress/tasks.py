# xcash/stress/tasks.py
from datetime import timedelta

import httpx
import structlog
from celery import shared_task
from django.db import transaction
from django.utils import timezone

from common.decorators import singleton_task

from .models import DepositStressCase
from .models import DepositStressCaseStatus
from .models import InvoiceStressCase
from .models import InvoiceStressCaseStatus
from .models import StressRun
from .models import StressRunStatus
from .models import WithdrawalStressCase
from .models import WithdrawalStressCaseStatus
from .payment import simulate_payment
from .service import StressService

logger = structlog.get_logger()


@shared_task(ignore_result=True)
def prepare_stress(stress_run_id: int) -> None:
    """准备 StressRun 测试数据：创建 Project 和 InvoiceStressCase。"""
    try:
        stress_run = StressRun.objects.get(pk=stress_run_id)
    except StressRun.DoesNotExist:
        return
    try:
        StressService.prepare(stress_run)
    except Exception as exc:
        logger.exception("stress.prepare.failed", stress_run_id=stress_run_id)
        StressRun.objects.filter(pk=stress_run_id).update(
            status=StressRunStatus.FAILED,
            error=str(exc)[:2000],
            finished_at=timezone.now(),
        )


# 瞬态连接错误：Django 服务未就绪、连接被重置等，可安全重试。
_TRANSIENT_EXC = (ConnectionError, httpx.ConnectError, httpx.RemoteProtocolError)

_RETRY_KWARGS = {
    "autoretry_for": _TRANSIENT_EXC,
    "retry_backoff": 3,
    "retry_backoff_max": 30,
    "max_retries": 5,
}


@shared_task(ignore_result=True, soft_time_limit=120, time_limit=180, **_RETRY_KWARGS)
def execute_stress_case(case_id: int) -> None:
    """执行单个 InvoiceStressCase 的完整流程。"""
    try:
        case = InvoiceStressCase.objects.select_related("stress_run__project").get(
            pk=case_id
        )
    except InvoiceStressCase.DoesNotExist:
        return

    if case.status != InvoiceStressCaseStatus.PENDING:
        return

    case.started_at = timezone.now()
    case.status = InvoiceStressCaseStatus.CREATING
    case.save(update_fields=["started_at", "status"])

    try:
        _execute(case)
    except Exception as exc:
        logger.exception("stress.case.failed", case_id=case.pk)
        case.status = InvoiceStressCaseStatus.FAILED
        case.error = str(exc)[:2000]
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])
        StressService.on_case_finished(case)


def _execute(case: InvoiceStressCase) -> None:
    """InvoiceStressCase 执行核心流程的 API 阶段（创建账单 + 选支付方式）。

    完成后立即调度链上支付阶段。支付 task 会在发交易前推进 Anvil 下一块
    时间戳，避免用固定 countdown 给每笔账单增加排队延迟。
    """
    # 阶段 1: 创建 Invoice
    resp = StressService.create_invoice(case)
    case.invoice_sys_no = resp["sys_no"]
    case.invoice_out_no = resp.get("out_no", "")
    case.status = InvoiceStressCaseStatus.CREATED
    case.invoice_created_at = timezone.now()
    case.save(
        update_fields=[
            "invoice_sys_no",
            "invoice_out_no",
            "status",
            "invoice_created_at",
        ]
    )

    # 阶段 2: 选择支付方式
    resp = StressService.select_method(case)
    case.crypto = resp.get("crypto", "")
    case.chain = resp.get("chain", "")
    case.pay_address = resp.get("pay_address", "")
    case.pay_amount = resp.get("pay_amount")
    case.api_done_at = timezone.now()
    case.save(
        update_fields=[
            "crypto",
            "chain",
            "pay_address",
            "pay_amount",
            "api_done_at",
        ]
    )

    # 阶段 3: 链上支付 —— 拆为独立 task，立即释放当前 worker 线程。
    execute_stress_case_payment.apply_async(args=[case.pk])


@shared_task(ignore_result=True, soft_time_limit=120, time_limit=180, **_RETRY_KWARGS)
def execute_stress_case_payment(case_id: int) -> None:
    """执行 InvoiceStressCase 的链上支付阶段（CREATED → PAYING → PAID）。

    由 execute_stress_case 的 _execute 调度。真正的时间戳保护在 _do_payment
    内完成，避免固定等待拖慢批量支付。
    """
    try:
        case = InvoiceStressCase.objects.select_related("stress_run__project").get(
            pk=case_id
        )
    except InvoiceStressCase.DoesNotExist:
        return

    # 状态守卫：只有处于 CREATED 的 case 才允许进入链上支付阶段。
    # 其他状态（重复派发、整轮超时已 SKIPPED、已 FAILED 等）直接幂等返回。
    if case.status != InvoiceStressCaseStatus.CREATED:
        return

    try:
        case.status = InvoiceStressCaseStatus.PAYING
        case.save(update_fields=["status"])

        payment_result = _do_payment(case)
        if hasattr(case, "refresh_from_db"):
            case.refresh_from_db(fields=["status"])
        case.tx_hash = payment_result["tx_hash"]
        case.payer_address = payment_result["payer_address"]
        case.chain_paid_at = timezone.now()
        update_fields = ["tx_hash", "payer_address", "chain_paid_at"]

        # 本地测试链确认很快，webhook 可能在 _do_payment 返回前已经把 case
        # 推进到 WEBHOOK_OK/SUCCEEDED。这里只在仍处于支付阶段时写 PAID，
        # 避免把 webhook 已推进的状态回退，导致后续归集验证找不到 case。
        if case.status == InvoiceStressCaseStatus.PAYING:
            case.status = InvoiceStressCaseStatus.PAID
            update_fields.append("status")
            case.save(update_fields=update_fields)

            # 派发 webhook 超时检查任务（15 分钟后）—— 必须在 PAID 状态确立后。
            check_webhook_timeout.apply_async(
                args=[case.pk],
                eta=timezone.now() + timedelta(minutes=15),
            )
        elif case.status in {
            InvoiceStressCaseStatus.WEBHOOK_OK,
            InvoiceStressCaseStatus.SUCCEEDED,
        }:
            case.save(update_fields=update_fields)
    except Exception as exc:
        logger.exception("stress.case_payment.failed", case_id=case.pk)
        case.status = InvoiceStressCaseStatus.FAILED
        case.error = str(exc)[:2000]
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])
        StressService.on_case_finished(case)


def _do_payment(case: InvoiceStressCase) -> dict[str, str]:
    """统一调用 stress 链上转币入口，返回 tx_hash 与付款方地址。

    当前场景是“账单支付”，所以 payment_ref 使用 case 维度命名。
    后续如果新增充币测试或其他需要模拟链上转入的测试，应该继续走
    `stress.payment.simulate_payment()`，而不是重新旁路实现发送逻辑。
    """
    from .evm import ensure_next_block_after

    ensure_next_block_after(getattr(case, "api_done_at", None) or timezone.now())
    return simulate_payment(
        to_address=case.pay_address,
        chain_code=case.chain,
        crypto_symbol=case.crypto,
        amount=case.pay_amount,
        payment_ref=f"case-{case.pk}",
    )


@shared_task(ignore_result=True, soft_time_limit=120, time_limit=180, **_RETRY_KWARGS)
def execute_withdrawal_case(case_id: int) -> None:
    """执行单个 WithdrawalStressCase 的完整流程。"""
    try:
        case = WithdrawalStressCase.objects.select_related("stress_run__project").get(
            pk=case_id
        )
    except WithdrawalStressCase.DoesNotExist:
        return

    if case.status != WithdrawalStressCaseStatus.PENDING:
        return

    case.started_at = timezone.now()
    case.status = WithdrawalStressCaseStatus.CREATING
    case.save(update_fields=["started_at", "status"])

    try:
        _execute_withdrawal(case)
    except Exception as exc:
        logger.exception("stress.withdrawal_case.failed", case_id=case.pk)
        case.status = WithdrawalStressCaseStatus.FAILED
        case.error = str(exc)[:2000]
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])
        StressService.on_case_finished(case)


def _execute_withdrawal(case: WithdrawalStressCase) -> None:
    """WithdrawalStressCase 执行核心流程。"""
    # 阶段 1: 调用提币 API
    resp = StressService.create_withdrawal(case)
    case.withdrawal_sys_no = resp["sys_no"]
    case.withdrawal_out_no = f"STRESS-WD-{case.stress_run_id}-{case.sequence}"
    case.tx_hash = resp.get("hash", "")
    case.status = WithdrawalStressCaseStatus.CREATED
    case.api_done_at = timezone.now()
    case.save(
        update_fields=[
            "withdrawal_sys_no",
            "withdrawal_out_no",
            "tx_hash",
            "status",
            "api_done_at",
        ]
    )

    # 阶段 2: 等待链上确认（由系统自动处理）
    case.status = WithdrawalStressCaseStatus.CONFIRMING
    case.save(update_fields=["status"])

    # 派发超时检查任务（15 分钟后）
    check_withdrawal_webhook_timeout.apply_async(
        args=[case.pk],
        eta=timezone.now() + timedelta(minutes=15),
    )


@shared_task(ignore_result=True)
def check_withdrawal_webhook_timeout(case_id: int) -> None:
    """检查 WithdrawalStressCase 是否在超时前收到了 webhook。"""
    with transaction.atomic():
        try:
            case = WithdrawalStressCase.objects.select_for_update().get(pk=case_id)
        except WithdrawalStressCase.DoesNotExist:
            return

        if case.status != WithdrawalStressCaseStatus.CONFIRMING:
            return

        case.status = WithdrawalStressCaseStatus.FAILED
        case.error = "webhook 超时未收到（15 分钟）"
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])

    StressService.on_case_finished(case)


@shared_task(ignore_result=True)
def finalize_stress_timeout(stress_run_id: int) -> None:
    """StressRun 级别的兜底超时：将所有未执行的 case 标记为 skipped 并结束整轮压测。

    解决场景：worker 重启 / Django 短暂不可用等导致部分 case 任务丢失，
    StressRun 永远凑不够终态数量而卡在 running。
    """
    with transaction.atomic():
        try:
            stress_run = StressRun.objects.select_for_update().get(pk=stress_run_id)
        except StressRun.DoesNotExist:
            return

        if stress_run.status != StressRunStatus.RUNNING:
            return

        terminal_invoice = {
            InvoiceStressCaseStatus.SUCCEEDED,
            InvoiceStressCaseStatus.FAILED,
            InvoiceStressCaseStatus.SKIPPED,
        }
        terminal_withdrawal = {
            WithdrawalStressCaseStatus.SUCCEEDED,
            WithdrawalStressCaseStatus.FAILED,
            WithdrawalStressCaseStatus.SKIPPED,
        }
        terminal_deposit = {
            DepositStressCaseStatus.SUCCEEDED,
            DepositStressCaseStatus.FAILED,
            DepositStressCaseStatus.SKIPPED,
        }

        non_terminal_invoices = stress_run.cases.exclude(status__in=terminal_invoice)
        non_terminal_withdrawals = stress_run.withdrawal_cases.exclude(
            status__in=terminal_withdrawal
        )
        non_terminal_deposits = stress_run.deposit_cases.exclude(
            status__in=terminal_deposit
        )

        skipped_count = (
            non_terminal_invoices.count()
            + non_terminal_withdrawals.count()
            + non_terminal_deposits.count()
        )
        if skipped_count == 0:
            return

        now = timezone.now()
        non_terminal_invoices.update(
            status=InvoiceStressCaseStatus.SKIPPED,
            error="压测整轮超时，任务未执行",
            finished_at=now,
        )
        non_terminal_withdrawals.update(
            status=WithdrawalStressCaseStatus.SKIPPED,
            error="压测整轮超时，任务未执行",
            finished_at=now,
        )
        non_terminal_deposits.update(
            status=DepositStressCaseStatus.SKIPPED,
            error="压测整轮超时，任务未执行",
            finished_at=now,
        )

        stress_run.skipped += skipped_count
        stress_run.status = StressRunStatus.COMPLETED
        stress_run.finished_at = now
        stress_run.save(update_fields=["skipped", "status", "finished_at"])

    logger.info(
        "stress.finalize_timeout",
        stress_run_id=stress_run_id,
        skipped=skipped_count,
    )


@shared_task(ignore_result=True)
def check_webhook_timeout(case_id: int) -> None:
    """检查 InvoiceStressCase 是否在超时前收到了 webhook。"""
    with transaction.atomic():
        try:
            case = InvoiceStressCase.objects.select_for_update().get(pk=case_id)
        except InvoiceStressCase.DoesNotExist:
            return

        if case.status != InvoiceStressCaseStatus.PAID:
            return

        case.status = InvoiceStressCaseStatus.FAILED
        case.error = "webhook 超时未收到（15 分钟）"
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])

    StressService.on_case_finished(case)


# ── 充币压测 ──────────────────────────────────────────────────


@shared_task(ignore_result=True, soft_time_limit=120, time_limit=180, **_RETRY_KWARGS)
def execute_deposit_case(case_id: int) -> None:
    """执行单个 DepositStressCase 的完整流程：获取地址 → 模拟充值 → 等待 webhook。"""
    try:
        case = DepositStressCase.objects.select_related("stress_run__project").get(
            pk=case_id
        )
    except DepositStressCase.DoesNotExist:
        return

    if case.status != DepositStressCaseStatus.PENDING:
        return

    case.started_at = timezone.now()
    case.status = DepositStressCaseStatus.CREATING
    case.save(update_fields=["started_at", "status"])

    try:
        _execute_deposit(case)
    except Exception as exc:
        logger.exception("stress.deposit_case.failed", case_id=case.pk)
        case.status = DepositStressCaseStatus.FAILED
        case.error = str(exc)[:2000]
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])
        StressService.on_case_finished(case)


def _execute_deposit(case: DepositStressCase) -> None:
    """DepositStressCase 执行核心流程的 API 阶段（获取充值地址）。

    完成后通过 Celery countdown=2 调度链上充值阶段，避免 worker 线程被
    sleep 阻塞。等待 2 秒的目的：确保区块时间戳晚于充值地址创建时间。

    注意：保持 case.status 为 CREATING，由 payment task 推进到 PAYING/PAID。
    """
    # 阶段 1: 获取充值地址
    deposit_address = StressService.ensure_deposit_address(case)
    case.deposit_address = deposit_address
    case.api_done_at = timezone.now()
    case.save(update_fields=["deposit_address", "api_done_at"])

    # 阶段 2: 链上充值 —— 拆为独立 task，countdown=2 保证区块时间戳晚于地址创建时间，
    # 同时立即释放 worker 线程。
    execute_deposit_case_payment.apply_async(args=[case.pk], countdown=2)


@shared_task(ignore_result=True, soft_time_limit=120, time_limit=180, **_RETRY_KWARGS)
def execute_deposit_case_payment(case_id: int) -> None:
    """执行 DepositStressCase 的链上充值阶段（CREATING → PAYING → PAID）。

    由 execute_deposit_case 的 _execute_deposit 通过 countdown=2 调度，等价于
    原 `time.sleep(2)` 的语义但不占用 worker 线程。
    """
    try:
        case = DepositStressCase.objects.select_related("stress_run__project").get(
            pk=case_id
        )
    except DepositStressCase.DoesNotExist:
        return

    # 状态守卫：只有处于 CREATING 的 case 才允许进入链上充值阶段。
    # _execute_deposit 完成阶段 1 后未改 status，故此处仍为 CREATING。
    if case.status != DepositStressCaseStatus.CREATING:
        return

    try:
        case.status = DepositStressCaseStatus.PAYING
        case.save(update_fields=["status"])

        payment_result = simulate_payment(
            to_address=case.deposit_address,
            chain_code=case.chain,
            crypto_symbol=case.crypto,
            amount=case.amount,
            payment_ref=f"deposit-{case.pk}",
        )
        case.tx_hash = payment_result["tx_hash"]
        case.payer_address = payment_result["payer_address"]
        case.status = DepositStressCaseStatus.PAID
        case.chain_paid_at = timezone.now()
        case.save(
            update_fields=[
                "tx_hash",
                "payer_address",
                "status",
                "chain_paid_at",
            ]
        )

        # 派发 webhook 超时检查任务（15 分钟后）—— 必须在 PAID 状态确立后。
        check_deposit_webhook_timeout.apply_async(
            args=[case.pk],
            eta=timezone.now() + timedelta(minutes=15),
        )
    except Exception as exc:
        logger.exception("stress.deposit_case_payment.failed", case_id=case.pk)
        case.status = DepositStressCaseStatus.FAILED
        case.error = str(exc)[:2000]
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])
        StressService.on_case_finished(case)


@shared_task(ignore_result=True)
def check_deposit_webhook_timeout(case_id: int) -> None:
    """检查 DepositStressCase 是否在超时前收到了 webhook。"""
    with transaction.atomic():
        try:
            case = DepositStressCase.objects.select_for_update().get(pk=case_id)
        except DepositStressCase.DoesNotExist:
            return

        if case.status != DepositStressCaseStatus.PAID:
            return

        case.status = DepositStressCaseStatus.FAILED
        case.error = "webhook 超时未收到（15 分钟）"
        case.finished_at = timezone.now()
        case.save(update_fields=["status", "error", "finished_at"])

    StressService.on_case_finished(case)
    _maybe_trigger_collection_verification(case.stress_run_id)


def _maybe_trigger_collection_verification(stress_run_id: int) -> None:
    """延迟调度归集验证任务。

    并发 webhook 处理时存在竞态：每个 handler 提交自己的 case 后检查
    其他 case 状态，但其他 handler 的事务可能尚未提交，导致所有 handler
    都认为还有 case 在 PAID 状态而不触发验证。

    解决方案：无条件延迟 15 秒调度。verify_deposit_collection 本身
    幂等且会检查前置条件，多次调度不会重复执行。
    """
    verify_deposit_collection.apply_async(
        args=[stress_run_id],
        countdown=15,
    )


def _maybe_trigger_invoice_collection_verification(stress_run_id: int) -> None:
    """合约账单归集验证派发器。

    与 deposit 版同形态：无条件延迟 15 秒调度，verify_invoice_collection
    本身幂等并自带前置条件检查，多次调度安全。
    """
    verify_invoice_collection.apply_async(
        args=[stress_run_id],
        countdown=15,
    )


@shared_task(bind=True, ignore_result=True, soft_time_limit=120, time_limit=180)
@singleton_task(timeout=180, use_params=True)
def verify_deposit_collection(self, stress_run_id: int) -> None:
    """VaultSlot 体系下，Deposit 确认（webhook OK）即代表充币入账完成，直接收口。

    新架构里「归集」（VaultSlot → 项目 vault）是 confirm_deposit 触发的
    fire-and-forget 异步 TxTask，与商户视角的「充币是否成功」解耦，因此压测
    不再轮询归集进度——旧版按 Deposit.status 轮询，而该字段已随充币模型简化移除
    （Deposit 确认状态直接取自其 Transfer，不再维护独立状态机）。

    本 task 与 verify_invoice_collection 同形态：取出已通过 webhook 验证的
    deposit case，逐个确认 Deposit 记录存在后标 SUCCEEDED。singleton_task 用
    stress_run_id 区分锁，保证 webhook handler 重复触发与安全网调度之间幂等；
    已无 WEBHOOK_OK case 时直接 return。
    """
    try:
        stress = StressRun.objects.select_related("project").get(pk=stress_run_id)
    except StressRun.DoesNotExist:
        return

    webhook_ok_cases = list(
        DepositStressCase.objects.filter(
            stress_run=stress,
            status=DepositStressCaseStatus.WEBHOOK_OK,
        )
    )
    if not webhook_ok_cases:
        return

    _finalize_collection_verification(stress, webhook_ok_cases, reason="completed")


def _finalize_collection_verification(
    stress: StressRun,
    webhook_ok_cases: list[DepositStressCase],
    reason: str,
) -> None:
    """VaultSlot 体系下，Deposit 完成即代表入账验证完成。"""
    from deposits.models import Deposit

    logger.info(
        "stress.deposit_collection.finalize",
        stress_run_id=stress.pk,
        reason=reason,
        cases=len(webhook_ok_cases),
    )

    for case in webhook_ok_cases:
        # Transfer.hash 带 0x，case.tx_hash 可能不带
        h = case.tx_hash
        hash_variants = (
            [h, f"0x{h}"] if not h.startswith("0x") else [h, h.removeprefix("0x")]
        )
        deposit = (
            Deposit.objects.filter(
                transfer__hash__in=hash_variants,
                customer__project=stress.project,
            )
            .first()
        )

        update_fields = [
            "collection_verified",
            "collection_hash",
            "status",
            "error",
            "finished_at",
        ]
        if deposit:
            case.collection_verified = True
            case.collection_hash = deposit.transfer.hash
            case.status = DepositStressCaseStatus.SUCCEEDED
            case.collection_done_at = timezone.now()
            update_fields.append("collection_done_at")
        else:
            case.status = DepositStressCaseStatus.FAILED
            case.error = "未找到 Deposit 记录"

        case.finished_at = timezone.now()
        case.save(update_fields=update_fields)
        StressService.on_case_finished(case)


# ── 合约账单归集验证（run-level，与 verify_deposit_collection 同形态）──


@shared_task(bind=True, ignore_result=True, soft_time_limit=120, time_limit=180)
@singleton_task(timeout=180, use_params=True)
def verify_invoice_collection(self, stress_run_id: int) -> None:
    """VaultSlot 合约账单不再执行旧 collector 归集，webhook OK 后直接收口。"""
    from invoices.models import InvoiceBillingMode

    try:
        stress = StressRun.objects.select_related("project").get(pk=stress_run_id)
    except StressRun.DoesNotExist:
        return

    # 只处理已经通过 webhook 验证的合约 case。压测并发下可能有少数 case
    # 仍停留在 CREATED/PAYING；它们不应阻塞同轮其他已完成归集的 case。
    webhook_ok_cases = list(
        InvoiceStressCase.objects.filter(
            stress_run=stress,
            billing_mode=InvoiceBillingMode.CONTRACT,
            status=InvoiceStressCaseStatus.WEBHOOK_OK,
        )
    )
    if not webhook_ok_cases:
        return

    _finalize_invoice_collection_verification(
        stress, webhook_ok_cases, reason="completed"
    )


def _finalize_invoice_collection_verification(
    stress: StressRun,
    webhook_ok_cases: list[InvoiceStressCase],
    reason: str,
) -> None:
    """VaultSlot 合约账单不再检查旧合约归集记录。"""
    from invoices.models import Invoice

    logger.info(
        "stress.invoice_collection.finalize",
        stress_run_id=stress.pk,
        reason=reason,
        cases=len(webhook_ok_cases),
    )

    for case in webhook_ok_cases:
        invoice = (
            Invoice.objects.filter(sys_no=case.invoice_sys_no)
            .first()
        )
        update_fields = [
            "collection_verified",
            "collection_hash",
            "status",
            "error",
            "finished_at",
        ]
        now = timezone.now()
        if invoice is not None:
            case.collection_verified = True
            case.collection_hash = invoice.transfer.hash if invoice.transfer_id else ""
            case.status = InvoiceStressCaseStatus.SUCCEEDED
            case.collection_done_at = now
            update_fields.append("collection_done_at")
        else:
            case.status = InvoiceStressCaseStatus.FAILED
            if invoice is None:
                case.error = "未找到对应 Invoice"
            else:
                case.error = f"VaultSlot 账单未完成（reason={reason}）"
        case.finished_at = now
        case.save(update_fields=update_fields)
        StressService.on_case_finished(case)
