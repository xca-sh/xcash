from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.resources import require_bandwidth_for_signed_transaction
from tron.resources import require_energy_for_contract_call

from chains.models import TxTask
from chains.models import TxTaskStatus
from common.fields import AddressField
from common.fields import HashField
from common.models import UndeletableModel

if TYPE_CHECKING:
    from tron.intents import TronTxIntent

logger = structlog.get_logger()


class TronWatchCursor(models.Model):
    """记录某条 Tron 链上 TRC20 扫描器的推进位置与最近错误。

    设计原则（与 EvmScanCursor 对齐）：
    - 每条 Tron 链只维护一个扫描游标；本链所有 TRC20（CryptoOnChain）
      在同一条区块进度上逐块扫描，故不再按合约地址拆分游标。
    - last_scanned_block 记录已扫描推进到的最高块高。
    """

    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.CASCADE,
        related_name="tron_watch_cursors",
        verbose_name=_("链"),
    )
    last_scanned_block = models.PositiveIntegerField(_("已扫描到的区块"), default=0)
    enabled = models.BooleanField(_("启用"), default=True)
    last_error = models.CharField(_("最近错误"), max_length=255, blank=True, default="")
    last_error_at = models.DateTimeField(_("最近错误时间"), blank=True, null=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain",),
                name="uniq_tron_watch_cursor_chain",
            ),
        ]
        ordering = ("chain_id",)
        verbose_name = _("Tron 扫描游标")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.chain.code


class TronTxTask(UndeletableModel):
    """Tron 主动链上任务。

    Tron 没有 EVM nonce；任务稳定身份由 TxTask 锚点承载，过期后重签会生成新 txID，
    历史 hash 通过 TxHash 追加，业务操作仅限 deploy/collect 这类幂等动作。
    """

    base_task = models.OneToOneField(
        "chains.TxTask",
        on_delete=models.CASCADE,
        related_name="tron_task",
        verbose_name=_("通用链上任务"),
    )
    sender = models.ForeignKey(
        "chains.Address",
        on_delete=models.PROTECT,
        verbose_name=_("发送地址"),
    )
    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.PROTECT,
        verbose_name=_("网络"),
    )
    to = AddressField(_("To"))
    function_selector = models.CharField(_("函数签名"), max_length=128)
    parameter = models.TextField(_("ABI 参数"), blank=True, default="")
    fee_limit = models.PositiveBigIntegerField(_("Fee Limit"))
    expiration = models.PositiveBigIntegerField(_("过期时间(ms)"), null=True, blank=True)
    ref_block_bytes = models.CharField(_("Ref Block Bytes"), max_length=16, blank=True, default="")
    ref_block_hash = models.CharField(_("Ref Block Hash"), max_length=32, blank=True, default="")
    signed_payload = models.JSONField(_("已签名链上载荷"), default=dict, blank=True)
    tx_id = HashField(unique=False, null=True, blank=True, verbose_name=_("当前 TxID"))
    last_attempt_at = models.DateTimeField(_("上次尝试时间"), blank=True, null=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        verbose_name = _("Tron 链上任务")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.base_task.tx_hash or f"tron-task-{self.pk or 'unsaved'}"

    @property
    def status(self) -> str:
        return self.base_task.display_status

    @property
    def can_rebroadcast(self) -> bool:
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        if base_task.status == TxTaskStatus.QUEUED:
            return True
        if base_task.status != TxTaskStatus.PENDING_CHAIN:
            return False
        return self.is_expired()

    def is_expired(self) -> bool:
        if self.expiration is None:
            return False
        return int(time.time() * 1000) >= int(self.expiration)

    def broadcast(self) -> None:
        if not self.can_rebroadcast:
            return
        self.record_broadcast_attempt()
        self.validate_fee_limit()
        client = TronHttpClient(chain=self.chain)
        resource_quote = require_energy_for_contract_call(
            client=client,
            owner_address=self.sender.address,
            contract_address=self.to,
            function_selector=self.function_selector,
            parameter=self.parameter,
        )
        unsigned = client.trigger_smart_contract(
            owner_address=self.sender.address,
            contract_address=self.to,
            function_selector=self.function_selector,
            parameter=self.parameter,
            fee_limit=self.fee_limit,
        )
        transaction = unsigned.get("transaction")
        if not isinstance(transaction, dict):
            raise TronClientError(f"invalid trigger transaction from {self.chain.code}")

        signed = self.sender.sign_tron_transaction(unsigned_transaction=transaction)
        require_bandwidth_for_signed_transaction(
            client=client,
            owner_address=self.sender.address,
            transaction=signed.raw_transaction,
            quote=resource_quote,
        )
        self.persist_signed_payload(signed_payload=signed.raw_transaction, tx_id=signed.tx_hash)

        response = client.broadcast_transaction(transaction=signed.raw_transaction)
        if response.get("result") is True:
            self.mark_pending_chain()
            return
        if self.is_duplicate_broadcast_response(response):
            self.mark_pending_chain()
            return
        message = response.get("message") or response.get("code") or response
        raise TronClientError(f"tron broadcast failed: {message}")

    def record_broadcast_attempt(self) -> None:
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["last_attempt_at"])

    def validate_fee_limit(self) -> None:
        if self.fee_limit <= 0:
            raise ValueError("Tron fee_limit must be > 0")

    def persist_signed_payload(self, *, signed_payload: dict, tx_id: str) -> None:
        raw_data = signed_payload.get("raw_data") or {}
        if not isinstance(raw_data, dict):
            raw_data = {}
        self.signed_payload = signed_payload
        self.tx_id = tx_id
        self.expiration = raw_data.get("expiration") or None
        self.ref_block_bytes = str(raw_data.get("ref_block_bytes") or "")
        self.ref_block_hash = str(raw_data.get("ref_block_hash") or "")
        self.save(
            update_fields=[
                "signed_payload",
                "tx_id",
                "expiration",
                "ref_block_bytes",
                "ref_block_hash",
            ]
        )
        self.base_task.append_tx_hash(tx_id)

    def mark_pending_chain(self) -> None:
        TxTask.objects.filter(
            pk=self.base_task_id,
            status__in=(TxTaskStatus.QUEUED, TxTaskStatus.PENDING_CHAIN),
        ).update(
            status=TxTaskStatus.PENDING_CHAIN,
            updated_at=timezone.now(),
        )

    @staticmethod
    def is_duplicate_broadcast_response(response: dict) -> bool:
        code = str(response.get("code") or "").upper()
        message = str(response.get("message") or "").upper()
        return "DUP_TRANSACTION" in code or "DUP_TRANSACTION" in message

    @classmethod
    def schedule(cls, intent: TronTxIntent) -> TronTxTask:
        if intent.verify_fn is not None:
            intent.verify_fn()
        with db_transaction.atomic():
            base_task = TxTask.objects.create(
                chain=intent.chain,
                sender=intent.sender,
                tx_type=intent.tx_type,
                status=TxTaskStatus.QUEUED,
            )
            return cls.objects.create(
                base_task=base_task,
                sender=intent.sender,
                chain=intent.chain,
                to=intent.to,
                function_selector=intent.function_selector,
                parameter=intent.parameter,
                fee_limit=intent.fee_limit,
            )
