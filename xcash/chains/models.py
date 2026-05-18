from __future__ import annotations

import contextlib
import enum
from decimal import Decimal
from functools import cached_property
from typing import TYPE_CHECKING
from typing import Any

import environ
import structlog
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import models
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from web3 import Web3
from web3.exceptions import ExtraDataLengthError
from web3.middleware import ExtraDataToPOAMiddleware

from common.fields import AddressField
from common.fields import EvmAddressField
from common.fields import HashField
from common.models import UndeletableModel

if TYPE_CHECKING:
    from currencies.models import Crypto
    from deposits.models import Deposit
    from deposits.models import DepositCollection
    from invoices.models import Invoice
    from withdrawals.models import Withdrawal

env = environ.Env()
logger = structlog.get_logger()


# Create your models here.
class ChainType(models.TextChoices):
    EVM = "evm", "EVM"
    TRON = "tron", "Tron"


class Chain(models.Model):
    name = models.CharField(
        _("名称"),
        unique=True,
    )
    code = models.CharField(
        _("代码"),
        unique=True,
    )
    type = models.CharField(
        _("类型"),
        choices=ChainType,
    )
    native_coin = models.ForeignKey(
        "currencies.Crypto",
        verbose_name="原生币",
        on_delete=models.PROTECT,
        related_name="chains_as_native_coin",
    )
    confirm_block_count = models.PositiveIntegerField(
        default=10,
        verbose_name=_("区块确认数"),
    )
    latest_block_number = models.PositiveIntegerField(
        default=0, verbose_name=_("最新区块")
    )

    active = models.BooleanField(default=False, verbose_name=_("启用"))

    # For EVM
    base_transfer_gas = models.PositiveIntegerField(
        _("原生币转账 Gas Limit"),
        default=50_000,
        help_text=_("原生币（ETH/BNB 等）转账的 gas 上限"),
    )
    erc20_transfer_gas = models.PositiveIntegerField(
        _("ERC20 转账 Gas Limit"),
        default=100_000,
        help_text=_("ERC-20 代币 transfer 调用的 gas 上限"),
    )
    # CREATE2 工厂合约地址：未启用合约支付收款的链留空
    create2_factory_address = EvmAddressField(
        _("CREATE2 工厂合约地址"),
        blank=True,
        null=True,
    )
    evm_log_max_block_range = models.PositiveIntegerField(
        _("EVM 单次日志请求最大区块数"),
        default=10,
        help_text=_("EVM 扫描器单次 eth_getLogs 请求允许覆盖的最大区块数。"),
    )
    chain_id = models.PositiveIntegerField(
        _("Chain ID"),
        unique=True,
        blank=True,
        null=True,
    )
    rpc = models.CharField(_("RPC"), blank=True, default="")
    tron_api_key = models.CharField(_("Tron API Key"), blank=True, default="")
    is_poa = models.BooleanField(_("POA"), blank=True, null=True)

    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("type",),
                condition=~models.Q(type=ChainType.EVM),
                name="uniq_chain_type_for_non_evm",
            ),
        ]
        verbose_name = _("链")
        verbose_name_plural = _("链")

    # 当前产品允许创建 EVM / Tron 两类链。
    PRODUCT_ENABLED_TYPES = (ChainType.EVM, ChainType.TRON)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # 链创建属于系统级配置，统一在模型层执行校验，避免后台和脚本绕过限制。
        self.full_clean()

        # EVM 链在 RPC 变更时自动检测 chain_id 和 POA
        if self.type == ChainType.EVM and self.rpc:
            rpc_changed = self.pk is None  # 新建时视为变更
            if not rpc_changed:
                old_rpc = (
                    Chain.objects.filter(pk=self.pk)
                    .values_list("rpc", flat=True)
                    .first()
                )
                rpc_changed = old_rpc != self.rpc
            if rpc_changed:
                # 仅在 chain_id 未显式指定时才自动检测，避免覆盖调用方明确传入的值
                if not self.chain_id:
                    self.chain_id = self._detect_chain_id()
                self.is_poa = self._detect_poa()
                # 清除 w3 缓存，确保下次访问使用新 RPC 和 POA 配置
                self.__dict__.pop("w3", None)
        elif self.type == ChainType.TRON:
            self.chain_id = None
            self.is_poa = None
            self.confirm_block_count = 0
            self.rpc = ""
            self.tron_api_key = self.tron_api_key.strip()
        elif self.type != ChainType.EVM:
            self.chain_id = None
            self.is_poa = None
        if self.type != ChainType.TRON:
            self.tron_api_key = ""

        with db_transaction.atomic():
            result = super().save(*args, **kwargs)
            self._sync_tron_usdt_watch_cursor()
        return result

    def _sync_tron_usdt_watch_cursor(self) -> None:
        """活跃 Tron 链应在配置层持有 USDT 扫描游标，避免依赖首次 beat 扫描显式创建。"""
        if self.type != ChainType.TRON or not self.active:
            return

        from tron.models import TronWatchCursor

        from currencies.models import ChainToken

        usdt_mapping = (
            ChainToken.objects.filter(
                chain=self,
                crypto__symbol="USDT",
                crypto__active=True,
            )
            .exclude(address="")
            .only("address")
            .first()
        )
        if usdt_mapping is None:
            return

        TronWatchCursor.objects.get_or_create(
            chain=self,
            contract_address=usdt_mapping.address,
            defaults={
                "last_scanned_block": 0,
                "last_safe_block": 0,
                "enabled": True,
            },
        )

    def clean(self) -> None:
        """限制后台和脚本只能创建当前产品阶段启用的链类型。"""
        super().clean()
        if self.type and self.type not in self.PRODUCT_ENABLED_TYPES:
            raise ValidationError(
                {"type": _("当前版本仅支持创建 EVM / Tron 链。")}
            )

    def _detect_chain_id(self) -> int | None:
        """通过 RPC 获取链的 chain_id。"""
        try:
            w3 = Web3(Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 8}))
            chain_id = w3.eth.chain_id
        except Exception:
            return self.chain_id
        else:
            return chain_id

    def _detect_poa(self) -> bool:
        """通过获取最新区块的 extraData 长度判断是否为 POA 链（如 BSC）。"""
        try:
            w3 = Web3(Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 8}))
            block = w3.eth.get_block("latest")
            # POA 链的 extraData（proofOfAuthorityData）通常远超 32 字节
            extra = block.get("proofOfAuthorityData") or block.get("extraData", b"")
            return len(extra) > 32
        except ExtraDataLengthError:
            # web3.py 在格式化区块前就会拦截超长 extraData；这个异常本身就是 POA 信号。
            return True
        except Exception:
            # RPC 短暂失败时保留已有配置，避免一次探测失败把 BSC 误改成非 POA。
            return bool(self.is_poa)

    def content(self):
        return {
            "name": self.name,
            "code": self.code,
            "type": self.type,
            "chain_id": self.chain_id if self.chain_id else None,
            "native_coin": self.native_coin.symbol,
        }

    @cached_property
    def w3(self):
        return self._build_w3()

    def _build_w3(self, *, force_poa: bool = False) -> Web3:
        w3 = Web3(Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 8}))
        if force_poa or self.is_poa:
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        return w3

    def get_block_with_poa_retry(
        self,
        block_identifier: int | str,
        *,
        full_transactions: bool = False,
    ) -> Any:
        """读取区块时遇到 POA extraData 校验错误，自动标记链并用 POA middleware 重试。"""
        try:
            return self.w3.eth.get_block(
                block_identifier,
                full_transactions=full_transactions,
            )
        except ExtraDataLengthError:
            self._mark_as_poa()
            retry_w3 = self._build_w3(force_poa=True)
            self.__dict__["w3"] = retry_w3
            return retry_w3.eth.get_block(
                block_identifier,
                full_transactions=full_transactions,
            )

    def _mark_as_poa(self) -> None:
        if self.pk:
            self.__class__.objects.filter(pk=self.pk).update(is_poa=True)
        self.is_poa = True

    @property
    def adapter(self) -> "AdapterInterface":  # noqa
        from chains.adapters import AdapterFactory

        return AdapterFactory.get_adapter(chain_type=self.type)

    @property
    def get_latest_block_number(self) -> int:
        if self.type == ChainType.EVM:
            return self.w3.eth.block_number
        if self.type == ChainType.TRON:
            # Tron 区块轮询尚未接入专用 RPC 适配器。
            # 过渡期对公共 update_latest_block 仅返回数据库中已知高度，
            # 保证 active Tron 链不会在定时任务中抛异常或误推进确认流程。
            return self.latest_block_number
        msg = f"Unsupported chain type: {self.type}"
        raise NotImplementedError(msg)


class AddressUsage(models.TextChoices):
    Deposit = "deposit", _("充币帐户")
    Vault = "vault", _("金库账户")


class AddressChainState(models.Model):
    """按 (address, chain) 维护串行化状态。"""

    address = models.ForeignKey(
        "Address",
        on_delete=models.CASCADE,
        related_name="chain_states",
        verbose_name=_("地址"),
    )
    chain = models.ForeignKey(
        "Chain",
        on_delete=models.CASCADE,
        related_name="address_states",
        verbose_name=_("链"),
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("address", "chain"),
                name="uniq_address_chain_state_address_chain",
            ),
        ]
        verbose_name = _("地址链状态")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.address_id}:{self.chain_id}"

    @classmethod
    def acquire_for_update(
        cls,
        *,
        address: Address,
        chain: Chain,
    ) -> AddressChainState:
        try:
            state, _created = cls.objects.get_or_create(address=address, chain=chain)
        except IntegrityError:
            # 并发首次创建撞唯一约束时：对方事务已写索引但尚未对本事务可见，
            # 无锁 get() 会误判 DoesNotExist。用 select_for_update 加锁回查，
            # 等对方事务提交后命中记录。调用方必须已在 atomic 事务中。
            return cls.objects.select_for_update().get(address=address, chain=chain)
        return cls.objects.select_for_update().get(pk=state.pk)


# BIP44 account' 层级与业务用途的映射，直接用于派生路径 m/44'/coin'/account'/0/address_index。
BIP44_ACCOUNT_MAP: dict[str, int] = {
    AddressUsage.Vault: 0,
    AddressUsage.Deposit: 1,
}


class Wallet(UndeletableModel):
    class Meta:
        verbose_name = _("钱包")
        verbose_name_plural = _("钱包")

    def __str__(self):
        # 钱包展示名必须是稳定标识，不能把所有"非项目钱包"都误判成 Core。
        if hasattr(self, "project"):
            return f"Wallet-{self.project.appid}"
        wallet_identifier = self.pk if self.pk is not None else "unsaved"
        return f"Wallet-{wallet_identifier}"

    @classmethod
    def generate(cls) -> Wallet:
        """
        创建钱包引用并委托独立 signer 生成远端钱包。
        :return:
        """
        from chains.signer import SignerServiceError
        from chains.signer import get_signer_backend

        signer_backend = get_signer_backend()
        try:
            with db_transaction.atomic():
                # 主应用本地只保存钱包引用，密钥材料完全由 signer 托管。
                wallet = cls.objects.create()
                signer_backend.create_wallet(wallet_id=wallet.pk)
        except SignerServiceError as exc:
            raise RuntimeError("signer 服务不可用，无法创建新钱包") from exc
        return wallet

    @staticmethod
    def get_bip44_account(usage: AddressUsage | str) -> int:
        """将业务用途映射为 BIP44 account' 层级。"""
        bip44_account = BIP44_ACCOUNT_MAP.get(usage)  # type: ignore[arg-type]
        if bip44_account is None:
            raise ValueError(f"未知的 AddressUsage: {usage}")
        return bip44_account

    def get_address(
        self,
        chain_type: ChainType | str,
        usage: AddressUsage,
        address_index: int = 0,
    ) -> Address:
        """
        从当前 Wallet 派生指定链类型和用途的地址。
        先查数据库，不存在时通过 BIP44 推导地址并创建；
        并发安全：若并发创建触发唯一约束则重新查询。
        """
        from django.db import IntegrityError

        bip44_account = self.get_bip44_account(usage)

        from chains.signer import SignerServiceError
        from chains.signer import get_signer_backend
        try:
            expected_address = get_signer_backend().derive_address(
                wallet=self,
                chain_type=chain_type,
                bip44_account=bip44_account,
                address_index=address_index,
            )
        except SignerServiceError as exc:
            raise RuntimeError(
                f"signer 服务不可用，无法为钱包 {self.pk} 派生地址"
            ) from exc

        created = False
        try:
            addr_obj, created = Address.objects.get_or_create(
                wallet=self,
                chain_type=chain_type,
                usage=usage,
                address_index=address_index,
                defaults={
                    "bip44_account": bip44_account,
                    "address": expected_address,
                },
            )
        except Address.MultipleObjectsReturned as exc:
            raise RuntimeError(
                "Address 身份数据已损坏："
                f"wallet_id={self.pk} chain_type={chain_type} "
                f"usage={usage} address_index={address_index} 存在多条记录"
            ) from exc
        except IntegrityError as exc:
            # 只有地址身份唯一键竞争时才允许回查；其他唯一约束错误要继续暴露。
            # 并发首次派生同一 HD 身份时，RC 隔离下对方事务的 INSERT 可能已写入
            # 索引（触发 unique 冲突）但尚未对本事务可见；用 select_for_update 加锁
            # 回查，等对方事务提交后再读，避免误判 DoesNotExist。
            try:
                with db_transaction.atomic():
                    addr_obj = Address.objects.select_for_update().get(
                        wallet=self,
                        chain_type=chain_type,
                        usage=usage,
                        address_index=address_index,
                    )
            except Address.DoesNotExist as not_exist_exc:
                raise exc from not_exist_exc

        # 地址身份一旦确定，bip44_account 和 address 都必须与 HD 推导结果一致，否则就是脏数据。
        if (
            addr_obj.bip44_account != bip44_account
            or addr_obj.address != expected_address
        ):
            raise RuntimeError(
                "Address 身份数据已损坏："
                f"wallet_id={self.pk} chain_type={chain_type} "
                f"usage={usage} address_index={address_index} "
                f"expected_bip44_account={bip44_account} actual_bip44_account={addr_obj.bip44_account} "
                f"expected_address={expected_address} actual_address={addr_obj.address}"
            )

        return addr_obj


class Address(UndeletableModel):
    wallet = models.ForeignKey(
        "Wallet", on_delete=models.CASCADE, verbose_name=_("钱包")
    )
    chain_type = models.CharField(choices=ChainType, verbose_name=_("链类型"))
    usage = models.CharField(_("用途"), choices=AddressUsage)
    # BIP44 account' 层级，由 usage 决定（冗余存储用于查询优化和数据校验）。
    bip44_account = models.PositiveIntegerField(_("BIP44 账户层级"))
    # BIP44 address_index 层级，该用途下的地址序号。
    address_index = models.PositiveIntegerField(_("地址索引"), default=0)
    address = AddressField(_("地址"), unique=True)

    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # (wallet, chain_type, usage, address_index) 是 BIP44 派生地址的唯一身份。
            # 同一 address_index 可以在不同 usage 下各存在一条（因 bip44_account 不同）。
            models.UniqueConstraint(
                fields=("wallet", "chain_type", "usage", "address_index"),
                name="uniq_address_wallet_chain_usage_addridx",
            ),
        ]
        verbose_name = _("地址")
        verbose_name_plural = _("地址")

    def __str__(self):
        return f"{self.address}"

    def send_crypto(
        self,
        crypto: Crypto,
        chain: Chain,
        to: str,
        amount: Decimal,
        action_type: OnchainActionType,
    ) -> str:
        """使用本账户私钥签名并发送转账，返回 tx hash / signature。

        EVM：仅创建内部任务，首次广播时才生成首个 tx_hash。
        """
        if chain.type == ChainType.EVM:
            # EvmBroadcastTask 内部管理锁，不在此处获取，避免双重加锁。
            from evm.intents import build_erc20_transfer_intent  # noqa: PLC0415
            from evm.intents import build_native_transfer_intent  # noqa: PLC0415
            from evm.models import EvmBroadcastTask  # noqa: PLC0415

            decimals = crypto.get_decimals(chain)
            value_raw = int(amount * Decimal(10**decimals))
            if crypto == chain.native_coin:
                intent = build_native_transfer_intent(
                    address=self,
                    chain=chain,
                    to=to,
                    value=value_raw,
                    action_type=action_type,
                )
            else:
                intent = build_erc20_transfer_intent(
                    address=self,
                    chain=chain,
                    crypto=crypto,
                    to=to,
                    value_raw=value_raw,
                    action_type=action_type,
                )
            task = EvmBroadcastTask.schedule(intent)
            return task.base_task.tx_hash

        msg = f"Unsupported chain type for send_crypto: {chain.type}"
        raise NotImplementedError(msg)



class OnchainActionType(models.TextChoices):
    Invoice = "iv", _("💳 支付")
    Deposit = "deposit", "💰 充币"
    Withdrawal = "withdrawal", "🏧 提币"

    GasRecharge = "gas-recharge", "⛽ Gas分发"
    DepositCollection = "deposit-collection", "💵 归集充币"
    Prefunding = "prefunding", "🏦 注入金库资金"
    X402Facilitate = "x402_facilitate", _("x402 代付")
    ContractDeployCollect = "contract_deploy_collect", _("合约部署归集")


class BroadcastTaskStage(models.TextChoices):
    QUEUED = "queued", _("待广播")
    PENDING_CHAIN = "pending_chain", _("待上链")
    PENDING_CONFIRM = "pending_confirm", _("确认中")
    FINALIZED = "finalized", _("已终结")


class BroadcastTaskResult(models.TextChoices):
    UNKNOWN = "unknown", _("未知")
    SUCCESS = "success", _("成功")
    FAILED = "failed", _("失败")


class BroadcastTaskFailureReason(models.TextChoices):
    # 通用失败原因：适用于所有链共享的提交与调度失败路径。
    RPC_REJECTED = "rpc_rejected", _("节点拒绝")
    INSUFFICIENT_BALANCE = "insufficient_balance", _("余额不足")
    FEE_TOO_LOW = "fee_too_low", _("手续费过低")

    # EVM 特有失败原因：当前只保留真正可能落到已创建 BroadcastTask 终局的链上失败原因。
    EXECUTION_REVERTED = "execution_reverted", _("链上执行回退")
    EXPECTED_TRANSFER_MISSING = "expected_transfer_missing", _("链上执行成功但预期资产移动缺失")


class TxHash(models.Model):
    broadcast_task = models.ForeignKey(
        "BroadcastTask",
        on_delete=models.CASCADE,
        related_name="tx_hashes",
        verbose_name=_("链上任务"),
    )
    chain = models.ForeignKey(
        "Chain",
        on_delete=models.PROTECT,
        related_name="tx_hashes",
        verbose_name=_("链"),
    )
    hash = HashField(unique=False, verbose_name=_("交易哈希"))
    version = models.PositiveIntegerField(_("版本"))
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        ordering = ("broadcast_task_id", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "hash"),
                name="uniq_tx_hash_chain_hash",
            ),
            models.UniqueConstraint(
                fields=("broadcast_task", "version"),
                name="uniq_tx_hash_task_version",
            ),
        ]
        verbose_name = _("交易哈希历史")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.hash

    def clean(self) -> None:
        super().clean()
        if self.broadcast_task_id and self.chain_id != self.broadcast_task.chain_id:
            raise ValidationError(
                {"chain": _("TxHash.chain 必须与 BroadcastTask.chain 保持一致。")}
            )


class BroadcastTask(UndeletableModel):
    """跨链统一的链上任务锚点。

    设计原则：
    - stage 只描述当前所处阶段：待广播 / 待上链 / 确认中 / 已终结。
    - result 只描述终局结果：未知 / 成功 / 失败。
    - 广播重试等实现细节继续留在各链子表，避免把"是否广播"污染到统一领域模型。
    - Withdrawal 等业务对象统一外键到该模型，不再直接依赖具体链实现或 tx hash。
    """

    chain = models.ForeignKey(
        "Chain",
        on_delete=models.PROTECT,
        verbose_name=_("链"),
    )
    address = models.ForeignKey(
        "Address",
        on_delete=models.PROTECT,
        verbose_name=_("地址"),
    )
    action_type = models.CharField(
        _("类型"),
        choices=OnchainActionType,
    )
    tx_hash = HashField(
        unique=False,
        verbose_name=_("交易哈希"),
        blank=True,
        null=True,
    )
    stage = models.CharField(
        _("阶段"),
        choices=BroadcastTaskStage,
        default=BroadcastTaskStage.QUEUED,
    )
    result = models.CharField(
        _("结果"),
        choices=BroadcastTaskResult,
        default=BroadcastTaskResult.UNKNOWN,
    )
    # failure_reason 使用统一枚举，便于跨链统计失败来源与后台筛选。
    failure_reason = models.CharField(
        _("失败原因"),
        max_length=64,
        choices=BroadcastTaskFailureReason,
        blank=True,
        default="",
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "tx_hash"),
                name="uniq_broadcast_task_chain_hash",
            ),
            models.CheckConstraint(
                # 只要出现成功/失败终局结果，就必须已经进入已终结阶段。
                condition=models.Q(result=BroadcastTaskResult.UNKNOWN)
                | models.Q(stage=BroadcastTaskStage.FINALIZED),
                name="ck_broadcast_task_result_requires_finalized_stage",
            ),
            models.CheckConstraint(
                # 已终结任务不能继续保留未知结果，否则会把阶段和结果语义混在一起。
                condition=~models.Q(stage=BroadcastTaskStage.FINALIZED)
                | ~models.Q(result=BroadcastTaskResult.UNKNOWN),
                name="ck_broadcast_task_finalized_requires_known_result",
            ),
            models.CheckConstraint(
                # 失败时必须明确记录失败原因；非失败任务不得携带失败原因。
                condition=(
                    models.Q(result=BroadcastTaskResult.FAILED)
                    & ~models.Q(failure_reason="")
                )
                | (
                    ~models.Q(result=BroadcastTaskResult.FAILED)
                    & models.Q(failure_reason="")
                ),
                name="ck_broadcast_task_failure_reason_matches_result",
            ),
        ]
        verbose_name = _("链上任务")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.tx_hash or f"broadcast-task-{self.pk or 'unsaved'}"

    def clean(self) -> None:
        """在模型层显式约束阶段、结果、失败原因三者的一致性。"""
        super().clean()
        errors = {}

        if self.result in {BroadcastTaskResult.SUCCESS, BroadcastTaskResult.FAILED}:
            if self.stage != BroadcastTaskStage.FINALIZED:
                errors["stage"] = _("成功/失败结果只能出现在已终结阶段。")

        if (
            self.stage == BroadcastTaskStage.FINALIZED
            and self.result == BroadcastTaskResult.UNKNOWN
        ):
            errors["result"] = _("已终结任务必须给出成功或失败结果。")

        if self.result == BroadcastTaskResult.FAILED:
            if not self.failure_reason:
                errors["failure_reason"] = _("失败任务必须填写失败原因。")
        elif self.failure_reason:
            errors["failure_reason"] = _("仅失败任务允许填写失败原因。")

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # BroadcastTask 是跨链主锚点，统一在保存前执行 full_clean，避免后台和脚本写入脏状态。
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def display_status(self) -> str:
        """把阶段与结果合成为一个稳定的人类可读状态。"""
        # 使用 Django 自动生成的 get_FOO_display()，既能保留 choices 语义，
        # 也能避免 IDE 对 TextChoices.label 静态推断不准确导致的误报。
        if self.stage != BroadcastTaskStage.FINALIZED:
            return self.get_stage_display()
        return self.get_result_display()

    @property
    def is_confirmed(self) -> bool:
        return (
            self.stage == BroadcastTaskStage.FINALIZED
            and self.result == BroadcastTaskResult.SUCCESS
        )

    @db_transaction.atomic
    def append_tx_hash(self, tx_hash: str) -> TxHash:
        locked_task = BroadcastTask.objects.select_for_update().get(pk=self.pk)
        # 并发广播可能产生相同 tx_hash（相同 nonce + gas_price 签名结果相同），
        # 若已存在则视为幂等，直接返回。
        existing = TxHash.objects.filter(
            chain=locked_task.chain, hash=tx_hash
        ).first()
        if existing:
            self.tx_hash = tx_hash
            return existing
        max_version = (
            TxHash.objects.filter(broadcast_task=locked_task)
            .aggregate(max_version=models.Max("version"))
            .get("max_version")
        )
        next_version = 1 if max_version is None else int(max_version) + 1
        created = TxHash.objects.create(
            broadcast_task=locked_task,
            chain=locked_task.chain,
            hash=tx_hash,
            version=next_version,
        )
        BroadcastTask.objects.filter(pk=locked_task.pk).update(
            tx_hash=tx_hash,
            updated_at=timezone.now(),
        )
        if locked_task.action_type == OnchainActionType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(broadcast_task=locked_task).update(
                hash=tx_hash,
                updated_at=timezone.now(),
            )
        self.tx_hash = tx_hash
        return created

    @staticmethod
    def resolve_by_hash(*, chain: Chain, tx_hash: str) -> BroadcastTask | None:
        """通过 tx_hash 查找对应的 BroadcastTask。

        优先从 TxHash 历史记录匹配（覆盖 gas 重签后的旧 hash），
        未命中时回退到 BroadcastTask.tx_hash（当前 hash）。
        两张表均有 (chain, hash) 唯一约束，无需额外去重。
        """
        if not tx_hash:
            return None
        # 优先查历史记录（gas 重签后旧 hash 只存在于 TxHash 表）
        history = (
            TxHash.objects.select_related("broadcast_task")
            .filter(chain=chain, hash=tx_hash)
            .first()
        )
        if history is not None:
            return history.broadcast_task
        # 回退到当前 tx_hash
        return BroadcastTask.objects.filter(chain=chain, tx_hash=tx_hash).first()

    @staticmethod
    def mark_finalized_success(*, chain: Chain, tx_hash: str) -> int:
        """将匹配的任务标记为成功终局。

        使用 .update() 绕过 save()/full_clean() 以避免逐行加载，
        依赖 DB CheckConstraint 保证状态三元组一致性。
        """
        if not tx_hash:
            return 0
        task = BroadcastTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return 0
        updated = (
            BroadcastTask.objects.filter(pk=task.pk, result=BroadcastTaskResult.UNKNOWN)
            .exclude(stage=BroadcastTaskStage.FINALIZED)
            .update(
                tx_hash=tx_hash,
                stage=BroadcastTaskStage.FINALIZED,
                result=BroadcastTaskResult.SUCCESS,
                failure_reason="",
                updated_at=timezone.now(),
            )
        )
        if updated and task.action_type == OnchainActionType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(broadcast_task=task).update(
                hash=tx_hash,
                updated_at=timezone.now(),
            )
        return updated

    @staticmethod
    def mark_finalized_failed(
        *,
        task_id: int,
        reason: BroadcastTaskFailureReason,
        expected_stage: BroadcastTaskStage | None = None,
    ) -> int:
        """将匹配的任务标记为失败终局。

        失败终局必须保留失败原因，便于后续按失败类型统计和排查。
        """
        queryset = BroadcastTask.objects.filter(
            pk=task_id,
            result=BroadcastTaskResult.UNKNOWN,
        ).exclude(stage=BroadcastTaskStage.FINALIZED)
        if expected_stage is not None:
            queryset = queryset.filter(stage=expected_stage)
        return queryset.update(
            stage=BroadcastTaskStage.FINALIZED,
            result=BroadcastTaskResult.FAILED,
            failure_reason=reason,
            updated_at=timezone.now(),
        )

    @staticmethod
    def reset_to_pending_chain(*, chain: Chain, tx_hash: str) -> int:
        """将匹配的任务回退到待上链阶段（用于 Transfer drop / reorg 恢复）。

        使用 .update() 绕过 save()/full_clean() 以避免逐行加载，
        依赖 DB CheckConstraint 保证状态三元组一致性。
        """
        task = BroadcastTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return 0
        updated = BroadcastTask.objects.filter(
            pk=task.pk,
            stage=BroadcastTaskStage.PENDING_CONFIRM,
            result=BroadcastTaskResult.UNKNOWN,
        ).update(
            tx_hash=tx_hash,
            stage=BroadcastTaskStage.PENDING_CHAIN,
            result=BroadcastTaskResult.UNKNOWN,
            failure_reason="",
            updated_at=timezone.now(),
        )
        if updated and task.action_type == OnchainActionType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(broadcast_task=task).update(
                hash=tx_hash,
                updated_at=timezone.now(),
            )
        return updated

    @staticmethod
    def mark_pending_confirm(*, chain: Chain, tx_hash: str) -> int:
        """链上已观察到交易后，将未终结的任务推进到确认中阶段。

        使用 .update() 绕过 save()/full_clean() 以避免逐行加载，
        依赖 DB CheckConstraint 保证状态三元组一致性。
        """
        if not tx_hash:
            return 0
        task = BroadcastTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return 0
        updated = (
            BroadcastTask.objects.filter(pk=task.pk)
            .exclude(stage=BroadcastTaskStage.FINALIZED)
            .update(
                tx_hash=tx_hash,
                stage=BroadcastTaskStage.PENDING_CONFIRM,
                result=BroadcastTaskResult.UNKNOWN,
                failure_reason="",
                updated_at=timezone.now(),
            )
        )
        if updated and task.action_type == OnchainActionType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(broadcast_task=task).update(
                hash=tx_hash,
                updated_at=timezone.now(),
            )
        return updated


class TransferStatus(models.TextChoices):
    CONFIRMING = "confirming", _("确认中")
    CONFIRMED = "confirmed", _("已确认")


class ConfirmMode(models.TextChoices):
    FULL = "full", _("完全")
    QUICK = "quick", _("快速")


class _EvmSenderClass(enum.Enum):
    SYSTEM = "system"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class OnchainTransfer(models.Model):
    if TYPE_CHECKING:
        # Django 反向 OneToOne 描述符在运行时动态挂载；这里显式声明给 IDE 做静态解析。
        invoice: Invoice
        deposit: Deposit
        deposit_collection: DepositCollection
        withdrawal: Withdrawal

    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    block = models.IntegerField(_("区块"))
    block_hash = HashField(
        verbose_name=_("区块哈希"),
        unique=False,
        blank=True,
        null=True,
    )
    # 修复：真实链上 tx hash 与"同 tx 内事件明细"拆分建模，避免继续依赖 `hash:logIndex` 字符串协议。
    hash = HashField(unique=False, verbose_name=_("哈希"))
    event_id = models.CharField(
        _("事件标识"),
        max_length=32,
        blank=True,
        default="",
        db_index=True,
    )

    crypto = models.ForeignKey(
        "currencies.Crypto", on_delete=models.CASCADE, verbose_name=_("加密货币")
    )
    from_address = AddressField(_("发送地址"))
    to_address = AddressField(_("目的地址"))
    value = models.DecimalField(_("数值"), max_digits=32, decimal_places=0)
    amount = models.DecimalField(_("数量"), max_digits=32, decimal_places=8)

    type = models.CharField(
        _("类型"),
        choices=OnchainActionType,
        blank=True,
        default="",
    )
    confirm_mode = models.CharField(
        choices=ConfirmMode,
        default=ConfirmMode.FULL,
        max_length=8,
        verbose_name=_("确认模式"),
        help_text=_("当前仅 Invoice 业务根据 fast_confirm_threshold 动态设置 QUICK/FULL；Deposit 与 Withdrawal 始终使用默认 FULL，走完整区块确认流程。"),
    )
    timestamp = models.PositiveIntegerField(verbose_name=_("时间戳"), db_index=True)
    datetime = models.DateTimeField(verbose_name=_("日期"))
    status = models.CharField(
        choices=TransferStatus,
        default=TransferStatus.CONFIRMING,
        verbose_name=_("状态"),
    )

    processed_at = models.DateTimeField(_("处理时间"), blank=True, null=True)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        ordering = ("-timestamp",)
        verbose_name = _("转账")
        verbose_name_plural = _("转账")
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "hash", "event_id"),
                name="uniq_transfer_chain_hash_event",
            ),
        ]

    def __str__(self):
        return self.hash

    @db_transaction.atomic
    def process(self):
        # 先加行锁再刷新，防止两个 Celery worker 并发处理同一笔转账
        OnchainTransfer.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.processed_at:
            return

        # 优先通过 TxHash 匹配内部广播交易（gas补充、归集、提币），
        # 一次 resolve 即可定位业务类型，避免逐一 try_match 重复查询。
        broadcast_task = BroadcastTask.resolve_by_hash(
            chain=self.chain, tx_hash=self.hash
        )
        if broadcast_task is not None:
            if self._match_internal(broadcast_task):
                self._mark_processed()
                if self.confirm_mode == ConfirmMode.QUICK:
                    from .tasks import confirm_transfer

                    confirm_transfer.delay(self.pk)
                return

            logger.warning(
                "OnchainTransfer 命中 BroadcastTask 但内部 handler 未认领，跳过外部匹配",
                transfer_id=self.pk,
                broadcast_task_id=broadcast_task.pk,
                tx_hash=self.hash,
            )
            self._mark_processed()
            return

        if self.chain.type == ChainType.EVM:
            sender_class = self._classify_evm_sender()
            if sender_class == _EvmSenderClass.SYSTEM:
                logger.warning(
                    "EVM OnchainTransfer 原交易发送方是系统地址但无 BroadcastTask，跳过外部匹配",
                    transfer_id=self.pk,
                    tx_hash=self.hash,
                )
                self._mark_processed()
                return
            if sender_class == _EvmSenderClass.UNKNOWN:
                logger.warning(
                    "无法确认 EVM 交易发送方分类，保留未处理状态等待重试",
                    transfer_id=self.pk,
                    tx_hash=self.hash,
                )
                return

        # 非内部广播交易，且 EVM tx.from 已确认不是系统地址时，才按外部收款逻辑逐一尝试匹配。
        from deposits.service import DepositService
        from invoices.service import InvoiceService
        from withdrawals.service import WithdrawalService

        (
            InvoiceService.try_match_invoice(self)
            or DepositService.try_create_deposit(self)
            or WithdrawalService.try_match_withdrawal_funding(self)
        )
        self._mark_processed()

        if self.confirm_mode == ConfirmMode.QUICK:
            from .tasks import confirm_transfer

            confirm_transfer.delay(self.pk)

    def _classify_evm_sender(self) -> _EvmSenderClass:
        """三态判定 EVM 原交易发送方，RPC 失败时 fail closed 并等待重试。"""
        try:
            tx = self.chain.w3.eth.get_transaction(self.hash)
            raw_from = None
            if isinstance(tx, dict):
                raw_from = tx.get("from")
            if raw_from is None:
                raw_from = getattr(tx, "from_", None) or getattr(tx, "fromAddress", None)
            if raw_from is None and hasattr(tx, "__getitem__"):
                try:
                    raw_from = tx["from"]
                except (KeyError, TypeError):
                    raw_from = None
            if not raw_from:
                return _EvmSenderClass.UNKNOWN
            from_address = Web3.to_checksum_address(str(raw_from))
        except Exception:
            logger.exception(
                "EVM tx.from 解析失败，本轮按 UNKNOWN 处理",
                transfer_id=self.pk,
                tx_hash=self.hash,
            )
            return _EvmSenderClass.UNKNOWN

        if Address.objects.filter(
            chain_type=ChainType.EVM,
            address=from_address,
        ).exists():
            return _EvmSenderClass.SYSTEM
        return _EvmSenderClass.EXTERNAL

    def _mark_processed(self) -> None:
        self.processed_at = timezone.now()
        OnchainTransfer.objects.filter(pk=self.pk).update(
            processed_at=self.processed_at
        )

    def _match_internal(self, broadcast_task: BroadcastTask) -> bool:
        """通过已解析的 BroadcastTask 直接分发到对应内部业务处理器。"""
        if self.chain.type == ChainType.EVM:
            try:
                from evm.internal_tx.handlers import get_handler

                handler = get_handler(OnchainActionType(broadcast_task.action_type))
            except (KeyError, ValueError):
                logger.warning(
                    "EVM 内部交易缺少 handler 注册",
                    transfer_id=self.pk,
                    action_type=broadcast_task.action_type,
                )
                return False
            return handler.match(self, broadcast_task)

        return self._legacy_match_internal_non_evm(broadcast_task)

    def _legacy_match_internal_non_evm(self, broadcast_task: BroadcastTask) -> bool:
        from deposits.service import DepositService
        from withdrawals.service import WithdrawalService

        tt = broadcast_task.action_type
        if tt == OnchainActionType.GasRecharge:
            return DepositService.try_match_gas_recharge(self, broadcast_task)
        if tt == OnchainActionType.DepositCollection:
            return DepositService.try_match_collection(self, broadcast_task)
        if tt == OnchainActionType.Withdrawal:
            return WithdrawalService.try_match_withdrawal(self, broadcast_task)
        return False

    @db_transaction.atomic
    def confirm(self):
        # 修复：确认前先加行锁并刷新，避免多个 worker 对同一笔转账重复确认和重复计费。
        OnchainTransfer.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.status == TransferStatus.CONFIRMED:
            return

        self.status = TransferStatus.CONFIRMED
        # Transfer 状态推进不依赖 post_save 更新逻辑，直接 update 可减少并发覆盖面。
        OnchainTransfer.objects.filter(pk=self.pk).update(
            status=TransferStatus.CONFIRMED
        )
        # 统一父任务在确认后进入稳定成功终局；业务层不需要感知广播细节。
        BroadcastTask.mark_finalized_success(chain=self.chain, tx_hash=self.hash)

        self._dispatch_business_confirm()

    @db_transaction.atomic
    def drop(self):
        """回退关联业务状态，然后删除 Transfer 记录。

        删除记录以释放唯一约束 (chain, hash, event_id),
        使 reorg 后同一笔 tx 被重新打包时, 扫描器可以自然重建 Transfer。
        """
        # 先加行锁，防止并发处理；已删除的 Transfer 直接跳过。
        if not OnchainTransfer.objects.select_for_update().filter(pk=self.pk).exists():
            return
        self.refresh_from_db()

        self._dispatch_business_drop()

        # 当确认前已观察到的交易后来又查不到时, 按"回退到待上链"处理;
        # 让任务继续通过重广播自愈, 而不是直接进入失败终局。
        BroadcastTask.reset_to_pending_chain(chain=self.chain, tx_hash=self.hash)

        self.delete()

    @property
    def confirm_progress(self):
        has = max(self.chain.latest_block_number - self.block, 1)
        need = self.chain.confirm_block_count

        if need <= 0:
            progress = 100
        else:
            progress = int(min(100.0, (has / need) * 100))

        if self.confirm_mode == ConfirmMode.QUICK:
            return {
                "has_confirmed_count": has,
                "need_confirmed_count": 1,
                "progress": 100,  # 统一返回 0-100 整数
            }

        return {
            "has_confirmed_count": has,
            "need_confirmed_count": need,
            "progress": progress,  # 统一返回 0-100 整数
        }

    def _dispatch_business_confirm(self) -> None:
        """统一按已归类的业务类型分发确认动作，confirm() 专用。"""
        if self.chain.type == ChainType.EVM and self.type:
            try:
                from evm.internal_tx.handlers import get_handler

                handler = get_handler(OnchainActionType(self.type))
            except (KeyError, ValueError):
                handler = None
            if handler is not None:
                handler.confirm(self)
                return

        self._legacy_dispatch_business_confirm()

    def _legacy_dispatch_business_confirm(self) -> None:
        from deposits.service import DepositService
        from invoices.service import InvoiceService
        from withdrawals.service import WithdrawalService

        if self.type == OnchainActionType.Invoice:
            InvoiceService.confirm_invoice(self.invoice)
        elif self.type == OnchainActionType.Deposit:
            DepositService.confirm_deposit(self.deposit)
        elif self.type == OnchainActionType.DepositCollection:
            from deposits.models import DepositCollection

            with contextlib.suppress(DepositCollection.DoesNotExist):
                DepositService.confirm_collection(self.deposit_collection)
        elif self.type == OnchainActionType.GasRecharge:
            from deposits.models import GasRecharge

            GasRecharge.objects.filter(transfer=self).update(
                recharged_at=timezone.now()
            )
        elif self.type == OnchainActionType.Withdrawal:
            from withdrawals.models import Withdrawal

            with contextlib.suppress(Withdrawal.DoesNotExist):
                WithdrawalService.confirm_withdrawal(self)

    def _dispatch_business_drop(self) -> None:
        """统一按已归类的业务类型分发回退动作，drop() 专用。"""
        if self.chain.type == ChainType.EVM and self.type:
            try:
                from evm.internal_tx.handlers import get_handler

                handler = get_handler(OnchainActionType(self.type))
            except (KeyError, ValueError):
                handler = None
            if handler is not None:
                handler.drop(self)
                return

        self._legacy_dispatch_business_drop()

    def _legacy_dispatch_business_drop(self) -> None:
        from deposits.service import DepositService
        from invoices.service import InvoiceService
        from withdrawals.service import WithdrawalService

        if self.type == OnchainActionType.Invoice:
            InvoiceService.drop_invoice(self.invoice)
        elif self.type == OnchainActionType.Deposit:
            DepositService.drop_deposit(self.deposit)
        elif self.type == OnchainActionType.DepositCollection:
            from deposits.models import DepositCollection

            with contextlib.suppress(DepositCollection.DoesNotExist):
                DepositService.drop_collection(self.deposit_collection)
        elif self.type == OnchainActionType.Withdrawal:
            from withdrawals.models import Withdrawal

            with contextlib.suppress(Withdrawal.DoesNotExist):
                WithdrawalService.drop_withdrawal(self)
