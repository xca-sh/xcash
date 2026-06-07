from __future__ import annotations

import structlog
from aml.tasks import screen_deposit_aml
from django.db import transaction as db_transaction
from django.utils import timezone

from chains.models import Transfer
from chains.models import TransferType
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback
from common.utils.math import format_decimal_stripped
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from webhooks.service import WebhookService

logger = structlog.get_logger()


class DepositService:
    """VaultSlot 收款体系下的充值生命周期。"""

    @staticmethod
    def build_webhook_payload(
        deposit: Deposit, *, confirmed: bool | None = None
    ) -> dict:
        if confirmed is None:
            confirmed = deposit.confirmed

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
        except Exception:  # noqa
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
    def _notify(cls, deposit: Deposit, *, confirmed: bool) -> None:
        payload = cls.build_webhook_payload(deposit, confirmed=confirmed)
        try:
            WebhookService.create_event(
                project=deposit.customer.project, payload=payload
            )
        except Exception:  # noqa
            logger.exception("创建充币 webhook 通知失败", deposit_id=deposit.pk)

    @classmethod
    def _pre_notify(cls, deposit: Deposit) -> None:
        if deposit.customer.project.pre_notify:
            cls._notify(deposit, confirmed=False)

    @classmethod
    def notify_completed(cls, deposit: Deposit) -> None:
        cls._notify(deposit, confirmed=True)

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
            customer = VaultSlot.objects.get(
                chain=transfer.chain,
                address=transfer.to_address,
                usage=VaultSlotUsage.DEPOSIT,
            ).customer
        except VaultSlot.DoesNotExist:
            return False

        transfer.type = TransferType.Deposit
        transfer.save(update_fields=["type"])

        deposit = Deposit.objects.create(
            customer=customer,
            transfer=transfer,
        )
        cls.initialize_deposit(deposit)
        db_transaction.on_commit(lambda: screen_deposit_aml.delay(deposit.pk))
        return True

    @classmethod
    def confirm_deposit(cls, deposit: Deposit) -> None:
        # 确认副作用（归集调度、webhook、内部回调）的「恰好一次」由上游
        # Transfer.confirm 的行锁 + 幂等护栏保证，这里不再维护独立状态机。
        try:
            cls.schedule_collect_for_completed_deposit(deposit)
        except Exception:  # noqa
            logger.exception("调度 VaultSlot 归集任务失败", deposit_id=deposit.pk)
        cls.notify_completed(deposit)
        send_saas_callback(
            SaasCallback(
                event=CallbackEvent.DEPOSIT_CONFIRMED,
                appid=deposit.customer.project.appid,
                sys_no=deposit.sys_no,
                worth=str(deposit.worth),
                currency=deposit.transfer.crypto.symbol,
            )
        )

    @staticmethod
    def schedule_collect_for_completed_deposit(deposit: Deposit) -> bool:
        deposit.refresh_from_db()
        if not deposit.confirmed:
            raise DepositStatusError("Deposit transfer must be confirmed")

        transfer = deposit.transfer
        if transfer.crypto_id == transfer.chain.native_coin.pk:
            return False

        return VaultSlot.schedule_collect_for_deposit(deposit.pk) is not None

    @classmethod
    def drop_deposit(cls, deposit: Deposit) -> None:
        # Deposit 经 OneToOne(on_delete=CASCADE) 绑定 Transfer，Transfer.drop 删除转账时
        # 会级联清除未确认充值，无需在此显式删除。但已确认充值不允许随 Transfer 回退被
        # 静默抹除（典型为确认后 reorg），抛错中断整个 drop 事务交由人工排查。
        if deposit.confirmed:
            raise DepositStatusError("已确认充值不可回退")
