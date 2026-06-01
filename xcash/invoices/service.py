from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.utils import timezone
from risk.tasks import mark_invoice_risk

from chains.models import ConfirmMode
from chains.models import TransferType
from chains.service import ChainService
from chains.service import TransferService
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.internal_callback import send_internal_callback
from common.utils.math import format_decimal_stripped
from currencies.service import CryptoService
from currencies.service import FiatService
from evm.models import VaultSlot
from webhooks.service import WebhookService

from .exceptions import InvoiceStatusError
from .models import Invoice
from .models import InvoiceBillingMode
from .models import InvoiceProtocol
from .models import InvoiceStatus

if TYPE_CHECKING:
    from chains.models import Transfer

logger = structlog.get_logger()


class InvoiceService:
    @staticmethod
    def finalize_methods(
        *,
        project,
        billing_mode: str,
        requested,
        currency: str | None = None,
    ) -> dict[str, list[str]]:
        """按 billing_mode 生成/收敛账单最终 methods。

        Invoice.available_methods(project, billing_mode) 是该模式下真正可付款的
        crypto -> chain 集合；调用方未指定 methods 时直接采用全集，指定时必须是全集
        子集。计价货币本身是加密货币时，最终 methods 还会收敛到该单一币种。
        """
        available = Invoice.available_methods(project, billing_mode)
        if not available:
            raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS)

        if not requested:
            finalized = available
        else:
            if not isinstance(requested, dict):
                raise APIError(ErrorCode.PARAMETER_ERROR, detail="methods")

            finalized: dict[str, list[str]] = {}
            for crypto_symbol, chain_codes in requested.items():
                if not isinstance(chain_codes, (list, tuple)):
                    raise APIError(ErrorCode.PARAMETER_ERROR, detail=crypto_symbol)

                try:
                    CryptoService.get_by_symbol(crypto_symbol)
                except ObjectDoesNotExist as exc:
                    raise APIError(
                        ErrorCode.INVALID_CRYPTO,
                        detail=crypto_symbol,
                    ) from exc

                available_chains = set(available.get(crypto_symbol, []))
                if not available_chains:
                    raise APIError(
                        ErrorCode.NO_RECIPIENT_ADDRESS,
                        detail=crypto_symbol,
                    )

                normalized_codes: list[str] = []
                for chain_code in chain_codes:
                    if not isinstance(chain_code, str):
                        raise APIError(
                            ErrorCode.PARAMETER_ERROR,
                            detail=crypto_symbol,
                        )

                    try:
                        ChainService.get_by_code(chain_code)
                    except ObjectDoesNotExist as exc:
                        raise APIError(
                            ErrorCode.INVALID_CHAIN,
                            detail=chain_code,
                        ) from exc
                    if chain_code not in available_chains:
                        raise APIError(
                            ErrorCode.NO_RECIPIENT_ADDRESS,
                            detail=f"{crypto_symbol}:{chain_code}",
                        )
                    normalized_codes.append(chain_code)

                if normalized_codes:
                    finalized[crypto_symbol] = normalized_codes

            if not finalized:
                raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS)

        if currency and CryptoService.exists(currency):
            chains = finalized.get(currency, [])
            if not chains:
                raise APIError(ErrorCode.NO_AVAILABLE_METHOD)
            return {currency: chains}

        return finalized

    @staticmethod
    def refresh_initial_worth(invoice: Invoice) -> None:
        """在账单创建后立即固化基础 worth，避免继续依赖隐式 post_save signal。"""
        usd = FiatService.get_by_code("USD")

        if invoice.crypto and invoice.pay_amount:
            worth = invoice.crypto.to_fiat(fiat=usd, amount=invoice.pay_amount)
        elif invoice.is_crypto_fixed:
            crypto = CryptoService.get_by_symbol(invoice.currency)
            worth = crypto.to_fiat(fiat=usd, amount=invoice.amount)
        else:
            fiat = FiatService.get_by_code(invoice.currency)
            worth = invoice.amount * fiat.fiat_price(usd)

        # worth 只更新自身字段，直接 update 可避免把整行实例再次写回。
        Invoice.objects.filter(pk=invoice.pk).update(
            worth=worth,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

    @staticmethod
    def try_auto_select_single_method(invoice: Invoice) -> None:
        """仅当 methods 唯一时自动分配当前支付指引，替代历史 post_save signal。"""
        methods = invoice.methods or {}
        if len(methods) != 1:
            return

        symbol, chain_codes = next(iter(methods.items()))
        if len(chain_codes) != 1:
            return

        try:
            crypto = CryptoService.get_by_symbol(symbol)
            chain = ChainService.get_by_code(chain_codes[0])
            invoice.select_method(crypto, chain)
        except ObjectDoesNotExist:
            logger.warning(
                "initialize_invoice: resource missing",
                symbol=symbol,
                chain=chain_codes[0],
            )
        except Invoice.InvoiceAllocationError as exc:
            logger.warning("initialize_invoice allocation failed", detail=str(exc))

    @staticmethod
    def schedule_expiration_check(invoice: Invoice) -> None:
        """在事务提交后注册过期检查任务，避免回滚后仍派发悬空 Celery 任务。"""
        from functools import partial

        from .tasks import check_expired

        # 提前捕获值，避免闭包延迟绑定陷阱。
        eta = invoice.expires_at + timedelta(seconds=1)
        dispatch = partial(check_expired.apply_async, (invoice.id,), eta=eta)
        transaction.on_commit(dispatch)

    @classmethod
    def initialize_invoice(cls, invoice: Invoice) -> Invoice:
        """账单创建后的显式初始化入口：worth、自动支付方式、过期任务。"""
        cls.refresh_initial_worth(invoice)
        cls.try_auto_select_single_method(invoice)
        cls.schedule_expiration_check(invoice)
        return invoice

    @staticmethod
    def build_webhook_payload(invoice: Invoice) -> dict:
        """构建 webhook 推送给商户的 payload，与 model 层解耦。

        将 payload 结构集中在 service 层管理，便于未来版本化或按场景差异化。
        """
        return {
            "type": "invoice",
            "data": {
                "sys_no": invoice.sys_no,
                "out_no": invoice.out_no,
                "crypto": invoice.crypto.symbol if invoice.crypto else None,
                "chain": invoice.transfer.chain.code if invoice.transfer_id else None,
                "pay_address": invoice.pay_address,
                "pay_amount": (
                    format_decimal_stripped(invoice.pay_amount)
                    if invoice.pay_amount is not None
                    else None
                ),
                "hash": invoice.transfer.hash if invoice.transfer_id else None,
                "block": invoice.transfer.block if invoice.transfer_id else None,
                "confirmed": invoice.status == InvoiceStatus.COMPLETED,
                "risk_level": invoice.risk_level,
                "risk_score": (
                    format_decimal_stripped(invoice.risk_score)
                    if invoice.risk_score is not None
                    else None
                ),
            },
        }

    @staticmethod
    @transaction.atomic
    def try_match_invoice(
        transfer: Transfer,
    ):
        # 只匹配账单当前支付指引。用户切换支付方式后，旧指引不再作为该账单的
        # 自动入账入口，避免为低概率误付款保留多槽位状态机。
        base_filter = Invoice.objects.filter(
            chain=transfer.chain,
            crypto=transfer.crypto,
            pay_address=transfer.to_address,
            started_at__lte=transfer.datetime,
            expires_at__gte=transfer.datetime,
            status__in=[InvoiceStatus.WAITING, InvoiceStatus.EXPIRED],
        )

        differ_candidate = (
            base_filter.filter(
                billing_mode=InvoiceBillingMode.DIFFER,
                pay_amount=transfer.amount,
            )
            .order_by("-started_at", "-pk")
            .values("pk")
            .first()
        )
        contract_candidate = (
            base_filter.filter(
                billing_mode=InvoiceBillingMode.CONTRACT,
                pay_amount=transfer.amount,
            )
            .order_by("-started_at", "-pk")
            .values("pk")
            .first()
        )
        candidate = differ_candidate or contract_candidate
        if candidate is None:
            return False

        # 锁住 Invoice（与 select_method 保持相同的加锁对象，防止切换支付方式
        # 与链上匹配并发覆盖）。
        # of=("self",) 限定行锁只作用于 invoices_invoice，避免 select_related 触发
        # PostgreSQL 把 projects_project / currencies_crypto 等 join 父表也锁成
        # FOR UPDATE，与并发 INSERT/UPDATE 子表时 PG 自动加的 FK FOR KEY SHARE 互斥而死锁。
        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("project", "crypto", "chain")
            .filter(pk=candidate["pk"])
            .first()
        )
        if invoice is None:
            return False

        if not InvoiceService._transfer_matches_current_payment(invoice, transfer):
            return False

        confirm_mode = (
            ConfirmMode.QUICK
            if invoice.project.fast_confirm_threshold > invoice.worth
            else ConfirmMode.FULL
        )
        transfer = TransferService.assign_type_and_mode(
            transfer, TransferType.Invoice, confirm_mode
        )

        # 账单状态更新不依赖 post_save 副作用，直接 update 可避免实例整行回写。
        Invoice.objects.filter(pk=invoice.pk).update(
            transfer_id=transfer.pk,
            status=InvoiceStatus.CONFIRMING,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

        transaction.on_commit(lambda: mark_invoice_risk.delay(invoice.pk))

        # EPAY_V1 为托管模式，交易即时确认，不存在链上等待区块确认的阶段，
        # 预通知对其无意义；其完成通知由 EpaySubmitService 独立处理。
        if (
            invoice.project.pre_notify
            and invoice.protocol == InvoiceProtocol.NATIVE
            and confirm_mode == ConfirmMode.FULL
        ):
            # 嵌套 atomic 建立 savepoint：即便 create_event 触发 DatabaseError
            # 把当前连接标记为 needs_rollback，回滚也只发生在 savepoint 内，
            # 外层 invoice 匹配事务仍能成功提交。
            try:
                with transaction.atomic():
                    WebhookService.create_event(
                        project=invoice.project,
                        payload=InvoiceService.build_webhook_payload(invoice),
                        delivery_url=invoice.notify_url,
                    )
            except Exception:
                logger.exception("发送账单预通知失败", invoice_id=invoice.pk)

        return True

    @staticmethod
    def _transfer_matches_current_payment(invoice: Invoice, transfer: Transfer) -> bool:
        if invoice.status not in [InvoiceStatus.WAITING, InvoiceStatus.EXPIRED]:
            return False
        if invoice.crypto_id != transfer.crypto_id or invoice.chain_id != transfer.chain_id:
            return False
        if invoice.pay_address != transfer.to_address:
            return False
        if not (invoice.started_at <= transfer.datetime <= invoice.expires_at):
            return False
        if invoice.billing_mode == InvoiceBillingMode.CONTRACT:
            return invoice.pay_amount is not None and invoice.pay_amount <= transfer.amount
        return invoice.pay_amount == transfer.amount

    @classmethod
    @transaction.atomic
    def confirm_invoice(
        cls,
        invoice: Invoice,
    ):
        # 必须在本方法内对 Invoice 加行锁，不能仅依赖调用方（Transfer.confirm）持有
        # Transfer 锁——其他调用路径可能绕开 Transfer 锁直接调用此方法。
        # of=("self",) 把锁限定在 invoices_invoice，避免连带锁 projects_project 引发死锁。
        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("project")
            .get(pk=invoice.pk)
        )
        if invoice.status != InvoiceStatus.CONFIRMING:
            raise InvoiceStatusError(f"Invoice must be confirming, {invoice.sys_no}")

        # 账单确认不依赖 save() 信号，直接 update 可减少并发覆盖面。
        Invoice.objects.filter(pk=invoice.pk).update(
            status=InvoiceStatus.COMPLETED,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

        try:
            VaultSlot.schedule_collect_for_invoice(invoice.pk)
        except Exception:
            logger.exception("调度 Invoice VaultSlot 归集任务失败", invoice_id=invoice.pk)

        if invoice.protocol == InvoiceProtocol.EPAY_V1:
            from .epay.service import EpaySubmitService

            EpaySubmitService.enqueue_paid_notify(invoice)
        elif invoice.protocol == InvoiceProtocol.NATIVE:
            WebhookService.create_event(
                project=invoice.project,
                payload=cls.build_webhook_payload(invoice),
                delivery_url=invoice.notify_url,
            )
        # 设计决策：开源版本不计算内部手续费或月成交量统计，
        # 账单状态机在 COMPLETED 即为终局，无需后续财务核算步骤。

        send_internal_callback(
            event="invoice.confirmed",
            appid=invoice.project.appid,
            sys_no=invoice.sys_no,
            worth=str(invoice.worth),
            currency=invoice.crypto.symbol,
        )

    @classmethod
    @transaction.atomic
    def drop_invoice(
        cls,
        invoice: Invoice,
    ):
        # 必须在本方法内对 Invoice 加行锁，防止 confirm_invoice 与 drop_invoice
        # 并发执行导致状态错乱（如 drop 回退 WAITING 的同时 confirm 已推送 COMPLETED webhook）。
        # of=("self",) 把锁限定在 invoices_invoice，避免连带锁 projects_project 引发死锁。
        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("project")
            .get(pk=invoice.pk)
        )
        if invoice.status != InvoiceStatus.CONFIRMING:
            raise InvoiceStatusError("Invoice must be confirming")

        if InvoiceService._current_payment_is_occupied_by_waiting_invoice(invoice):
            # 账单确认被回退时，当前支付组合可能已经被新的 WAITING 账单复用。
            # 此时不能重新占用旧组合，直接清空支付指引，让用户重新选择。
            logger.warning(
                "drop_invoice: current payment occupied, clearing payment",
                invoice=invoice.sys_no,
            )
            Invoice.objects.filter(pk=invoice.pk).update(
                crypto=None,
                chain=None,
                pay_address=None,
                pay_amount=None,
                updated_at=timezone.now(),
            )

        # 账单回退状态：若已过期则恢复为 EXPIRED 而非 WAITING，避免僵尸账单。
        rollback_status = (
            InvoiceStatus.EXPIRED
            if invoice.expires_at <= timezone.now()
            else InvoiceStatus.WAITING
        )
        Invoice.objects.filter(pk=invoice.pk).update(
            status=rollback_status,
            transfer=None,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

    @staticmethod
    def _current_payment_is_occupied_by_waiting_invoice(invoice: Invoice) -> bool:
        if (
            invoice.crypto_id is None
            or invoice.chain_id is None
            or not invoice.pay_address
            or invoice.pay_amount is None
        ):
            return False
        # 占用判据与 uniq_invoice_active_payment 约束保持一致——只看 status=WAITING，
        # 不叠加 expires_at 过滤：约束锁定所有 WAITING 账单的 (pay_address, pay_amount)
        # 组合，若这里漏判"过期未翻转"的占用者，drop_invoice 把本账单回退为 WAITING 时
        # 会命中约束抛 IntegrityError，而 drop_invoice 未捕获，账单将卡死在 CONFIRMING。
        return Invoice.objects.filter(
            project=invoice.project,
            crypto=invoice.crypto,
            chain=invoice.chain,
            pay_address=invoice.pay_address,
            pay_amount=invoice.pay_amount,
            status=InvoiceStatus.WAITING,
        ).exclude(pk=invoice.pk).exists()
