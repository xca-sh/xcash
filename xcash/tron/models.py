from __future__ import annotations

import time
from hashlib import sha256
from typing import TYPE_CHECKING

import structlog
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.codec import TronAddressCodec
from tron.resources import require_bandwidth_for_signed_transaction
from tron.resources import require_energy_for_contract_call
from web3 import Web3

from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStatus
from common.fields import AddressField
from common.fields import HashField
from common.models import UndeletableModel

if TYPE_CHECKING:
    from tron.intents import TronTxIntent

logger = structlog.get_logger()

TRON_MAX_BROADCAST_HASHES = 5


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
    def can_broadcast_queued(self) -> bool:
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        return base_task.status == TxTaskStatus.QUEUED

    @property
    def can_rebroadcast_expired_submitted(self) -> bool:
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        return base_task.status == TxTaskStatus.SUBMITTED and self.is_expired()

    def is_expired(self) -> bool:
        if self.expiration is None:
            return False
        return int(time.time() * 1000) >= int(self.expiration)

    def broadcast(self) -> None:
        if not self.can_broadcast_queued:
            return
        self.execute_broadcast()

    def rebroadcast_expired_submitted(self) -> None:
        if not self.can_rebroadcast_expired_submitted:
            return
        if self.rebroadcast_hash_limit_reached():
            updated = TxTask.mark_finalized_failed(
                task_id=self.base_task_id,
                expected_status=TxTaskStatus.SUBMITTED,
            )
            if updated:
                logger.warning(
                    "Tron 任务重签次数达到上限，已标记失败",
                    tron_task_id=self.pk,
                    tx_task_id=self.base_task_id,
                    chain=self.chain.code,
                    sender=self.sender.address,
                    tx_hash_count=TRON_MAX_BROADCAST_HASHES,
                )
            return
        self.execute_broadcast()

    def rebroadcast_hash_limit_reached(self) -> bool:
        return (
            TxHash.objects.filter(tx_task_id=self.base_task_id).count()
            >= TRON_MAX_BROADCAST_HASHES
        )

    def execute_broadcast(self) -> None:
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
        expected_tx_id = self.validate_unsigned_transaction(transaction)

        signed = self.sender.sign_tron_transaction(unsigned_transaction=transaction)
        if str(signed.tx_hash).lower() != expected_tx_id:
            raise TronClientError("tron signed tx hash mismatch unsigned txID")
        require_bandwidth_for_signed_transaction(
            client=client,
            owner_address=self.sender.address,
            transaction=signed.raw_transaction,
            quote=resource_quote,
        )
        self.persist_signed_payload(signed_payload=signed.raw_transaction, tx_id=signed.tx_hash)

        response = client.broadcast_transaction(transaction=signed.raw_transaction)
        if response.get("result") is True:
            self.mark_submitted()
            return
        if self.is_duplicate_broadcast_response(response):
            self.mark_submitted()
            return
        message = response.get("message") or response.get("code") or response
        raise TronClientError(f"tron broadcast failed: {message}")

    def record_broadcast_attempt(self) -> None:
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["last_attempt_at"])

    def validate_fee_limit(self) -> None:
        if self.fee_limit <= 0:
            raise ValueError("Tron fee_limit must be > 0")

    def validate_unsigned_transaction(self, transaction: dict) -> str:
        raw_data_hex = (
            str(transaction.get("raw_data_hex") or "")
            .removeprefix("0x")
            .removeprefix("0X")
        )
        if not raw_data_hex:
            raise TronClientError("tron unsigned transaction raw_data_hex missing")
        try:
            expected_tx_id = sha256(bytes.fromhex(raw_data_hex)).hexdigest()
        except ValueError as exc:
            raise TronClientError("tron unsigned transaction raw_data_hex invalid") from exc

        tx_id = str(transaction.get("txID") or "").lower()
        if not tx_id or tx_id != expected_tx_id:
            raise TronClientError("tron unsigned transaction txID mismatch raw_data")

        raw_data = transaction.get("raw_data")
        if not isinstance(raw_data, dict):
            raise TronClientError("tron unsigned transaction missing raw_data")
        try:
            fee_limit = int(raw_data.get("fee_limit") or 0)
        except (TypeError, ValueError) as exc:
            raise TronClientError("tron unsigned transaction fee_limit invalid") from exc
        if fee_limit != int(self.fee_limit):
            raise TronClientError("tron unsigned transaction fee_limit mismatch")

        contracts = raw_data.get("contract") or []
        if not isinstance(contracts, list) or len(contracts) != 1:
            raise TronClientError("tron unsigned transaction contract count mismatch")
        contract = contracts[0]
        if not isinstance(contract, dict) or contract.get("type") != "TriggerSmartContract":
            raise TronClientError("tron unsigned transaction type mismatch")

        parameter = contract.get("parameter") or {}
        value = parameter.get("value") if isinstance(parameter, dict) else None
        if not isinstance(value, dict):
            raise TronClientError("tron unsigned transaction parameter mismatch")

        expected_owner = TronAddressCodec.base58_to_hex41(self.sender.address).lower()
        expected_contract = TronAddressCodec.base58_to_hex41(self.to).lower()
        if str(value.get("owner_address") or "").lower() != expected_owner:
            raise TronClientError("tron unsigned transaction owner mismatch")
        if str(value.get("contract_address") or "").lower() != expected_contract:
            raise TronClientError("tron unsigned transaction contract mismatch")

        selector = Web3.keccak(text=self.function_selector)[:4].hex()
        expected_data = f"{selector}{self.parameter}".lower()
        actual_data = (
            str(value.get("data") or "").removeprefix("0x").removeprefix("0X").lower()
        )
        if actual_data != expected_data:
            raise TronClientError("tron unsigned transaction call data mismatch")

        return expected_tx_id

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
        self.base_task.append_tx_hash(tx_id, expires_at_ms=self.expiration)

    def mark_submitted(self) -> None:
        TxTask.mark_submitted(
            task_id=self.base_task_id,
            allow_resubmitted=True,
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
