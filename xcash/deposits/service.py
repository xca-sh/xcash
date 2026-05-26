from __future__ import annotations

import structlog
from django.db import transaction as db_transaction
from django.utils import timezone
from risk.tasks import mark_deposit_risk

from chains.models import Transfer
from chains.models import TransferType
from common.internal_callback import send_internal_callback
from common.utils.math import format_decimal_stripped
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from deposits.models import DepositStatus
from evm.models import DepositSlot
from evm.models import DepositSlotUsage
from webhooks.service import WebhookService

logger = structlog.get_logger()


class DepositService:
    """DepositSlot 收款体系下的充值生命周期。"""

    @staticmethod
    def build_webhook_payload(
        deposit: Deposit, *, confirmed: bool | None = None
    ) -> dict:
        if confirmed is None:
            confirmed = deposit.status == DepositStatus.COMPLETED

        customer = deposit.customer
        return {
            "type": "deposit",
            "data": {
                "sys_no": deposit.sys_no,
                "uid": customer.uid if customer else None,
                "chain": deposit.transfer.chain.code,
                "block": deposit.transfer.block,
                "hash": deposit.transfer.hash,
                "crypto": deposit.transfer.crypto.symbol,
                "amount": format_decimal_stripped(deposit.transfer.amount),
                "confirmed": confirmed,
                "risk_level": deposit.risk_level,
                "risk_score": (
                    format_decimal_stripped(deposit.risk_score)
                    if deposit.risk_score is not None
                    else None
                ),
            },
        }

    @staticmethod
    def refresh_worth(deposit: Deposit) -> None:
        try:
            worth = deposit.transfer.crypto.usd_amount(deposit.transfer.amount)
        except Exception:
            logger.exception(
                "calculate_worth 失败，worth 保持默认值 0", deposit_id=deposit.pk
            )
            return

        Deposit.objects.filter(pk=deposit.pk).update(
            worth=worth,
            updated_at=timezone.now(),
        )
        deposit.worth = worth

    @classmethod
    def _notify(cls, deposit: Deposit, status: str) -> None:
        payload = cls.build_webhook_payload(
            deposit, confirmed=status == DepositStatus.COMPLETED
        )
        try:
            WebhookService.create_event(
                project=deposit.customer.project, payload=payload
            )
        except Exception:
            logger.exception("创建充币 webhook 通知失败", deposit_id=deposit.pk)

    @classmethod
    def _pre_notify(cls, deposit: Deposit) -> None:
        if deposit.customer.project.pre_notify:
            cls._notify(deposit, DepositStatus.CONFIRMING)

    @classmethod
    def notify_completed(cls, deposit: Deposit) -> None:
        cls._notify(deposit, DepositStatus.COMPLETED)

    @classmethod
    def initialize_deposit(cls, deposit: Deposit) -> Deposit:
        cls.refresh_worth(deposit)
        cls._pre_notify(deposit)
        return deposit

    @classmethod
    def try_create_deposit(cls, transfer: Transfer) -> bool:
        if not transfer.crypto.active:
            return False

        try:
            customer = DepositSlot.objects.get(
                chain=transfer.chain,
                address=transfer.to_address,
                usage=DepositSlotUsage.DEPOSIT,
            ).customer
        except DepositSlot.DoesNotExist:
            return False

        transfer.type = TransferType.Deposit
        transfer.save(update_fields=["type"])

        deposit = Deposit.objects.create(
            customer=customer,
            transfer=transfer,
            status=DepositStatus.CONFIRMING,
        )
        cls.initialize_deposit(deposit)
        db_transaction.on_commit(lambda: mark_deposit_risk.delay(deposit.pk))
        return True

    @classmethod
    @db_transaction.atomic
    def _transition_status(cls, deposit: Deposit, target: str) -> bool:
        Deposit.objects.select_for_update().filter(pk=deposit.pk).first()
        deposit.refresh_from_db()

        if deposit.status == target:
            return False
        if deposit.status != DepositStatus.CONFIRMING:
            raise DepositStatusError("Deposit status must be CONFIRMING")

        deposit.status = target
        deposit.save(update_fields=["status", "updated_at"])
        return True

    @classmethod
    def confirm_deposit(cls, deposit: Deposit) -> None:
        if cls._transition_status(deposit, DepositStatus.COMPLETED):
            try:
                cls.schedule_collect_for_completed_deposit(deposit)
            except Exception:
                logger.exception("调度 DepositSlot 归集任务失败", deposit_id=deposit.pk)
            cls.notify_completed(deposit)
            send_internal_callback(
                event="deposit.confirmed",
                appid=deposit.customer.project.appid,
                sys_no=deposit.sys_no,
                worth=str(deposit.worth),
                currency=deposit.transfer.crypto.symbol,
            )

    @staticmethod
    def schedule_collect_for_completed_deposit(deposit: Deposit) -> bool:
        deposit.refresh_from_db()
        if deposit.status != DepositStatus.COMPLETED:
            raise DepositStatusError("Deposit status must be COMPLETED")

        transfer = deposit.transfer
        if transfer.crypto_id == transfer.chain.native_coin_id:
            return False

        return DepositSlot.schedule_collect_for_deposit(deposit.pk) is not None

    @classmethod
    @db_transaction.atomic
    def drop_deposit(cls, deposit: Deposit) -> None:
        if not Deposit.objects.select_for_update().filter(pk=deposit.pk).exists():
            return
        deposit.refresh_from_db()
        if deposit.status != DepositStatus.CONFIRMING:
            raise DepositStatusError("Deposit status must be CONFIRMING")
        deposit.delete()
