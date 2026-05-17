from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from web3 import Web3

import evm.intents
from chains.models import AddressChainState
from chains.models import BroadcastTask
from chains.models import BroadcastTaskFailureReason
from chains.models import BroadcastTaskResult
from chains.models import BroadcastTaskStage
from chains.models import ChainType
from chains.models import TxHash
from chains.signer import get_signer_backend
from common.fields import EvmAddressField
from common.models import UndeletableModel
from evm.choices import TxKind
from evm.constants import EVM_PIPELINE_DEPTH
from evm.intents import assert_action_type_implemented
from evm.intents import get_preflight_buffer_multiplier

if TYPE_CHECKING:
    from evm.intents import EvmTxIntent

class EvmScanCursorType(models.TextChoices):
    """定义 EVM 自扫描器的游标类型。"""

    NATIVE_DIRECT = "native_direct", _("原生币直转")
    ERC20_TRANSFER = "erc20_transfer", _("ERC20 转账")


class EvmScanCursor(models.Model):
    """记录某条 EVM 链上某类扫描器的推进位置与最近错误。

    设计原则：
    - 游标按"链 + 扫描器类型"维度维护，不按 token 维度膨胀。
    - last_scanned_block 记录主扫描面已经推进到的最高块高。
    - last_safe_block 记录当前安全块高，便于后台观察追平程度。
    """

    chain = models.ForeignKey(
        "chains.Chain",
        on_delete=models.CASCADE,
        related_name="evm_scan_cursors",
        verbose_name=_("链"),
    )
    scanner_type = models.CharField(
        _("扫描器类型"),
        max_length=32,
        choices=EvmScanCursorType,
    )
    last_scanned_block = models.PositiveIntegerField(_("已扫描到的区块"), default=0)
    last_safe_block = models.PositiveIntegerField(_("安全区块"), default=0)
    enabled = models.BooleanField(_("启用"), default=True)
    last_error = models.TextField(_("最近错误"), blank=True, default="")
    last_error_at = models.DateTimeField(_("最近错误时间"), blank=True, null=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "scanner_type"),
                name="uniq_evm_scan_cursor_chain_scanner_type",
            ),
        ]
        ordering = ("chain_id", "scanner_type")
        verbose_name = _("EVM 扫描游标")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.chain.code}:{self.scanner_type}"


class EvmBroadcastTask(UndeletableModel):
    # base_task 是跨链统一锚点；EVM 子表继续保存 nonce/gas/data 等链特有执行参数。
    base_task = models.OneToOneField(
        "chains.BroadcastTask",
        on_delete=models.CASCADE,
        related_name="evm_task",
        verbose_name=_("通用链上任务"),
        blank=True,
        null=True,
    )
    address = models.ForeignKey(
        "chains.Address",
        on_delete=models.PROTECT,
        verbose_name=_("地址"),
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
    tx_kind = models.CharField(
        _("交易形态"),
        max_length=32,
        choices=TxKind.choices,
    )
    gas_price = models.PositiveBigIntegerField(_("Gas Price"), blank=True, null=True)
    signed_payload = models.TextField(_("已签名链上载荷"), blank=True, default="")

    last_attempt_at = models.DateTimeField(_("上次尝试时间"), blank=True, null=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        # 统一采用具名 UniqueConstraint，便于数据库约束报错定位和后续约束扩展。
        constraints = [
            models.UniqueConstraint(
                fields=("address", "chain", "nonce"),
                # 约束名直接采用 BroadcastTask 语义，保持当前模型命名一致。
                name="uniq_evm_broadcast_task_address_chain_nonce",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    tx_kind__in=[
                        TxKind.NATIVE_TRANSFER,
                        TxKind.CONTRACT_CALL,
                    ]
                ),
                name="ck_evm_broadcast_task_tx_kind_valid",
            ),
        ]
        ordering = ("created_at",)
        # EVM 主执行对象统一命名为 BroadcastTask，避免继续把稳定任务对象写成历史别名。
        verbose_name = _("链上任务")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return (
            self.base_task.tx_hash or f"{self.address_id}:{self.nonce}"
            if self.base_task_id
            else f"{self.address_id}:{self.nonce}"
        )

    @property
    def transaction_dict(self) -> dict:
        if self.gas_price is None:
            raise ValueError("EVM 任务尚未签名，gas_price 不可为空")
        return {
            "chainId": self.chain.chain_id,
            "nonce": self.nonce,
            "from": self.address.address,
            "to": self.to,
            "value": int(self.value),
            # 交易字典要稳定适配 signer 请求载荷和 web3 原始交易格式，空 data 统一使用 0x。
            "data": self.data if self.data else "0x",
            "gas": self.gas,
            "gasPrice": self.gas_price,
        }

    def broadcast(self, *, allow_pending_chain_rebroadcast: bool = False) -> None:
        if not self._can_broadcast_for_current_stage(
            allow_pending_chain_rebroadcast=allow_pending_chain_rebroadcast
        ):
            return
        if self._recover_queued_receipt_if_any():
            return
        if self._is_broadcast_order_blocked(
            allow_pending_chain_rebroadcast=allow_pending_chain_rebroadcast
        ):
            return

        self._ensure_signed_with_latest_gas_price()

        if not self._passes_balance_preflight():
            return
        if not self._passes_execution_preflight():
            return

        self._record_broadcast_attempt()
        self._send_signed_payload()

    def _is_broadcast_order_blocked(
        self, *, allow_pending_chain_rebroadcast: bool
    ) -> bool:
        if self.has_lower_queued_nonce():
            return True
        return not allow_pending_chain_rebroadcast and self.is_pipeline_full()

    def _passes_balance_preflight(self) -> bool:
        # pre-flight 第 1 步：主动阈值检查。
        # buffer_required = value + N * task.gas * current_gas_price。
        # N 由 tx_kind 派发表控制；task.gas 是 schedule 时按具体交易形态
        # 已经确定的 gas limit，避免原生转账和合约调用都套用 ERC-20 上限。
        current_native_balance = self.chain.w3.eth.get_balance(self.address.address)  # noqa: SLF001
        current_gas_price = self.chain.w3.eth.gas_price  # noqa: SLF001
        multiplier = get_preflight_buffer_multiplier(TxKind(self.tx_kind))
        expected_collection_gas_cost = (
            current_gas_price * self.chain.erc20_transfer_gas
        )
        buffer_required = int(self.value) + multiplier * self.gas * current_gas_price
        if current_native_balance < buffer_required:
            # 仅归集场景补 gas；Withdrawal 的 address 是 Vault 本身，补 gas 无意义，
            # 保持 QUEUED 静默返回，等运营向 Vault 注资即可。
            if self._is_eligible_for_gas_recharge():
                self._request_gas_recharge(
                    expected_collection_gas_cost=expected_collection_gas_cost
                )
            # 不更新 last_attempt_at，避免 reconcile/dispatch 误判为活跃任务。
            return False

        return True

    def _passes_execution_preflight(self) -> bool:
        # pre-flight 第 2 步：estimate_gas 兜底 revert。
        # 主动阈值已覆盖余额不足；这里只防合约 revert / token 余额不足一类
        # 注定失败的交易被反复 send_raw_transaction。通用 RPC 错误原样上抛给 Celery。
        #
        # 注意：estimate_gas 不传 nonce。nonce 顺序由 has_lower_queued_nonce +
        # pipeline_full 保证，与"交易语义能否执行"无关；一旦绑定 nonce，EVM 节点
        # 在同地址前序 tx 未 confirm 时会对本 tx 直接返回 "Nonce too high"
        # （geth/anvil 的 -32003），把所有同地址后续任务打成假失败。
        preflight_tx = self._build_transaction_dict(gas_price=self.gas_price)
        preflight_tx.pop("nonce", None)
        try:
            self.chain.w3.eth.estimate_gas(preflight_tx)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            if self._is_execution_reverted_error(exc):
                # estimate_gas 回退发生在交易广播前，此 nonce 尚未被链上消费。
                # 直接终局会制造 nonce 空洞并放行后续更高 nonce；保持 QUEUED，
                # 仅记录尝试时间让调度节流，等待后续重试、补救或人工处理。
                self._record_broadcast_attempt()
                return False
            raise

        return True

    def _record_broadcast_attempt(self) -> None:
        self.last_attempt_at = timezone.now()
        self.save(update_fields=["last_attempt_at"])

    def _send_signed_payload(self) -> None:
        # pre-flight 通过，真正广播。
        raw_payload = Web3.to_bytes(hexstr=self.signed_payload)
        try:
            self.chain.w3.eth.send_raw_transaction(raw_payload)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            if self._is_nonce_too_low_error(exc):
                if self._recover_queued_receipt_if_any():
                    return
                raise
            if self._is_already_known_error(exc):
                self._mark_pending_chain()
                return
            raise
        self._mark_pending_chain()

    def _known_tx_hashes(self) -> list[str]:
        """返回当前任务所有已知 tx_hash，按新版本优先查询。"""
        if not self.base_task_id:
            return []

        hashes: list[str] = []
        base_tx_hash = (
            BroadcastTask.objects.filter(pk=self.base_task_id)
            .values_list("tx_hash", flat=True)
            .first()
        )
        if base_tx_hash:
            hashes.append(base_tx_hash)

        for tx_hash in (
            TxHash.objects.filter(broadcast_task_id=self.base_task_id)
            .order_by("-version")
            .values_list("hash", flat=True)
        ):
            if tx_hash not in hashes:
                hashes.append(tx_hash)
        return hashes

    def _find_receipt_for_known_hashes(self) -> tuple[str | None, dict | None]:
        from web3.exceptions import TransactionNotFound  # noqa: PLC0415

        for tx_hash in self._known_tx_hashes():
            try:
                receipt = self.chain.w3.eth.get_transaction_receipt(tx_hash)  # noqa: SLF001
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

        send_raw_transaction 可能已被节点接受，但 worker 在 _mark_pending_chain 前
        中断。再次执行时不能盲目重发或让 nonce too low 卡住队列，应先用历史
        hash 观察链上事实，再回到统一 coordinator/业务管线。
        """
        if not self.base_task_id:
            return False

        base_task = BroadcastTask.objects.only("stage", "result", "tx_hash").get(
            pk=self.base_task_id
        )
        if (
            base_task.stage != BroadcastTaskStage.QUEUED
            or base_task.result != BroadcastTaskResult.UNKNOWN
        ):
            return False
        if not self._known_tx_hashes():
            return False

        tx_hash, receipt = self._find_receipt_for_known_hashes()
        if receipt is None or tx_hash is None:
            return False

        from evm.coordinator import InternalEvmTaskCoordinator  # noqa: PLC0415

        status = receipt.get("status")
        if status == 1:
            self._mark_pending_chain()
            InternalEvmTaskCoordinator._observe_confirmed_transaction(
                evm_task=self,
                tx_hash=tx_hash,
                receipt=receipt,
            )
            return True
        if status == 0:
            self._mark_pending_chain()
            InternalEvmTaskCoordinator._finalize_failed_task(evm_task=self)
            return True
        raise RuntimeError("EVM receipt status missing or invalid")

    @staticmethod
    def _is_execution_reverted_error(exc: Exception) -> bool:
        """识别链上模拟时合约回退 / 交易本身注定失败的错误。"""
        from web3.exceptions import ContractLogicError  # noqa: PLC0415

        if isinstance(exc, ContractLogicError):
            return True
        msg = str(exc).lower()
        return "execution reverted" in msg

    def _can_broadcast_for_current_stage(
        self, *, allow_pending_chain_rebroadcast: bool
    ) -> bool:
        """校验当前父任务阶段是否允许进入真实广播副作用。"""
        if not self.base_task_id:
            return True

        base_task = BroadcastTask.objects.only("stage", "result").get(
            pk=self.base_task_id
        )
        if base_task.result != BroadcastTaskResult.UNKNOWN:
            return False
        if base_task.stage == BroadcastTaskStage.PENDING_CHAIN:
            return allow_pending_chain_rebroadcast
        return base_task.stage == BroadcastTaskStage.QUEUED

    def _is_eligible_for_gas_recharge(self) -> bool:
        """判断当前任务是否适用"向其 address 补 gas"。

        条件：
        - base_task.action_type 在 EVM gas 补给派发表中允许补给
        - self.address 已登记为有效 DepositAddress（排除其它用途的地址）

        Withdrawal 任务的 address 本身即 Vault，补 gas 会形成 vault→vault 死循环，
        故排除；无 base_task 或非归集类型直接返回 False。
        """
        if not self.base_task_id:
            return False
        if not evm.intents.is_gas_rechargeable(self.base_task.action_type):
            return False

        from deposits.models import DepositAddress  # noqa: PLC0415

        return DepositAddress.objects.filter(address=self.address).exists()

    def _request_gas_recharge(self, *, expected_collection_gas_cost: int) -> None:
        """pre-flight 阈值不足时，委托 GasRechargeService 幂等补 gas。

        调用此方法前须已通过 _is_eligible_for_gas_recharge 校验，确保
        self.address 存在对应 DepositAddress。内部再 select_related 拉齐
        Vault 派生所需字段；若 DB 关系异常（极端情况）记录日志静默跳过，
        让上层保持 QUEUED 等下一轮再试，而不是把 pre-flight 打成硬错误。
        """
        from deposits.models import DepositAddress  # noqa: PLC0415
        from deposits.service import GasRechargeService  # noqa: PLC0415

        try:
            deposit_address = DepositAddress.objects.select_related(
                "customer__project__wallet", "address",
            ).get(address=self.address)
        except DepositAddress.DoesNotExist:
            return

        GasRechargeService.request_recharge(
            deposit_address=deposit_address,
            chain=self.chain,
            expected_collection_gas_cost=expected_collection_gas_cost,
        )

    def _ensure_signed_with_latest_gas_price(self) -> None:
        """首次广播时签名并生成首个 tx_hash；重试时仅在 gas 提升时重签。"""
        current_gas_price = self.chain.w3.eth.gas_price  # noqa: SLF001
        if not self.signed_payload or self.gas_price is None:
            signed = get_signer_backend().sign_evm_transaction(
                address=self.address,
                chain=self.chain,
                tx_dict=self._build_transaction_dict(gas_price=current_gas_price),
            )
            self.gas_price = current_gas_price
            self.signed_payload = signed.raw_transaction
            self.save(update_fields=["gas_price", "signed_payload"])
            if self.base_task_id:
                self.base_task.append_tx_hash(signed.tx_hash)
            return

        if current_gas_price <= self.gas_price:
            return

        signed = get_signer_backend().sign_evm_transaction(
            address=self.address,
            chain=self.chain,
            tx_dict=self._build_transaction_dict(gas_price=current_gas_price),
        )
        self.gas_price = current_gas_price
        self.signed_payload = signed.raw_transaction
        self.save(update_fields=["gas_price", "signed_payload"])

        # 重签后 tx_hash 变化，更新父任务并追加历史记录以便链上观测匹配。
        if self.base_task_id:
            self.base_task.append_tx_hash(signed.tx_hash)

    def _build_transaction_dict(self, *, gas_price: int) -> dict:
        return {
            "chainId": self.chain.chain_id,
            "nonce": self.nonce,
            "from": self.address.address,
            "to": self.to,
            "value": int(self.value),
            "data": self.data if self.data else "0x",
            "gas": self.gas,
            "gasPrice": gas_price,
        }

    def _mark_pending_chain(self) -> None:
        if self.base_task_id:
            # 首次成功提交到节点后，统一父任务从"待广播"进入"待上链"。
            BroadcastTask.objects.filter(
                pk=self.base_task_id,
                stage=BroadcastTaskStage.QUEUED,
                result=BroadcastTaskResult.UNKNOWN,
            ).update(
                stage=BroadcastTaskStage.PENDING_CHAIN,
                updated_at=timezone.now(),
            )

    @property
    def status(self) -> str:
        if self.base_task_id:
            return self.base_task.display_status
        return "待执行"

    def has_lower_queued_nonce(self) -> bool:
        """同账户更低 nonce 尚未提交到节点（QUEUED）时阻断，保证 nonce 按顺序进入 mempool。"""
        if not self.address_id or not self.chain_id:
            return False
        return EvmBroadcastTask.objects.filter(
            address=self.address,
            chain=self.chain,
            nonce__lt=self.nonce,
            base_task__stage=BroadcastTaskStage.QUEUED,
            base_task__result=BroadcastTaskResult.UNKNOWN,
        ).exists()

    def is_pipeline_full(self) -> bool:
        """同地址同链已有 >=EVM_PIPELINE_DEPTH 笔在 mempool 中等待确认时阻断。"""
        if not self.address_id or not self.chain_id:
            return False
        return (
            EvmBroadcastTask.objects.filter(
                address=self.address,
                chain=self.chain,
                base_task__stage=BroadcastTaskStage.PENDING_CHAIN,
                base_task__result=BroadcastTaskResult.UNKNOWN,
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
    def schedule(cls, intent: EvmTxIntent) -> EvmBroadcastTask:
        """按 EvmTxIntent 原子创建待执行广播任务。

        通过 AddressChainState 行锁对 (address, chain) 串行化，杜绝并发 nonce
        冲突。verify_fn 必须在行锁内、nonce 分配前执行；验证失败时整个事务
        回滚，避免留下未通过业务二次校验的 BroadcastTask 或 nonce 空洞。

        首次签名和首个 tx_hash 生成延后到 broadcast()；内部稳定身份只依赖
        (address, chain, nonce)。
        """
        assert_action_type_implemented(intent.action_type)

        with db_transaction.atomic():
            state = AddressChainState.acquire_for_update(
                address=intent.address,
                chain=intent.chain,
            )

            # 在行锁内执行调用方注入的验证回调（如余额二次确认）。
            if intent.verify_fn is not None:
                intent.verify_fn()

            nonce = cls._next_nonce(intent.address, intent.chain, state=state)
            base_task = BroadcastTask.objects.create(
                chain=intent.chain,
                address=intent.address,
                action_type=intent.action_type,
                stage=BroadcastTaskStage.QUEUED,
                result=BroadcastTaskResult.UNKNOWN,
            )

            broadcast_task = EvmBroadcastTask.objects.create(
                base_task=base_task,
                address=intent.address,
                chain=intent.chain,
                to=intent.to,
                value=intent.value,
                nonce=nonce,
                data=intent.data,
                gas=intent.gas,
                tx_kind=intent.tx_kind,
            )
            state.next_nonce = nonce + 1
            state.save()
            return broadcast_task

    @staticmethod
    def _next_nonce(address, chain, *, state: AddressChainState) -> int:
        """为 (address, chain) 维度分配严格递增的下一个 nonce。

        调用方必须已通过 AddressChainState.acquire_for_update() 持有行锁，
        并将锁定的 state 实例传入，确保 nonce 分配在串行化保护下进行。
        """
        latest_nonce = (
            EvmBroadcastTask.objects.filter(address=address, chain=chain)
            .aggregate(max_nonce=models.Max("nonce"))
            .get("max_nonce")
        )
        derived_next_nonce = 0 if latest_nonce is None else int(latest_nonce) + 1
        if state.next_nonce is None:
            next_nonce = derived_next_nonce
        else:
            next_nonce = max(int(state.next_nonce), derived_next_nonce)
        if state.next_nonce != next_nonce:
            state.next_nonce = next_nonce
            state.save()
        return next_nonce


class X402FacilitationStatus(models.TextChoices):
    CREATED = "created", _("已创建")
    BROADCASTED = "broadcasted", _("已广播")
    CONFIRMED = "confirmed", _("已确认")
    FAILED = "failed", _("已失败")
    DROPPED = "dropped", _("已回退")


class X402Facilitation(UndeletableModel):
    """x402 EIP-3009 代付的最小业务实体。"""

    broadcast_task = models.OneToOneField(
        "chains.BroadcastTask",
        on_delete=models.PROTECT,
        related_name="x402_facilitation",
        blank=True,
        null=True,
        verbose_name=_("广播任务"),
    )
    chain = models.ForeignKey("chains.Chain", on_delete=models.PROTECT)
    crypto = models.ForeignKey("currencies.Crypto", on_delete=models.PROTECT)
    facilitator_address = models.ForeignKey(
        "chains.Address",
        on_delete=models.PROTECT,
        verbose_name=_("代付地址"),
    )
    authorization_from_address = EvmAddressField(_("授权方"))
    authorization_to_address = EvmAddressField(_("收款方"))
    authorization_value_raw = models.DecimalField(
        _("授权额度（最小单位）"),
        max_digits=32,
        decimal_places=0,
    )
    valid_after = models.PositiveBigIntegerField(_("生效起始时间"))
    valid_before = models.PositiveBigIntegerField(_("生效结束时间"))
    authorization_nonce = models.BinaryField(_("授权 nonce"), max_length=32)
    authorization_v = models.PositiveSmallIntegerField(_("v"))
    authorization_r = models.BinaryField(_("r"), max_length=32)
    authorization_s = models.BinaryField(_("s"), max_length=32)
    transfer = models.OneToOneField(
        "chains.OnchainTransfer",
        on_delete=models.PROTECT,
        related_name="x402_facilitation",
        blank=True,
        null=True,
    )
    status = models.CharField(
        _("状态"),
        choices=X402FacilitationStatus,
        max_length=16,
        default=X402FacilitationStatus.CREATED,
    )
    failure_reason = models.CharField(
        _("失败原因"),
        blank=True,
        default="",
        max_length=64,
        choices=BroadcastTaskFailureReason,
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("x402 代付")
        verbose_name_plural = verbose_name

    def clean(self):
        super().clean()
        if self.facilitator_address.chain_type != ChainType.EVM:
            raise ValidationError({"facilitator_address": _("必须是系统 EVM 地址")})
        if (
            self.broadcast_task_id
            and self.broadcast_task.address_id != self.facilitator_address_id
        ):
            raise ValidationError(
                {"facilitator_address": _("必须等于 BroadcastTask.address")}
            )


class ContractDeployCollectionStatus(models.TextChoices):
    CREATED = "created", _("已创建")
    BROADCASTED = "broadcasted", _("已广播")
    CONFIRMED = "confirmed", _("已确认")
    FAILED = "failed", _("已失败")
    DROPPED = "dropped", _("已回退")


class ContractDeployCollection(UndeletableModel):
    """CREATE2 部署收款合约并归集 ERC20 的最小业务实体。"""

    broadcast_task = models.OneToOneField(
        "chains.BroadcastTask",
        on_delete=models.PROTECT,
        related_name="contract_deploy_collection",
        blank=True,
        null=True,
    )
    chain = models.ForeignKey("chains.Chain", on_delete=models.PROTECT)
    crypto = models.ForeignKey("currencies.Crypto", on_delete=models.PROTECT)
    deployer_address = models.ForeignKey("chains.Address", on_delete=models.PROTECT)
    factory_address = EvmAddressField()
    collector_address = EvmAddressField()
    vault_address = EvmAddressField()
    salt = models.BinaryField(max_length=32)
    collector_init_code_hash = models.BinaryField(max_length=32)
    expected_collect_value_raw = models.DecimalField(max_digits=32, decimal_places=0)
    transfer = models.OneToOneField(
        "chains.OnchainTransfer",
        on_delete=models.PROTECT,
        related_name="contract_deploy_collection",
        blank=True,
        null=True,
    )
    status = models.CharField(
        choices=ContractDeployCollectionStatus,
        max_length=16,
        default=ContractDeployCollectionStatus.CREATED,
    )
    failure_reason = models.CharField(
        blank=True,
        default="",
        max_length=64,
        choices=BroadcastTaskFailureReason,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        super().clean()
        if self.deployer_address.chain_type != ChainType.EVM:
            raise ValidationError({"deployer_address": _("必须是系统 EVM 地址")})
        if (
            self.broadcast_task_id
            and self.broadcast_task.address_id != self.deployer_address_id
        ):
            raise ValidationError(
                {"deployer_address": _("必须等于 BroadcastTask.address")}
            )
