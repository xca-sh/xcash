from __future__ import annotations

import re
from decimal import Decimal

import structlog
from aml.service import AmlScreeningService
from aml.tasks import enqueue_screen_transfer_aml
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone

from chains.capabilities import ChainProductCapabilityService
from chains.models import Chain
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.vault_slots import ensure_deposit_address
from chains.vault_slots import schedule_collect_for_slot
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.permission_check import get_saas_deposit_customer_limit
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback
from common.utils.math import format_decimal_stripped
from currencies.models import Crypto
from currencies.service import CryptoService
from deposits.exceptions import DepositStatusError
from deposits.models import Deposit
from projects.models import Customer
from projects.models import Project
from webhooks.service import WebhookService

logger = structlog.get_logger()

# 客户 uid 合法字符：字母、数字、下划线、中划线，长度 1~128
DEPOSIT_UID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")


class DepositService:
    """智能合约收款体系下的充值生命周期。"""

    @staticmethod
    def resolve_deposit_address(
        *, project: Project, uid: str, chain_code: str, crypto_symbol: str
    ) -> tuple[str, Chain, Crypto]:
        """校验后返回客户在指定链上的充值地址，客户与地址不存在时按需创建。

        /v1 商户接口与 SaaS 内部接口共用这一套校验，避免两处口径漂移：
        主网/测试网隔离、币种启用、链上可充值、归集地址已配置、客户数上限。
        """
        if not uid or not DEPOSIT_UID_PATTERN.match(uid):
            raise APIError(ErrorCode.INVALID_UID)

        try:
            chain = Chain.objects.get(code=chain_code, active=True)
        except Chain.DoesNotExist as exc:
            raise APIError(ErrorCode.INVALID_CHAIN) from exc

        # 测试项目只能在测试网建充币地址，非测试项目只能用主网，隔离两类代币防止资金/记账混淆。
        if chain.is_testnet != project.is_test:
            raise APIError(ErrorCode.INVALID_CHAIN)

        if not crypto_symbol:
            raise APIError(ErrorCode.INVALID_CRYPTO, detail="crypto is required")
        try:
            crypto = CryptoService.get_by_symbol(crypto_symbol)
        except ObjectDoesNotExist as exc:
            raise APIError(ErrorCode.INVALID_CRYPTO) from exc

        # 停用的币不允许申请充币地址，避免用户入金后因币种未启用而没有 Deposit 记录。
        if not crypto.active:
            raise APIError(ErrorCode.INVALID_CRYPTO)

        if not crypto.support_this_chain(chain=chain):
            raise APIError(
                ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT,
                detail=f"{crypto_symbol} 不支持 {chain_code} 链",
            )
        if not ChainProductCapabilityService.has_active_crypto_on_chain(
            chain=chain,
            crypto=crypto,
        ):
            raise APIError(
                ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT,
                detail=f"{crypto_symbol} 未在 {chain_code} 链启用",
            )
        if not ChainProductCapabilityService.supports_deposit_address(
            chain=chain,
            crypto=crypto,
        ):
            raise APIError(ErrorCode.INVALID_CHAIN)
        if not project.vault_address_for_chain_type(chain.type):
            raise APIError(ErrorCode.RECIPIENT_NOT_CONFIGURED)

        if not Customer.objects.filter(project=project, uid=uid).exists():
            customer_limit = get_saas_deposit_customer_limit(appid=project.appid)
            if (
                customer_limit is not None
                and Customer.objects.filter(project=project).count() >= customer_limit
            ):
                raise APIError(ErrorCode.DEPOSIT_CUSTOMER_LIMIT_REACHED)
        customer, _ = Customer.objects.get_or_create(project=project, uid=uid)

        deposit_address = ensure_deposit_address(
            chain=chain,
            customer=customer,
            crypto=crypto,
        )
        return deposit_address, chain, crypto

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
                **AmlScreeningService.webhook_risk_fields(deposit.transfer_id),
            },
        }

    @staticmethod
    def calculate_worth(transfer: Transfer) -> Decimal | None:
        """按当前价格折算转账的 USD 价值；价格不可用时返回 None。"""
        try:
            return transfer.crypto.usd_amount(transfer.amount)
        except Exception:  # noqa
            logger.exception("calculate_worth 失败", transfer_id=transfer.pk)
            return None

    @classmethod
    def refresh_worth(cls, deposit: Deposit) -> None:
        worth = cls.calculate_worth(deposit.transfer)
        if worth is None:
            # worth 保持默认值 0
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
        slot = (
            VaultSlot.objects.select_related("customer__project")
            .filter(
                chain=transfer.chain,
                address=transfer.to_address,
                usage=VaultSlotUsage.DEPOSIT,
            )
            .first()
        )
        if slot is None:
            return False

        transfer.type = TransferType.Deposit
        transfer.save(update_fields=["type"])

        # 匹配即开始 AML 查询，与区块确认并行；充值记录要到确认时才创建，
        # 这里按当前价格估算价值判断是否达到项目阈值。
        worth = cls.calculate_worth(transfer)
        if (
            slot.customer is not None
            and worth is not None
            and AmlScreeningService.should_screen(
                project=slot.customer.project, worth=worth, transfer=transfer
            )
        ):
            enqueue_screen_transfer_aml(transfer.pk)
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
        AmlScreeningService.bill_screening(deposit)
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
