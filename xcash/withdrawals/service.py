from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from django.db import transaction as db_transaction
from django.db.models import Sum
from django.utils import timezone

from chains.adapters import AdapterFactory
from chains.models import AddressUsage
from chains.models import ChainType
from chains.models import TransferType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.transfer_matching import raw_amount
from chains.transfer_matching import transfer_matches
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.internal_callback import send_internal_callback
from common.utils.math import format_decimal_stripped
from evm.constants import DEFAULT_BASE_TRANSFER_GAS
from evm.constants import DEFAULT_ERC20_TRANSFER_GAS
from users.otp import validate_admin_approval_context
from webhooks.service import WebhookService
from withdrawals.models import Withdrawal
from withdrawals.models import WithdrawalReviewLog
from withdrawals.models import WithdrawalReviewStatus

logger = structlog.get_logger()

if TYPE_CHECKING:
    from chains.models import Transfer


class WithdrawalService:
    # 当前支持提币的链类型；新增链类型需同步更新 submit_withdrawal 中的分发逻辑。
    WITHDRAWAL_SUPPORTED_CHAIN_TYPES = (ChainType.EVM,)

    POLICY_TRACKED_REVIEW_STATUSES = (
        WithdrawalReviewStatus.REVIEWING,
        WithdrawalReviewStatus.APPROVED,
    )
    IN_FLIGHT_TX_STATUSES = (
        TxTaskStatus.QUEUED,
        TxTaskStatus.PENDING_CHAIN,
        TxTaskStatus.PENDING_CONFIRM,
    )

    @staticmethod
    def build_webhook_payload(withdrawal: Withdrawal) -> dict:
        """统一构造 withdrawal webhook payload，仅表达链上两阶段通知。"""
        data = {
            "sys_no": withdrawal.sys_no,
            "out_no": withdrawal.out_no,
            "chain": withdrawal.chain.code,
            "hash": (
                withdrawal.tx_task.tx_hash if withdrawal.tx_task_id else withdrawal.hash
            ),
            "amount": format_decimal_stripped(withdrawal.amount),
            "crypto": withdrawal.crypto.symbol,
            "confirmed": (
                withdrawal.tx_task_id
                and withdrawal.tx_task.status == TxTaskStatus.CONFIRMED
            ),
        }
        if withdrawal.customer_id:
            data["uid"] = withdrawal.customer.uid
        return {
            "type": "withdrawal",
            "data": data,
        }

    @staticmethod
    def estimate_current_network_fee_raw(*, chain, crypto) -> int:
        """估算当前这笔提币需要额外预留的原生币网络费。"""
        if chain.type != ChainType.EVM:
            return 0

        try:
            gas_price = chain.w3.eth.gas_price  # noqa: SLF001
        except Exception:
            logger.warning(
                "获取 EVM gas_price 失败，跳过实时 gas 预留", chain=chain.code
            )
            return 0

        gas_limit = (
            DEFAULT_BASE_TRANSFER_GAS
            if crypto == chain.native_coin
            else DEFAULT_ERC20_TRANSFER_GAS
        )
        return int(gas_price * gas_limit)

    # 在途提币的 gas 预留使用签名时的历史 gas_price，Gas 暴涨时可能低估；
    # 乘以安全系数补偿实时与历史 gas_price 的偏差。
    GAS_RESERVE_SAFETY_FACTOR = Decimal("1.2")

    @classmethod
    def pending_gas_reserved_raw(cls, *, project, chain) -> int:
        """统计本项目在该 EVM 链上所有在途提币已经占用的 gas 预算（含安全系数）。"""
        if chain.type != ChainType.EVM:
            return 0

        reserved = 0
        pending_tasks = Withdrawal.objects.filter(
            project=project,
            chain=chain,
            review_status=WithdrawalReviewStatus.APPROVED,
            tx_task__status__in=cls.IN_FLIGHT_TX_STATUSES,
            tx_task__evm_task__isnull=False,
        ).values_list(
            "tx_task__evm_task__gas",
            "tx_task__evm_task__gas_price",
        )
        for gas, gas_price in pending_tasks:
            if gas and gas_price:
                reserved += int(gas) * int(gas_price)
        # 安全系数补偿：历史签名时 gas_price 可能低于当前实际消耗
        return int(Decimal(reserved) * cls.GAS_RESERVE_SAFETY_FACTOR)

    @staticmethod
    def pending_amount_raw(*, project, chain, crypto, decimals: int) -> int:
        """统计同项目、同链、同币种在途提币已占用的资产数量。"""
        pending_amount = Withdrawal.objects.filter(
            project=project,
            chain=chain,
            crypto=crypto,
            review_status=WithdrawalReviewStatus.APPROVED,
            tx_task__status__in=WithdrawalService.IN_FLIGHT_TX_STATUSES,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        return int(pending_amount * Decimal(10**decimals))

    @classmethod
    def has_sufficient_balance(
        cls,
        *,
        project,
        chain,
        crypto,
        address: str,
        amount,
        adapter,
    ) -> bool:
        """统一计算提币可用余额，保证软检查与锁内复核使用同一套规则。"""
        decimals = crypto.get_decimals(chain)
        value_raw = int(amount * Decimal(10**decimals))
        if value_raw <= 0:
            return False

        pending_asset_raw = cls.pending_amount_raw(
            project=project,
            chain=chain,
            crypto=crypto,
            decimals=decimals,
        )
        current_fee_raw = cls.estimate_current_network_fee_raw(
            chain=chain, crypto=crypto
        )
        pending_gas_raw = cls.pending_gas_reserved_raw(project=project, chain=chain)

        on_chain_asset_raw = adapter.get_balance(address, chain, crypto)
        if crypto == chain.native_coin:
            # 原生币提币既消耗转出金额，也消耗 gas；在途单子的 gas 必须一起预留。
            available_raw = max(
                0, on_chain_asset_raw - pending_asset_raw - pending_gas_raw
            )
            return available_raw >= value_raw + current_fee_raw

        asset_available_raw = max(0, on_chain_asset_raw - pending_asset_raw)
        if asset_available_raw < value_raw:
            return False

        native_available_raw = adapter.get_balance(address, chain, chain.native_coin)
        native_available_raw = max(0, native_available_raw - pending_gas_raw)
        return native_available_raw >= current_fee_raw

    @staticmethod
    def estimate_withdrawal_worth(*, crypto, amount) -> Decimal:
        """统一计算提币美元价值，供限额与落库共用。"""
        worth = crypto.usd_amount(amount)
        return Decimal(worth)

    @classmethod
    def policy_used_worth_today(
        cls, *, project, exclude_withdrawal_id: int | None = None
    ) -> Decimal:
        """统计当天已占用的提币额度；审核中单据同样占用额度，防止排队绕过日限额。"""
        queryset = Withdrawal.objects.filter(
            project=project,
            review_status__in=cls.POLICY_TRACKED_REVIEW_STATUSES,
            created_at__date=timezone.localdate(),
        ).exclude(tx_task__status=TxTaskStatus.FAILED)
        if exclude_withdrawal_id is not None:
            queryset = queryset.exclude(pk=exclude_withdrawal_id)
        return queryset.aggregate(total=Sum("worth"))["total"] or Decimal("0")

    @classmethod
    def assert_project_policy(
        cls,
        *,
        project,
        chain,
        crypto,
        to: str,
        amount,
        exclude_withdrawal_id: int | None = None,
    ) -> Decimal:
        """在创建/审核前统一执行项目级风控，避免 API 与后台各自维护一套规则。"""
        needs_worth = any(
            limit is not None and limit > 0
            for limit in (
                project.withdrawal_review_exempt_limit,
                project.withdrawal_single_limit,
                project.withdrawal_daily_limit,
            )
        )
        if not needs_worth:
            return Decimal("0")

        try:
            worth = cls.estimate_withdrawal_worth(crypto=crypto, amount=amount)
        except Exception as exc:
            logger.exception(
                "计算提币 USD 价值失败，无法执行限额校验",
                project_id=project.pk,
                chain=chain.code,
                crypto=crypto.symbol,
            )
            raise APIError(
                ErrorCode.PARAMETER_ERROR, detail="无法计算提币 USD 价值"
            ) from exc

        if (
            project.withdrawal_single_limit is not None
            and 0 < project.withdrawal_single_limit < worth
        ):
            raise APIError(
                ErrorCode.WITHDRAWAL_SINGLE_LIMIT_EXCEEDED,
                detail={
                    "worth": str(worth),
                    "limit": str(project.withdrawal_single_limit),
                },
            )

        if (
            project.withdrawal_daily_limit is not None
            and project.withdrawal_daily_limit > 0
        ):
            used_today = cls.policy_used_worth_today(
                project=project,
                exclude_withdrawal_id=exclude_withdrawal_id,
            )
            if used_today + worth > project.withdrawal_daily_limit:
                raise APIError(
                    ErrorCode.WITHDRAWAL_DAILY_LIMIT_EXCEEDED,
                    detail={
                        "worth": str(worth),
                        "used_today": str(used_today),
                        "limit": str(project.withdrawal_daily_limit),
                    },
                )

        return worth

    @staticmethod
    def should_require_review(*, project, worth: Decimal) -> bool:
        """审核开关开启后，允许低价值提币按项目门槛直接放行，减少人工审核噪音。"""
        if not project.withdrawal_review_required:
            return False
        return not (
            project.withdrawal_review_exempt_limit is not None
            and project.withdrawal_review_exempt_limit > 0
            and worth < project.withdrawal_review_exempt_limit
        )

    @classmethod
    def _make_balance_verify_fn(
        cls, *, project, chain, crypto, address, amount, adapter
    ):
        """把余额二次验证闭包化，确保链上签名前仍按最新在途状态复核余额。"""

        def verify():
            if not cls.has_sufficient_balance(
                project=project,
                chain=chain,
                crypto=crypto,
                address=address,
                amount=amount,
                adapter=adapter,
            ):
                raise APIError(ErrorCode.INSUFFICIENT_BALANCE)

        return verify

    @classmethod
    def _schedule_evm_withdrawal(
        cls, *, vault_address, chain, crypto, to, value_raw, verify_fn
    ):
        """EVM 提币写入统一链上任务队列，广播由定时任务异步完成。"""
        from evm.intents import build_erc20_transfer_intent  # noqa: PLC0415
        from evm.intents import build_native_transfer_intent  # noqa: PLC0415
        from evm.models import EvmTxTask  # noqa: PLC0415

        if crypto == chain.native_coin:
            intent = build_native_transfer_intent(
                sender=vault_address,
                chain=chain,
                to=to,
                value=value_raw,
                tx_type=TxTaskType.Withdrawal,
                verify_fn=verify_fn,
            )
        else:
            intent = build_erc20_transfer_intent(
                sender=vault_address,
                chain=chain,
                crypto=crypto,
                to=to,
                value_raw=value_raw,
                tx_type=TxTaskType.Withdrawal,
                verify_fn=verify_fn,
            )
        task = EvmTxTask.schedule(intent)
        return task.base_task

    @classmethod
    def submit_withdrawal(cls, *, withdrawal: Withdrawal) -> Withdrawal:
        """把审核通过的提币请求真正送入链上发送队列。"""
        # 提币含可空外键（如 chain/tx_task），这里避免 select_related + FOR UPDATE 触发 PostgreSQL 限制。
        withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal.pk)
        if withdrawal.review_status not in (
            WithdrawalReviewStatus.REVIEWING,
            WithdrawalReviewStatus.APPROVED,
        ):
            raise ValueError(
                "仅审核中/待执行的提币单可以进入链上发送队列："
                f"withdrawal_id={withdrawal.pk} review_status={withdrawal.review_status}"
            )
        if withdrawal.tx_task_id:
            return withdrawal

        project = withdrawal.project
        chain = withdrawal.chain
        crypto = withdrawal.crypto
        amount = withdrawal.amount

        vault_address = project.wallet.get_address(
            chain_type=chain.type,
            usage=AddressUsage.HotWallet,
        )
        adapter = AdapterFactory.get_adapter(chain.type)
        verify_fn = cls._make_balance_verify_fn(
            project=project,
            chain=chain,
            crypto=crypto,
            address=vault_address.address,
            amount=amount,
            adapter=adapter,
        )

        decimals = crypto.get_decimals(chain)
        value_raw = int(amount * Decimal(10**decimals))
        if value_raw <= 0:
            raise APIError(ErrorCode.PARAMETER_ERROR)

        if chain.type != ChainType.EVM:
            raise APIError(ErrorCode.INVALID_CHAIN)

        tx_task = cls._schedule_evm_withdrawal(
            vault_address=vault_address,
            chain=chain,
            crypto=crypto,
            to=withdrawal.to,
            value_raw=value_raw,
            verify_fn=verify_fn,
        )

        # 提币请求只有在链上任务创建成功后才切到 APPROVED，避免"已批准但无任务"。
        withdrawal.tx_task = tx_task
        withdrawal.hash = tx_task.tx_hash
        withdrawal.review_status = WithdrawalReviewStatus.APPROVED
        withdrawal.save(
            update_fields=["tx_task", "hash", "review_status", "updated_at"]
        )
        return withdrawal

    @staticmethod
    def initialize_withdrawal(withdrawal: Withdrawal) -> Withdrawal:
        """显式计算提币 worth，替代历史 post_save signal。"""
        try:
            worth = WithdrawalService.estimate_withdrawal_worth(
                crypto=withdrawal.crypto,
                amount=withdrawal.amount,
            )
        except Exception:
            logger.exception(
                "calculate_worth 失败，worth 保持默认值 0",
                withdrawal_id=withdrawal.pk,
            )
            return withdrawal

        Withdrawal.objects.filter(pk=withdrawal.pk).update(
            worth=worth,
        )
        withdrawal.worth = worth
        return withdrawal

    @staticmethod
    def _ensure_reviewer_permission(*, reviewer, withdrawal: Withdrawal) -> None:
        """提币审核仅超管可操作。"""
        if reviewer is None:
            msg = "审核人不能为空"
            raise PermissionError(msg)

        if reviewer.is_superuser:
            return

        msg = (
            "仅超管可以审核提币："
            f"withdrawal_id={withdrawal.pk} reviewer_id={reviewer.pk}"
        )
        raise PermissionError(msg)

    @staticmethod
    def _create_review_log(
        *,
        withdrawal: Withdrawal,
        actor,
        action: str,
        from_review_status: str,
        to_review_status: str,
        note: str = "",
        approval_context: dict[str, object] | None = None,
    ) -> WithdrawalReviewLog:
        """每次审核决策都必须落审计日志，便于运营追溯与责任定位。"""
        snapshot = {
            "out_no": withdrawal.out_no,
            "chain": withdrawal.chain.code,
            "crypto": withdrawal.crypto.symbol,
            "amount": str(withdrawal.amount),
            "worth": str(withdrawal.worth),
            "to": withdrawal.to,
        }
        if approval_context is not None:
            # 审批上下文需要随审核日志持久化，便于事后确认是否满足 OTP 新鲜度约束。
            snapshot["approval_context"] = approval_context
        return WithdrawalReviewLog.objects.create(
            withdrawal=withdrawal,
            project=withdrawal.project,
            actor=actor,
            action=action,
            from_review_status=from_review_status,
            to_review_status=to_review_status,
            note=note,
            snapshot=snapshot,
        )

    @classmethod
    @db_transaction.atomic
    def approve_withdrawal(
        cls,
        *,
        withdrawal_id: int,
        reviewer,
        note: str = "",
        approval_context: dict[str, object] | None = None,
    ) -> Withdrawal:
        """后台批准后，才真正把提币请求推进到链上发送队列。"""
        # 加锁顺序：先 Project 再 Withdrawal，与 reject_withdrawal 对齐，防止死锁。
        project_id = Withdrawal.objects.values_list("project_id", flat=True).get(
            pk=withdrawal_id
        )
        from projects.models import Project

        project = Project.objects.select_for_update().get(pk=project_id)
        withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal_id)
        normalized_approval_context = validate_admin_approval_context(
            context=approval_context
        )
        cls._ensure_reviewer_permission(
            reviewer=reviewer,
            withdrawal=withdrawal,
        )
        if withdrawal.review_status != WithdrawalReviewStatus.REVIEWING:
            raise ValueError(
                "仅审核中的提币单可以批准："
                f"withdrawal_id={withdrawal.pk} review_status={withdrawal.review_status}"
            )

        from_review_status = withdrawal.review_status
        cls.assert_project_policy(
            project=project,
            chain=withdrawal.chain,
            crypto=withdrawal.crypto,
            to=withdrawal.to,
            amount=withdrawal.amount,
            exclude_withdrawal_id=withdrawal.pk,
        )
        withdrawal = cls.submit_withdrawal(withdrawal=withdrawal)
        withdrawal.reviewed_by = reviewer
        withdrawal.reviewed_at = timezone.now()
        withdrawal.save(update_fields=["reviewed_by", "reviewed_at", "updated_at"])
        cls._create_review_log(
            withdrawal=withdrawal,
            actor=reviewer,
            action=WithdrawalReviewLog.Action.APPROVED,
            from_review_status=from_review_status,
            to_review_status=withdrawal.review_status,
            note=note,
            approval_context=normalized_approval_context,
        )
        return withdrawal

    @classmethod
    @db_transaction.atomic
    def reject_withdrawal(
        cls,
        *,
        withdrawal_id: int,
        reviewer,
        note: str = "",
        approval_context: dict[str, object] | None = None,
    ) -> Withdrawal:
        """后台拒绝审核中的提币请求，直接终局为 REJECTED。"""
        # 加锁顺序：先 Project 再 Withdrawal，与 approve_withdrawal 对齐，防止死锁。
        project_id = Withdrawal.objects.values_list("project_id", flat=True).get(
            pk=withdrawal_id
        )
        from projects.models import Project

        Project.objects.select_for_update().get(pk=project_id)
        withdrawal = Withdrawal.objects.select_for_update().get(pk=withdrawal_id)
        normalized_approval_context = validate_admin_approval_context(
            context=approval_context
        )
        cls._ensure_reviewer_permission(
            reviewer=reviewer,
            withdrawal=withdrawal,
        )
        if withdrawal.review_status != WithdrawalReviewStatus.REVIEWING:
            raise ValueError(
                "仅审核中的提币单可以拒绝："
                f"withdrawal_id={withdrawal.pk} review_status={withdrawal.review_status}"
            )

        from_review_status = withdrawal.review_status
        withdrawal.review_status = WithdrawalReviewStatus.REJECTED
        withdrawal.reviewed_by = reviewer
        withdrawal.reviewed_at = timezone.now()
        withdrawal.save(
            update_fields=[
                "review_status",
                "reviewed_by",
                "reviewed_at",
                "updated_at",
            ]
        )
        cls._create_review_log(
            withdrawal=withdrawal,
            actor=reviewer,
            action=WithdrawalReviewLog.Action.REJECTED,
            from_review_status=from_review_status,
            to_review_status=withdrawal.review_status,
            note=note,
            approval_context=normalized_approval_context,
        )
        cls.notify_status_changed(withdrawal)
        return withdrawal

    @staticmethod
    def notify_status_changed(withdrawal: Withdrawal) -> None:
        """注册事务提交后的 Webhook 通知，保证事务回滚时不会发出错误通知。

        仅对 PENDING_CONFIRM / CONFIRMED 两个链上阶段发通知；
        必须在 @db_transaction.atomic 块内调用；事务提交后才真正创建 Webhook Event。
        """
        if not withdrawal.tx_task_id or withdrawal.tx_task.status not in (
            TxTaskStatus.PENDING_CONFIRM,
            TxTaskStatus.CONFIRMED,
        ):
            return
        # 缓存当前值，避免闭包捕获 ORM 对象在 on_commit 时已过期
        project = withdrawal.project
        payload = WithdrawalService.build_webhook_payload(withdrawal)
        withdrawal_pk = withdrawal.pk
        tx_status = withdrawal.tx_task.status

        def _send():
            try:
                WebhookService.create_event(project=project, payload=payload)
            except Exception:
                logger.exception(
                    "发送提币状态通知失败",
                    withdrawal_id=withdrawal_pk,
                    tx_status=tx_status,
                )

        db_transaction.on_commit(_send)

    @staticmethod
    @db_transaction.atomic
    def try_match_withdrawal(
        transfer: "Transfer",
        tx_task: "TxTask",
    ):
        # Withdrawal 不参与 ConfirmMode 判断，始终使用模型默认值 FULL，走完整区块确认流程。
        try:
            # 对 Withdrawal 加行锁，防止重复推送导致并发匹配同一笔提币
            withdrawal = Withdrawal.objects.select_for_update().get(
                tx_task=tx_task,
            )
        except Withdrawal.DoesNotExist:
            return False

        update_fields = ["transfer", "updated_at"]

        # 提币 chain 在创建时已强制写入，链不一致即视为错配，直接忽略。
        if withdrawal.chain_id != transfer.chain_id:
            logger.warning(
                "链不匹配，忽略提币匹配",
                withdrawal_id=withdrawal.id,
                transfer_hash=transfer.hash,
                expected_chain_id=withdrawal.chain_id,
                actual_chain_id=transfer.chain_id,
            )
            return False

        expected_chain = withdrawal.chain
        expected_value = raw_amount(
            amount=withdrawal.amount,
            crypto=withdrawal.crypto,
            chain=expected_chain,
        )
        if not transfer_matches(
            transfer,
            chain=expected_chain,
            crypto=withdrawal.crypto,
            from_address=tx_task.sender.address,
            to_address=withdrawal.to,
            value=expected_value,
        ):
            logger.warning(
                "提币链上转账与提币单不匹配，忽略",
                withdrawal_id=withdrawal.id,
                tx_task_id=tx_task.pk,
                transfer_id=transfer.pk,
                tx_hash=transfer.hash,
            )
            return False

        if withdrawal.review_status != WithdrawalReviewStatus.APPROVED:
            # 非 APPROVED 通常意味着审核拒绝或异常脏数据，不应抛异常
            # 抛 TypeError 会导致 Celery 任务失败并进入重试风暴
            logger.warning(
                "提币未批准，忽略匹配（可能为重复事件）",
                withdrawal_id=withdrawal.id,
                current_review_status=withdrawal.review_status,
                transfer_hash=transfer.hash,
            )
            return False

        withdrawal.transfer = transfer
        # 合并写入，避免 chain 为 None 时产生两次 DB 写操作
        # 明确 update_fields 防止覆盖其他字段的并发修改
        withdrawal.save(update_fields=update_fields)
        if withdrawal.tx_task_id:
            # 提币一旦命中链上转账，就进入"待确认"；真正稳定成功仍要等确认数达标。
            TxTask.mark_pending_confirm(
                chain=transfer.chain,
                tx_hash=transfer.hash,
            )
            withdrawal.tx_task.status = TxTaskStatus.PENDING_CONFIRM
        WithdrawalService.notify_status_changed(withdrawal)

        transfer.type = TransferType.Withdrawal
        transfer.save(update_fields=["type"])
        return True

    @classmethod
    @db_transaction.atomic
    def confirm_withdrawal(cls, transfer: "Transfer"):
        # 对 Withdrawal 加行锁，防止 Celery 重试并发确认同一笔提币
        withdrawal = Withdrawal.objects.select_for_update().get(transfer=transfer)

        if withdrawal.review_status != WithdrawalReviewStatus.APPROVED:
            raise ValueError(
                f"提币未批准，无法确认："
                f"withdrawal_id={withdrawal.id} review_status={withdrawal.review_status}"
            )
        if withdrawal.tx_task.status != TxTaskStatus.CONFIRMED:
            # 此路径代表业务逻辑错误，使用 ValueError 快速暴露
            raise ValueError(
                f"提币状态异常，无法确认："
                f"withdrawal_id={withdrawal.id} tx_status={withdrawal.tx_task.status}"
            )

        cls.notify_status_changed(withdrawal)
        # 开源版不再累计内部费率统计，提币确认仅保留业务状态变更。

        send_internal_callback(
            event="withdrawal.confirmed",
            appid=withdrawal.project.appid,
            sys_no=withdrawal.sys_no,
            worth=str(withdrawal.worth),
            currency=withdrawal.crypto.symbol,
        )

    @staticmethod
    @db_transaction.atomic
    def drop_withdrawal(transfer: "Transfer"):
        """Transfer 被 drop 仅意味着当前观测到的链上记录消失，不代表交易永久失败。

        TxTask 会被 Transfer.drop() 回退到 PENDING_CHAIN 继续观察/重广播。
        Withdrawal 只清除 transfer 关联，链上回退事实由 TxTask.status 表达。
        只有 TxTask 真正进入 FAILED 终局时才代表链上失败。
        """
        # 对 Withdrawal 加行锁，防止并发 drop 同一笔提币
        withdrawal = Withdrawal.objects.select_for_update().get(transfer=transfer)

        if withdrawal.review_status == WithdrawalReviewStatus.REJECTED:
            return
        if withdrawal.tx_task_id and withdrawal.tx_task.status in (
            TxTaskStatus.CONFIRMED,
            TxTaskStatus.FAILED,
        ):
            return

        if withdrawal.review_status != WithdrawalReviewStatus.APPROVED:
            raise ValueError(
                f"提币状态异常，无法回退："
                f"withdrawal_id={withdrawal.id} review_status={withdrawal.review_status}"
            )

        # 清除已删除的 Transfer 关联，等待 TxTask 重广播/重匹配。
        withdrawal.transfer = None
        withdrawal.save(update_fields=["transfer", "updated_at"])

    @classmethod
    def fail_withdrawal(cls, *, tx_task) -> None:
        """TxTask 确认链上交易永久失败时，清理提币链上关联。

        调用时机：TxTask 进入 FAILED 终局状态后，由链特定模块回调。
        与 REJECTED（人工审核拒绝）语义完全不同：FAILED 属于 tx_task.status。
        """
        try:
            withdrawal = Withdrawal.objects.select_for_update().get(
                tx_task=tx_task,
            )
        except Withdrawal.DoesNotExist:
            return

        # 幂等保护：已终局或已拒绝则跳过
        if tx_task.status == TxTaskStatus.CONFIRMED:
            return
        if withdrawal.review_status == WithdrawalReviewStatus.REJECTED:
            return

        if withdrawal.review_status != WithdrawalReviewStatus.APPROVED:
            raise ValueError(
                f"提币状态异常，无法标记失败："
                f"withdrawal_id={withdrawal.id} review_status={withdrawal.review_status}"
            )

        withdrawal.transfer = None
        withdrawal.save(update_fields=["transfer", "updated_at"])
        cls.notify_status_changed(withdrawal)
