from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from eth_typing import HexStr  # noqa
from eth_utils import keccak  # noqa
from web3 import Web3

from chains.models import Address
from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStatus
from common.fields import EvmAddressField
from common.models import UndeletableModel
from evm.constants import EVM_PIPELINE_DEPTH

if TYPE_CHECKING:
    from evm.intents import EvmTxIntent

logger = structlog.get_logger()


class EvmScanCursor(models.Model):
    """记录某条 EVM 链上日志扫描器的推进位置与最近错误。

    设计原则：
    - 每条 EVM 链只维护一个日志扫描游标。
    - last_scanned_block 记录 EVM 日志扫描已经推进到的最高块高。
    """

    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.CASCADE,
        related_name="evm_scan_cursors",
        verbose_name=_("链"),
    )
    last_scanned_block = models.PositiveIntegerField(_("已扫描到的区块"), default=0)
    enabled = models.BooleanField(_("启用"), default=True)
    last_error = models.TextField(_("最近错误"), blank=True, default="")
    last_error_at = models.DateTimeField(_("最近错误时间"), blank=True, null=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain",),
                name="uniq_evm_scan_cursor_chain",
            ),
        ]
        ordering = ("chain_id",)
        verbose_name = _("EVM 扫描游标")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.chain.code


class EvmTxTask(UndeletableModel):
    # base_task 是跨链统一锚点；EVM 子表继续保存 nonce/gas/data 等链特有执行参数。
    base_task = models.OneToOneField(
        "chains.TxTask",
        on_delete=models.CASCADE,
        related_name="evm_task",
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
    nonce = models.PositiveBigIntegerField(_("Nonce"))
    to = EvmAddressField(_("To"))
    value = models.DecimalField(
        _("Value"),
        max_digits=32,
        decimal_places=0,
        default=0,
    )
    data = models.TextField(_("Data"), blank=True, default="")
    gas = models.PositiveIntegerField(_("Gas"))
    gas_price = models.PositiveBigIntegerField(_("Gas Price"), blank=True, null=True)
    signed_payload = models.TextField(_("已签名链上载荷"), blank=True, default="")

    last_attempt_at = models.DateTimeField(_("上次尝试时间"), blank=True, null=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        # 统一采用具名 UniqueConstraint，便于数据库约束报错定位和后续约束扩展。
        constraints = [
            models.UniqueConstraint(
                fields=("sender", "chain", "nonce"),
                # 约束名直接采用 TxTask 语义，保持当前模型命名一致。
                name="uniq_evm_tx_task_sender_chain_nonce",
            ),
        ]
        ordering = ("created_at",)
        # EVM 主执行对象统一命名为 TxTask，避免继续把稳定任务对象写成历史别名。
        verbose_name = _("链上任务")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.base_task.tx_hash or f"{self.sender_id}:{self.nonce}"

    def broadcast(self) -> None:
        if not self._can_broadcast_queued():
            return
        if self._recover_queued_receipt_if_any():
            return
        if self._is_broadcast_order_blocked(ignore_pipeline_full=False):
            return
        self._execute_broadcast()

    def rebroadcast_submitted(self) -> None:
        if not self._can_rebroadcast_submitted():
            return
        if self._is_broadcast_order_blocked(ignore_pipeline_full=True):
            return
        self._execute_broadcast()

    def _execute_broadcast(self) -> None:
        self._record_broadcast_attempt()
        self._ensure_signed_with_latest_gas_price()

        if not self._passes_balance_preflight():
            return

        self._send_signed_payload()

    def _is_broadcast_order_blocked(self, *, ignore_pipeline_full: bool) -> bool:
        if self.has_lower_queued_nonce():
            return True
        return not ignore_pipeline_full and self.is_pipeline_full()

    def _passes_balance_preflight(self) -> bool:
        # pre-flight 第 1 步：主动阈值检查。
        # buffer_required = value + N * task.gas * signed_gas_price。
        # task.gas 是 schedule 时按具体内部合约调用确定的 gas limit。
        if self.gas_price is None:
            raise ValueError("EVM 任务尚未签名，gas_price 不可为空")
        current_native_balance = self.chain.w3.eth.get_balance(
            self.sender.address
        )  # noqa: SLF001
        signed_gas_price = int(self.gas_price)
        buffer_required = int(self.value) + 2 * self.gas * signed_gas_price
        # 余额不足时保持 QUEUED，等待运营向发起地址补充 gas。
        return current_native_balance >= buffer_required

    def _record_broadcast_attempt(self) -> None:
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["last_attempt_at"])

    def _send_signed_payload(self) -> None:
        # pre-flight 通过，真正广播。
        raw_payload = Web3.to_bytes(hexstr=HexStr(self.signed_payload))
        try:
            self.chain.w3.eth.send_raw_transaction(raw_payload)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            if self._is_nonce_too_low_error(exc):
                if self._recover_queued_receipt_if_any():
                    return
                raise
            if self._is_already_known_error(exc):
                self._mark_submitted()
                return
            raise
        self._mark_submitted()

    def known_tx_hashes(self) -> list[str]:
        """返回当前任务所有已知 tx_hash，按新版本优先查询。"""
        hashes: list[str] = []
        base_tx_hash = (
            TxTask.objects.filter(pk=self.base_task_id)
            .values_list("tx_hash", flat=True)
            .first()
        )
        if base_tx_hash:
            hashes.append(base_tx_hash)  # noqa

        for tx_hash in (
            TxHash.objects.filter(tx_task_id=self.base_task_id)
            .order_by("-version")
            .values_list("hash", flat=True)
        ):
            if tx_hash not in hashes:
                hashes.append(tx_hash)
        return hashes

    def _find_receipt_for_known_hashes(self) -> tuple[str | None, dict | None]:
        from web3.exceptions import TransactionNotFound  # noqa: PLC0415

        for tx_hash in self.known_tx_hashes():
            try:
                receipt = self.chain.w3.eth.get_transaction_receipt(
                    tx_hash
                )  # noqa: SLF001
            except TransactionNotFound:
                continue
            except AttributeError:
                return None, None
            if receipt is None:
                continue
            return tx_hash, dict(receipt)
        return None, None

    def _recover_queued_receipt_if_any(self) -> bool:
        """QUEUED 任务若已有 tx_hash，先按链上 receipt 恢复状态。

        send_raw_transaction 可能已被节点接受，但 worker 在 _mark_submitted 前
        中断。再次执行时不能盲目重发或让 nonce too low 卡住队列，应先用历史
        hash 观察链上事实，再回到统一 poller/业务管线。
        """
        base_task = TxTask.objects.only("status", "tx_hash").get(pk=self.base_task_id)
        if base_task.status != TxTaskStatus.QUEUED:
            return False

        tx_hash, receipt = self._find_receipt_for_known_hashes()
        if receipt is None or tx_hash is None:
            return False

        from evm.poller import EvmTaskPoller  # noqa: PLC0415

        status = receipt.get("status")
        if status == 1:
            self._mark_submitted()
            EvmTaskPoller.process_succeeded_receipt(
                evm_task=self,
                tx_hash=tx_hash,
                receipt=receipt,
            )
            return True
        if status == 0:
            self._mark_submitted()
            EvmTaskPoller.finalize_failed_task(evm_task=self)
            return True
        raise RuntimeError("EVM receipt status missing or invalid")

    def _can_broadcast_queued(self) -> bool:
        """普通广播入口只允许 QUEUED 任务进入真实广播副作用。"""
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        return base_task.status == TxTaskStatus.QUEUED

    def _can_rebroadcast_submitted(self) -> bool:
        """重播入口只允许 SUBMITTED 任务由 poller 补偿提交。"""
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        return base_task.status == TxTaskStatus.SUBMITTED

    @staticmethod
    def _replacement_gas_price(*, old_gas_price: int, current_gas_price: int) -> int:
        bumped = (old_gas_price * 1125 + 999) // 1000
        return max(int(current_gas_price), bumped)

    def _ensure_signed_with_latest_gas_price(self) -> None:
        """首次广播时签名并生成首个 tx_hash；重试时仅在 gas 提升时重签。"""
        current_gas_price = self.chain.w3.eth.gas_price  # noqa: SLF001
        if not self.signed_payload or self.gas_price is None:
            signed = self.sender.sign_evm_transaction(
                tx_dict=self._build_transaction_dict(gas_price=current_gas_price),
            )
            self.gas_price = current_gas_price
            self.signed_payload = signed.raw_transaction
            self.save(update_fields=["gas_price", "signed_payload"])
            self.base_task.append_tx_hash(signed.tx_hash)
            return

        if current_gas_price <= self.gas_price:
            return

        replacement_gas_price = self._replacement_gas_price(
            old_gas_price=int(self.gas_price),
            current_gas_price=int(current_gas_price),
        )
        signed = self.sender.sign_evm_transaction(
            tx_dict=self._build_transaction_dict(gas_price=replacement_gas_price),
        )
        self.gas_price = replacement_gas_price
        self.signed_payload = signed.raw_transaction
        self.save(update_fields=["gas_price", "signed_payload"])

        # 重签后 tx_hash 变化，更新父任务并追加历史记录以便链上观测匹配。
        self.base_task.append_tx_hash(signed.tx_hash)

    def _build_transaction_dict(self, *, gas_price: int) -> dict:
        return {
            "chainId": self.chain.chain_id,
            "nonce": self.nonce,
            "from": self.sender.address,
            "to": self.to,
            "value": int(self.value),
            "data": self.data if self.data else "0x",
            "gas": self.gas,
            "gasPrice": gas_price,
        }

    def _mark_submitted(self) -> None:
        # 首次成功提交到节点后，统一父任务从"待提交"进入"已提交"。
        TxTask.mark_submitted(task_id=self.base_task_id)

    @property
    def status(self) -> str:
        return self.base_task.display_status

    def has_lower_queued_nonce(self) -> bool:
        """同账户更低 nonce 尚未提交到节点（QUEUED）时阻断，保证 nonce 按顺序进入 mempool。"""
        return EvmTxTask.objects.filter(
            sender=self.sender,
            chain=self.chain,
            nonce__lt=self.nonce,
            base_task__status=TxTaskStatus.QUEUED,
        ).exists()

    def is_pipeline_full(self) -> bool:
        """同地址同链已有 >=EVM_PIPELINE_DEPTH 笔在 mempool 中等待确认时阻断。"""
        return (
            EvmTxTask.objects.filter(
                sender=self.sender,
                chain=self.chain,
                base_task__status=TxTaskStatus.SUBMITTED,
            ).count()
            >= EVM_PIPELINE_DEPTH
        )

    @staticmethod
    def _is_already_known_error(exc: Exception) -> bool:
        """判断节点返回的错误是否表示"交易已存在于 mempool 或已上链"。

        不同 EVM 客户端返回的措辞各异：
        - Geth / BSC / Bor / coreth / op-geth / Arbitrum: "already known"
        - Nethermind: "AlreadyKnown"（无空格，需单独匹配）
        - Besu: "Known transaction"
        - Parity / OpenEthereum: "Transaction with the same hash was already imported."
        - Anvil (Foundry): "transaction already imported"
        - Erigon: "existing txn with same hash"
        """
        msg = str(exc).lower()
        return (
            "already known" in msg
            or "alreadyknown" in msg
            or "known transaction" in msg
            or "already imported" in msg
            or "existing txn with same hash" in msg
        )

    @staticmethod
    def _is_nonce_too_low_error(exc: Exception) -> bool:
        """nonce too low 只表示该 nonce 已不可用，不能等同本系统交易已知。"""
        return "nonce too low" in str(exc).lower()

    @classmethod
    def schedule(cls, intent: EvmTxIntent) -> EvmTxTask:
        """按 EvmTxIntent 原子创建待执行交易任务。

        通过 Address 行锁对同一发送地址串行化，杜绝并发 nonce 冲突。
        verify_fn 必须在行锁内、nonce 分配前执行；验证失败时整个事务回滚，
        避免留下未通过业务二次校验的 TxTask 或 nonce 空洞。

        首次签名和首个 tx_hash 生成延后到 broadcast()；内部稳定身份只依赖
        (sender, chain, nonce)。
        """
        if intent.value != 0:
            raise ValueError("EVM task value must be 0")

        with db_transaction.atomic():
            Address.acquire_for_update(
                address=intent.sender,
            )

            # 在行锁内执行调用方注入的验证回调（如余额二次确认）。
            if intent.verify_fn is not None:
                intent.verify_fn()

            nonce = cls._next_nonce(intent.sender, intent.chain)
            base_task = TxTask.objects.create(
                chain=intent.chain,
                sender=intent.sender,
                tx_type=intent.tx_type,
                status=TxTaskStatus.QUEUED,
            )

            return EvmTxTask.objects.create(
                base_task=base_task,
                sender=intent.sender,
                chain=intent.chain,
                to=intent.to,
                value=intent.value,
                nonce=nonce,
                data=intent.data,
                gas=intent.gas,
            )

    @staticmethod
    def _next_nonce(address, chain) -> int:
        """为 (address, chain) 维度分配严格递增的下一个 nonce。

        调用方必须已通过 Address.acquire_for_update() 持有行锁，确保基于
        EvmTxTask 推导 nonce 与创建任务处于同一串行化区间。
        """
        latest_nonce = (
            EvmTxTask.objects.filter(sender=address, chain=chain)
            .aggregate(max_nonce=models.Max("nonce"))
            .get("max_nonce")
        )
        return 0 if latest_nonce is None else int(latest_nonce) + 1  # noqa
