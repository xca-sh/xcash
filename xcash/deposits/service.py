from __future__ import annotations

import structlog
from aml.tasks import screen_deposit_aml
from django.db import transaction as db_transaction
from django.utils import timezone

from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.vault_slots import schedule_collect_for_slot
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback
from common.utils.math import format_decimal_stripped
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from webhooks.service import WebhookService

logger = structlog.get_logger()


class DepositService:
    """智能合约收款体系下的充值生命周期。"""

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
    def notify_completed(cls, deposit: Deposit) -> None:
        cls._notify(deposit, confirmed=True)

    @classmethod
    def initialize_deposit(cls, deposit: Deposit) -> Deposit:
        cls.refresh_worth(deposit)
        return deposit

    @classmethod
    def try_match_deposit_transfer(cls, transfer: Transfer) -> bool:
        # 停用币（Crypto.active=False）也要归类入账：链上资金已实收，记账事实
        # 不能因运营停用而丢失；停用只挡新充币地址申请与商户 webhook 通知。
        if not VaultSlot.objects.filter(
            chain=transfer.chain,
            address=transfer.to_address,
            usage=VaultSlotUsage.DEPOSIT,
        ).exists():
            return False

        transfer.type = TransferType.Deposit
        transfer.save(update_fields=["type"])
        return True

    @classmethod
    def create_confirmed_deposit(cls, transfer: Transfer) -> Deposit | None:
        if transfer.status != TransferStatus.CONFIRMED:
            raise DepositStatusError("Deposit transfer must be confirmed")

        try:
            customer = VaultSlot.objects.get(
                chain=transfer.chain,
                address=transfer.to_address,
                usage=VaultSlotUsage.DEPOSIT,
            ).customer
        except VaultSlot.DoesNotExist:
            # type=Deposit 由 DEPOSIT slot 命中归类而来，确认时 slot 消失属于
            # 数据异常：Transfer 已 CONFIRMED 且不会重试，静默返回会造成
            # 「链上有钱、账上无单」的对账黑洞，必须留下高等级告警供人工补账。
            logger.exception(
                "已确认的充值转账找不到对应 DEPOSIT VaultSlot，未建 Deposit，需人工补账",
                transfer_id=transfer.pk,
                chain=transfer.chain.code,
                to_address=transfer.to_address,
                tx_hash=transfer.hash,
            )
            return None

        deposit, created = Deposit.objects.get_or_create(
            customer=customer,
            transfer=transfer,
        )
        if created:
            cls.initialize_deposit(deposit)
        cls.confirm_deposit(deposit)
        return deposit

    @classmethod
    def confirm_deposit(cls, deposit: Deposit) -> None:
        # 确认副作用（归集调度、webhook、内部回调）的「恰好一次」由上游
        # Transfer.confirm 的行锁 + 幂等护栏保证，这里不再维护独立状态机。
        try:
            cls.schedule_collect_for_completed_deposit(deposit)
        except Exception:  # noqa
            logger.exception("调度 VaultSlot 归集任务失败", deposit_id=deposit.pk)
        db_transaction.on_commit(lambda: screen_deposit_aml.delay(deposit.pk))
        if deposit.transfer.crypto.active:
            cls.notify_completed(deposit)
        else:
            # 停用币充值：记账、AML、归集、SaaS 计费照常，仅跳过商户 webhook——
            # 商户侧该币已下架，通知可能触发其自动上账逻辑。
            logger.warning(
                "停用币充值已入账，跳过商户 webhook 通知",
                deposit_id=deposit.pk,
                crypto=deposit.transfer.crypto.symbol,
                chain=deposit.transfer.chain.code,
            )
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

        return DepositService.schedule_collect_for_deposit(deposit.pk) is not None

    @staticmethod
    def schedule_collect_for_deposit(
        deposit_pk: int,
    ) -> VaultSlotCollectSchedule | None:
        """按充值定位其 VaultSlot，登记（或复用）该槽位该币种的待归集计划。"""
        deposit = Deposit.objects.select_related(
            "customer",
            "transfer__chain",
            "transfer__crypto",
        ).get(pk=deposit_pk)
        transfer = deposit.transfer
        chain = transfer.chain
        crypto = transfer.crypto

        try:
            slot = VaultSlot.objects.get(
                chain=chain,
                customer=deposit.customer,
                usage=VaultSlotUsage.DEPOSIT,
                address=transfer.to_address,
            )
        except VaultSlot.DoesNotExist as exc:
            raise RuntimeError(
                "VaultSlot 不存在："
                f"deposit_id={deposit.pk} chain={chain.code} "
                f"customer_id={deposit.customer_id} address={transfer.to_address}"
            ) from exc

        return schedule_collect_for_slot(chain=chain, crypto=crypto, slot=slot)
