from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import IntegrityError
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from eth_typing import HexStr  # noqa
from eth_utils import keccak  # noqa
from web3 import Web3

from chains.models import TERMINAL_TX_TASK_STATUSES
from chains.models import AddressChainState
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.types import AddressStr
from common.fields import AddressField
from common.fields import EvmAddressField
from common.models import UndeletableModel
from core.models import SystemWallet
from core.runtime_settings import get_vault_slot_collect_delay
from evm.adapter import EvmAdapter
from evm.constants import EVM_PIPELINE_DEPTH
from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.contracts_codec import predict_xcash_vault_slot_address
from evm.intents import build_vault_slot_collect_intent
from evm.intents import build_vault_slot_deploy_intent
from projects.models import Customer

if TYPE_CHECKING:
    from evm.intents import EvmTxIntent


class VaultSlotUsage(models.TextChoices):
    DEPOSIT = "deposit", _("用户充币")
    INVOICE = "invoice", _("账单收款")


class VaultSlot(models.Model):
    """项目在指定 EVM 链上的 XcashVaultSlot。"""

    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    usage = models.CharField(
        _("用途"),
        choices=VaultSlotUsage,
        max_length=16,
        db_index=True,
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("客户"),
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        verbose_name=_("项目"),
    )
    invoice_index = models.PositiveIntegerField(
        _("账单槽位序号"),
        null=True,
        blank=True,
    )
    address = AddressField(_("收款地址"))
    salt = models.BinaryField(_("CREATE2 Salt"), max_length=32)
    deploy_tx_task = models.OneToOneField(
        "evm.EvmTxTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployed_vault_slot",
        verbose_name=_("部署交易任务"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("customer", "chain"),
                name="uniq_evm_vault_slot_customer_chain",
            ),
            models.UniqueConstraint(
                fields=("project", "usage", "chain", "invoice_index"),
                name="uniq_evm_vault_slot_project_usage_chain_invoice_index",
            ),
            models.UniqueConstraint(
                fields=("chain", "address"),
                name="uniq_evm_vault_slot_chain_address",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        usage=VaultSlotUsage.DEPOSIT,
                        customer__isnull=False,
                        invoice_index__isnull=True,
                    )
                    | models.Q(
                        usage=VaultSlotUsage.INVOICE,
                        customer__isnull=True,
                        invoice_index__isnull=False,
                    )
                ),
                name="ck_evm_vault_slot_usage_customer",
            ),
        ]
        verbose_name = _("收款地址")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.address

    def save(self, *args, **kwargs):
        if self.usage == VaultSlotUsage.DEPOSIT:
            if self.customer_id is None:
                raise ValueError("VaultSlot customer is required for deposit usage")
            if self.project_id is None:
                self.project_id = self.customer.project_id
            if self.invoice_index is not None:
                raise ValueError(
                    "VaultSlot invoice_index must be empty for deposit usage"
                )
        elif self.usage == VaultSlotUsage.INVOICE:
            if self.customer_id is not None:
                raise ValueError("VaultSlot customer must be empty for invoice usage")
            if self.invoice_index is None:
                raise ValueError(
                    "VaultSlot invoice_index is required for invoice usage"
                )
        return super().save(*args, **kwargs)

    @property
    def is_deployed(self) -> bool:
        if self.deploy_tx_task_id is None:
            return False
        return self.deploy_tx_task.base_task.status == TxTaskStatus.CONFIRMED

    @staticmethod
    def _is_deployed_on_chain(*, chain: Chain, address: AddressStr) -> bool:
        return EvmAdapter.is_contract(chain, address)

    @staticmethod
    def build_salt(
        *,
        usage: VaultSlotUsage,
        customer: Customer | None = None,
        project_id: int | None = None,
        invoice_index: int | None = None,
    ) -> bytes:
        if usage == VaultSlotUsage.DEPOSIT:
            if customer is None:
                raise ValueError("customer is required for deposit salt")
            # 不掺 chain.code：configure deterministic deployer 后所有 EVM 链的
            # factory / template / vault 地址都一致，再用同一 salt 即可让客户在所有 EVM 链
            # 拿到同一个 VaultSlot 地址。
            return keccak(
                b"xcash:vault-slot:deposit:"
                + str(customer.project_id).encode()
                + b":"
                + customer.uid.encode()
            )

        if usage == VaultSlotUsage.INVOICE:
            if project_id is None or invoice_index is None:
                raise ValueError(
                    "project_id and invoice_index are required for invoice salt"
                )
            return keccak(
                b"xcash:vault-slot:invoice:"
                + str(project_id).encode()
                + b":"
                + str(invoice_index).encode()
            )

        raise ValueError(f"unsupported VaultSlot usage: {usage}")

    @staticmethod
    def ensure_deposit_address(chain: Chain, customer: Customer) -> AddressStr:
        if chain.type != ChainType.EVM:
            raise ValueError("VaultSlot 仅支持 EVM 链")

        project = customer.project
        existing = VaultSlot.objects.filter(
            chain=chain,
            project=project,
            usage=VaultSlotUsage.DEPOSIT,
            customer=customer,
        ).first()
        if existing is not None:
            db_transaction.on_commit(
                lambda slot_pk=existing.pk: VaultSlot.schedule_deploy(slot_pk)
            )
            return existing.address

        vault_address = project.vault
        if not vault_address:
            raise RuntimeError(
                f"Project {customer.project_id} VaultSlot Vault 地址未配置"
            )
        salt = VaultSlot.build_salt(
            usage=VaultSlotUsage.DEPOSIT,
            customer=customer,
        )
        slot_address = predict_xcash_vault_slot_address(
            vault=vault_address,
            salt=salt,
        )
        try:
            slot, created = VaultSlot.objects.get_or_create(
                chain=chain,
                project=project,
                usage=VaultSlotUsage.DEPOSIT,
                customer=customer,
                defaults={
                    "address": slot_address,
                    "salt": salt,
                },
            )
        except IntegrityError as exc:
            try:
                slot = VaultSlot.objects.get(
                    chain=chain,
                    project=project,
                    usage=VaultSlotUsage.DEPOSIT,
                    customer=customer,
                )
            except VaultSlot.DoesNotExist as not_exist_exc:
                raise exc from not_exist_exc
        else:
            if created:
                db_transaction.on_commit(
                    lambda slot_pk=slot.pk: VaultSlot.schedule_deploy(slot_pk)
                )
        return slot.address

    @staticmethod
    def ensure_invoice_address(
        *,
        project,
        chain: Chain,
        invoice_index: int,
    ) -> AddressStr:
        if chain.type != ChainType.EVM:
            raise ValueError("VaultSlot 仅支持 EVM 链")

        existing = VaultSlot.objects.filter(
            chain=chain,
            project=project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=invoice_index,
        ).first()
        if existing is not None:
            db_transaction.on_commit(
                lambda slot_pk=existing.pk: VaultSlot.schedule_deploy(slot_pk)
            )
            return existing.address

        vault_address = project.vault
        if not vault_address:
            raise RuntimeError(f"Project {project.pk} VaultSlot Vault 地址未配置")
        salt = VaultSlot.build_salt(
            usage=VaultSlotUsage.INVOICE,
            project_id=project.pk,
            invoice_index=invoice_index,
        )
        slot_address = predict_xcash_vault_slot_address(
            vault=vault_address,
            salt=salt,
        )
        try:
            slot, created = VaultSlot.objects.get_or_create(
                chain=chain,
                project=project,
                usage=VaultSlotUsage.INVOICE,
                invoice_index=invoice_index,
                defaults={
                    "address": slot_address,
                    "salt": salt,
                },
            )
        except IntegrityError as exc:
            try:
                slot = VaultSlot.objects.get(
                    chain=chain,
                    project=project,
                    usage=VaultSlotUsage.INVOICE,
                    invoice_index=invoice_index,
                )
            except VaultSlot.DoesNotExist as not_exist_exc:
                raise exc from not_exist_exc
        else:
            if created:
                db_transaction.on_commit(
                    lambda slot_pk=slot.pk: VaultSlot.schedule_deploy(slot_pk)
                )
        return slot.address

    @staticmethod
    def schedule_deploy(slot_pk: int) -> EvmTxTask | None:
        with db_transaction.atomic():
            slot = (
                VaultSlot.objects.select_for_update(of=("self",))
                .select_related(
                    "chain",
                    "project",
                )
                .get(pk=slot_pk)
            )
            # 并发 waiters 可能在首个事务更新 deploy_tx_task 前就已经发起
            # SELECT ... FOR UPDATE 并排队。拿到锁后必须重新读这个判重字段，
            # 否则会继续使用排队查询开始时的旧值，为同一 CREATE2 地址重复建任务。
            slot.refresh_from_db(fields=["deploy_tx_task"])

            if slot.deploy_tx_task_id is not None:
                deploy_task = EvmTxTask.objects.select_related("base_task").get(
                    pk=slot.deploy_tx_task_id
                )
                base_task = deploy_task.base_task
                # 仍在途或已确认成功的部署任务直接复用；只有失败终局才需重建。
                if base_task.status != TxTaskStatus.FAILED:
                    return deploy_task

            if VaultSlot._is_deployed_on_chain(chain=slot.chain, address=slot.address):
                return None

            system_wallet = SystemWallet.get_current()
            sender = system_wallet.wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
            )
            vault_address = slot.project.vault
            if not vault_address:
                raise RuntimeError(
                    f"Project {slot.project_id} VaultSlot Vault 地址未配置"
                )
            vault_address = Web3.to_checksum_address(vault_address)

            # 锁住 VaultSlot 本行后再创建任务，避免并发 on_commit 调度同时看到
            # deploy_tx_task 为空，从而为同一个 CREATE2 地址创建多笔部署交易。
            intent = build_vault_slot_deploy_intent(
                sender=sender,
                chain=slot.chain,
                factory_address=XCASH_VAULT_SLOT_FACTORY_ADDRESS,
                vault_address=vault_address,
                salt=bytes(slot.salt),
            )
            task = EvmTxTask.schedule(intent)
            if isinstance(task, EvmTxTask):
                VaultSlot.objects.filter(pk=slot.pk).update(deploy_tx_task=task)
            return task

    @staticmethod
    def schedule_collect_for_deposit(
        deposit_pk: int,
    ) -> VaultSlotCollectSchedule | None:
        from deposits.models import Deposit

        deposit = Deposit.objects.select_related(
            "customer",
            "transfer__chain",
            "transfer__crypto",
        ).get(pk=deposit_pk)
        transfer = deposit.transfer
        chain = transfer.chain
        crypto = transfer.crypto

        if crypto == chain.native_coin:
            return None

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

        return VaultSlot.schedule_collect_for_slot(
            chain=chain,
            crypto=crypto,
            slot=slot,
            missing_token_message=(
                f"Crypto {crypto.symbol} 未部署在链 {chain.code}，无法调度 VaultSlot 归集"
            ),
        )

    @staticmethod
    def schedule_collect_for_invoice(
        invoice_pk: int,
    ) -> VaultSlotCollectSchedule | None:
        from invoices.models import Invoice
        from invoices.models import InvoiceBillingMode

        invoice = Invoice.objects.select_related(
            "project",
            "chain",
            "crypto",
        ).get(pk=invoice_pk)

        if invoice.billing_mode != InvoiceBillingMode.CONTRACT:
            return None
        if (
            invoice.chain_id is None
            or invoice.crypto_id is None
            or not invoice.pay_address
        ):
            return None

        chain = invoice.chain
        crypto = invoice.crypto
        if crypto == chain.native_coin:
            return None

        try:
            slot = VaultSlot.objects.get(
                chain=chain,
                project=invoice.project,
                usage=VaultSlotUsage.INVOICE,
                address=invoice.pay_address,
            )
        except VaultSlot.DoesNotExist as exc:
            raise RuntimeError(
                "Invoice VaultSlot 不存在："
                f"invoice_id={invoice.pk} chain={chain.code} "
                f"project_id={invoice.project_id} address={invoice.pay_address}"
            ) from exc

        return VaultSlot.schedule_collect_for_slot(
            chain=chain,
            crypto=crypto,
            slot=slot,
            missing_token_message=(
                f"Crypto {crypto.symbol} 未部署在链 {chain.code}，无法调度 Invoice VaultSlot 归集"
            ),
        )

    @staticmethod
    def schedule_collect_for_slot(
        *,
        chain: Chain,
        crypto,
        slot: VaultSlot,
        missing_token_message: str,
    ) -> VaultSlotCollectSchedule:
        if not crypto.address(chain):
            raise RuntimeError(missing_token_message)

        return VaultSlotCollectSchedule.ensure_pending(
            chain=chain,
            vault_slot=slot,
            crypto=crypto,
        )

    @staticmethod
    def create_collect_tx_task_for_slot(
        *,
        chain: Chain,
        crypto,
        slot: VaultSlot,
        missing_token_message: str,
    ) -> EvmTxTask:
        token_address = crypto.address(chain)
        if not token_address:
            raise RuntimeError(missing_token_message)

        # 归集交易只把 VaultSlot 内的资金转给合约写死的 vault，collect() 无权限校验，
        # 调用方仅承担 gas。故与部署一样统一用系统热钱包，全局只需维护一个热钱包的 gas。
        sender = SystemWallet.get_current().wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )
        intent = build_vault_slot_collect_intent(
            sender=sender,
            chain=chain,
            vault_slot_address=slot.address,
            token_address=token_address,
        )
        existing_task = (
            EvmTxTask.objects.filter(
                sender=sender,
                chain=chain,
                to=intent.to,
                data=intent.data,
                base_task__tx_type=TxTaskType.VaultSlotCollect,
            )
            .exclude(base_task__status__in=TERMINAL_TX_TASK_STATUSES)
            .first()
        )
        if existing_task is not None:
            return existing_task

        return EvmTxTask.schedule(intent)


class VaultSlotCollectSchedule(models.Model):
    """VaultSlot ERC20 归集窗口计划。

    计划只负责把同一链、同一 VaultSlot、同一币种的多次到账聚合到 due_at；
    到期后创建的 EvmTxTask 继续承载广播、确认和失败状态。
    """

    vault_slot = models.ForeignKey(
        VaultSlot,
        on_delete=models.CASCADE,
        related_name="collect_schedules",
        verbose_name=_("收款地址"),
    )
    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    crypto = models.ForeignKey(
        "currencies.Crypto",
        on_delete=models.PROTECT,
        verbose_name=_("币种"),
    )
    due_at = models.DateTimeField(_("计划执行时间"), db_index=True)
    tx_task = models.OneToOneField(
        "evm.EvmTxTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="vault_slot_collect_schedule",
        verbose_name=_("链上任务"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "vault_slot", "crypto"),
                condition=models.Q(tx_task__isnull=True),
                name="uniq_pending_vault_slot_collect",
            ),
        ]
        ordering = ("due_at", "pk")
        verbose_name = _("收款地址归集计划")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.chain_id}:{self.vault_slot_id}:{self.crypto_id}"

    @classmethod
    def ensure_pending(
        cls,
        *,
        chain: Chain,
        vault_slot: VaultSlot,
        crypto,
    ) -> VaultSlotCollectSchedule:
        existing = cls.objects.filter(
            chain=chain,
            vault_slot=vault_slot,
            crypto=crypto,
            tx_task__isnull=True,
        ).first()
        if existing is not None:
            return existing

        due_at = timezone.now() + get_vault_slot_collect_delay()
        try:
            return cls.objects.create(
                chain=chain,
                vault_slot=vault_slot,
                crypto=crypto,
                due_at=due_at,
            )
        except IntegrityError:
            return cls.objects.get(
                chain=chain,
                vault_slot=vault_slot,
                crypto=crypto,
                tx_task__isnull=True,
            )

    def create_tx_task(self) -> EvmTxTask:
        return VaultSlot.create_collect_tx_task_for_slot(
            chain=self.chain,
            crypto=self.crypto,
            slot=self.vault_slot,
            missing_token_message=(
                f"Crypto {self.crypto.symbol} 未部署在链 {self.chain.code}，无法调度 VaultSlot 归集"
            ),
        )

    @classmethod
    def execute_due(cls, *, limit: int = 32) -> int:
        now = timezone.now()
        created_count = 0
        with db_transaction.atomic():
            schedules = list(
                cls.objects.select_for_update(skip_locked=True)
                .select_related(
                    "chain",
                    "crypto",
                )
                .filter(tx_task__isnull=True, due_at__lte=now)
                .order_by("due_at", "pk")[:limit]
            )
            for schedule in schedules:
                tx_task = schedule.create_tx_task()
                schedule.tx_task = tx_task
                schedule.save(update_fields=["tx_task", "updated_at"])
                created_count += 1
        return created_count


class DepositVaultSlot(VaultSlot):
    class Meta:
        proxy = True
        verbose_name = _("充币收款地址")
        verbose_name_plural = verbose_name


class InvoiceVaultSlot(VaultSlot):
    class Meta:
        proxy = True
        verbose_name = _("账单收款地址")
        verbose_name_plural = verbose_name


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

    def broadcast(self, *, allow_pending_chain_rebroadcast: bool = False) -> None:
        if not self._can_broadcast_for_current_status(
            allow_pending_chain_rebroadcast=allow_pending_chain_rebroadcast
        ):
            return
        if self._recover_queued_receipt_if_any():
            return
        if self._is_broadcast_order_blocked(
            allow_pending_chain_rebroadcast=allow_pending_chain_rebroadcast
        ):
            return

        self._record_broadcast_attempt()
        self._ensure_signed_with_latest_gas_price()

        if not self._passes_balance_preflight():
            return

        self._send_signed_payload()

    def _is_broadcast_order_blocked(
        self, *, allow_pending_chain_rebroadcast: bool
    ) -> bool:
        if self.has_lower_queued_nonce():
            return True
        return not allow_pending_chain_rebroadcast and self.is_pipeline_full()

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
                self._mark_pending_chain()
                return
            raise
        self._mark_pending_chain()

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

        send_raw_transaction 可能已被节点接受，但 worker 在 _mark_pending_chain 前
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
            self._mark_pending_chain()
            EvmTaskPoller.process_succeeded_receipt(
                evm_task=self,
                tx_hash=tx_hash,
                receipt=receipt,
            )
            return True
        if status == 0:
            self._mark_pending_chain()
            EvmTaskPoller.finalize_failed_task(evm_task=self)
            return True
        raise RuntimeError("EVM receipt status missing or invalid")

    def _can_broadcast_for_current_status(
        self, *, allow_pending_chain_rebroadcast: bool
    ) -> bool:
        """校验当前父任务状态是否允许进入真实广播副作用。"""
        base_task = TxTask.objects.only("status").get(pk=self.base_task_id)
        if base_task.status in TERMINAL_TX_TASK_STATUSES:
            return False
        if base_task.status == TxTaskStatus.PENDING_CHAIN:
            return allow_pending_chain_rebroadcast
        return base_task.status == TxTaskStatus.QUEUED

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

    def _mark_pending_chain(self) -> None:
        # 首次成功提交到节点后，统一父任务从"待广播"进入"待上链"。
        TxTask.objects.filter(
            pk=self.base_task_id,
            status=TxTaskStatus.QUEUED,
        ).update(
            status=TxTaskStatus.PENDING_CHAIN,
            updated_at=timezone.now(),
        )

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
                base_task__status=TxTaskStatus.PENDING_CHAIN,
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

        通过 AddressChainState 行锁对 (sender, chain) 串行化，杜绝并发 nonce
        冲突。verify_fn 必须在行锁内、nonce 分配前执行；验证失败时整个事务
        回滚，避免留下未通过业务二次校验的 TxTask 或 nonce 空洞。

        首次签名和首个 tx_hash 生成延后到 broadcast()；内部稳定身份只依赖
        (sender, chain, nonce)。
        """
        if intent.value != 0:
            raise ValueError("EVM task value must be 0")

        with db_transaction.atomic():
            AddressChainState.acquire_for_update(
                address=intent.sender,
                chain=intent.chain,
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

        调用方必须已通过 AddressChainState.acquire_for_update() 持有行锁，
        确保基于 EvmTxTask 推导 nonce 与创建任务处于同一串行化区间。
        """
        latest_nonce = (
            EvmTxTask.objects.filter(sender=address, chain=chain)
            .aggregate(max_nonce=models.Max("nonce"))
            .get("max_nonce")
        )
        return 0 if latest_nonce is None else int(latest_nonce) + 1  # noqa
