from __future__ import annotations

import contextlib
import enum
from decimal import Decimal
from functools import cached_property
from typing import TYPE_CHECKING

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

from chains.constants import CHAIN_SPECS
from chains.constants import ChainCode
from chains.constants import ChainSpec
from chains.constants import ChainType  # noqa: F401  re-export 给下游模块过渡使用
from common.fields import AddressField
from common.fields import HashField
from common.models import UndeletableModel

if TYPE_CHECKING:
    from currencies.models import Crypto
    from deposits.models import Deposit
    from invoices.models import Invoice
    from withdrawals.models import Withdrawal

env = environ.Env()
logger = structlog.get_logger()


class Chain(models.Model):
    code = models.CharField(
        _("代码"),
        choices=ChainCode,
        unique=True,
    )
    type = models.CharField(
        _("类型"),
        max_length=16,
        editable=False,
        help_text=_("由 code 常量自动决定，不可手动修改。"),
    )
    latest_block_number = models.PositiveIntegerField(
        default=0, verbose_name=_("最新区块")
    )
    active = models.BooleanField(default=False, verbose_name=_("启用"))
    evm_log_max_block_range = models.PositiveIntegerField(
        _("EVM 单次日志请求最大区块数"),
        default=10,
        help_text=_("EVM 扫描器单次 eth_getLogs 请求允许覆盖的最大区块数。"),
    )
    rpc = models.CharField(_("RPC"), blank=True, default="")
    tron_api_key = models.CharField(_("Tron API Key"), blank=True, default="")
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        verbose_name = _("链")
        verbose_name_plural = _("链")

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # type 是链固有属性的反规范化冗余，由 code 常量自动推导，
        # 不让业务层手动设置，避免脏数据。
        if self.code and self.code in CHAIN_SPECS:
            self.type = CHAIN_SPECS[self.code].type
        self.full_clean()
        with db_transaction.atomic():
            result = super().save(*args, **kwargs)
            self._sync_tron_usdt_watch_cursor()
        return result

    @property
    def spec(self) -> ChainSpec:
        return CHAIN_SPECS[self.code]

    @property
    def name(self) -> str:
        return ChainCode(self.code).label

    @property
    def chain_id(self) -> int | None:
        return self.spec.chain_id

    @property
    def is_poa(self) -> bool | None:
        return self.spec.is_poa

    @property
    def confirm_block_count(self) -> int:
        return self.spec.confirm_block_count

    @cached_property
    def native_coin(self):
        from currencies.models import Crypto  # noqa: PLC0415

        # Crypto.name 与 Crypto.coingecko_id 均为 unique 字段；首次创建必须填充，
        # 否则多链原生币会因为留空互相冲突。symbol 在 ChainSpec 中即天然唯一，
        # 直接拿来兜底是最稳妥的做法，业务层后续 admin 仍可改写。
        symbol = self.spec.native_coin_symbol
        crypto, _ = Crypto.objects.get_or_create(
            symbol=symbol,
            defaults={
                "name": symbol,
                "coingecko_id": symbol.lower(),
                "decimals": self.spec.native_coin_decimals,
                "active": True,
            },
        )
        return crypto

    def clean(self) -> None:
        """收紧合法 RPC：仅当 EVM 链配置了 RPC 时，校验远端 chain_id 与常量一致。

        把过去 `_detect_chain_id` 从 RPC 写入字段的逻辑反过来用 ——
        以常量为单一事实源，校验运维填入的 RPC 是否真的对应所选链。
        Tron 无 chain_id 概念，跳过此校验；空 RPC 允许占位创建，配置完整时再校验。
        """
        super().clean()
        # full_clean 会先收集 clean_fields 的错误再调用本方法，所以这里可能拿到非法 code。
        # 让 clean_fields 的 choices 校验报错即可，本方法直接放行。
        if self.code not in CHAIN_SPECS:
            return
        # type 的权威来源是 CHAIN_SPECS，不能依赖 DB 字段（full_clean 时 type 可能尚未设置）。
        if self.spec.type != ChainType.EVM or not self.rpc:
            return
        try:
            w3 = Web3(Web3.HTTPProvider(self.rpc, request_kwargs={"timeout": 8}))
            actual_chain_id = w3.eth.chain_id
        except Exception as exc:
            raise ValidationError(
                {"rpc": _("RPC 连接失败：%(err)s") % {"err": exc}}
            ) from exc
        if actual_chain_id != self.chain_id:
            raise ValidationError(
                {
                    "rpc": _(
                        "RPC chain_id 与所选链不匹配：期望 %(expected)s，实际 %(actual)s"
                    )
                    % {"expected": self.chain_id, "actual": actual_chain_id}
                }
            )

    def _sync_tron_usdt_watch_cursor(self) -> None:
        """活跃 Tron 链应在配置层持有 USDT 扫描游标，避免依赖首次 beat 扫描显式创建。"""
        if self.type != ChainType.TRON or not self.active:
            return

        from tron.models import TronWatchCursor  # noqa: PLC0415

        from currencies.models import ChainToken  # noqa: PLC0415

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
            defaults={"last_scanned_block": 0, "enabled": True},
        )

    def content(self):
        return {
            "name": self.name,
            "code": self.code,
            "type": self.type,
            "chain_id": self.chain_id,
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
    ):
        """读取区块时遇到 POA extraData 校验错误，用 force_poa 重建 w3 重试一次。

        常量层已把 BSC/Polygon 标 POA，正常路径不会触发兜底；
        若新接入链未及时打 POA 标记，这里仅做即时降级，不再回写 DB。
        """
        try:
            return self.w3.eth.get_block(
                block_identifier, full_transactions=full_transactions
            )
        except ExtraDataLengthError:
            retry_w3 = self._build_w3(force_poa=True)
            self.__dict__["w3"] = retry_w3
            return retry_w3.eth.get_block(
                block_identifier, full_transactions=full_transactions
            )

    @property
    def adapter(self) -> "AdapterInterface":  # noqa: F821
        from chains.adapters import AdapterFactory  # noqa: PLC0415

        return AdapterFactory.get_adapter(chain_type=self.type)

    @property
    def get_latest_block_number(self) -> int:
        if self.type == ChainType.EVM:
            return self.w3.eth.block_number
        if self.type == ChainType.TRON:
            return self.latest_block_number
        msg = f"Unsupported chain type: {self.type}"
        raise NotImplementedError(msg)


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
class AddressUsage(models.TextChoices):
    HotWallet = "hot_wallet", _("热钱包")


BIP44_ACCOUNT_MAP: dict[str, int] = {
    AddressUsage.HotWallet: 0,
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
    address = AddressField(_("地址"), unique=True)
    chain_type = models.CharField(choices=ChainType, verbose_name=_("链类型"))
    usage = models.CharField(_("用途"), choices=AddressUsage)
    # BIP44 account' 层级，由 usage 决定（冗余存储用于查询优化和数据校验）。
    bip44_account = models.PositiveIntegerField(_("BIP44 账户层级"))
    # BIP44 address_index 层级，该用途下的地址序号。
    address_index = models.PositiveIntegerField(_("地址索引"), default=0)

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
        tx_type: TxTaskType,
    ) -> str:
        """使用本账户私钥签名并发送转账，返回 tx hash / signature。

        EVM：仅创建内部任务，首次广播时才生成首个 tx_hash。
        """
        if chain.type == ChainType.EVM:
            # EvmTxTask 内部管理锁，不在此处获取，避免双重加锁。
            from evm.intents import build_erc20_transfer_intent  # noqa: PLC0415
            from evm.intents import build_native_transfer_intent  # noqa: PLC0415
            from evm.models import EvmTxTask  # noqa: PLC0415

            decimals = crypto.get_decimals(chain)
            value_raw = int(amount * Decimal(10**decimals))
            if crypto == chain.native_coin:
                intent = build_native_transfer_intent(
                    address=self,
                    chain=chain,
                    to=to,
                    value=value_raw,
                    tx_type=tx_type,
                )
            else:
                intent = build_erc20_transfer_intent(
                    address=self,
                    chain=chain,
                    crypto=crypto,
                    to=to,
                    value_raw=value_raw,
                    tx_type=tx_type,
                )
            task = EvmTxTask.schedule(intent)
            return task.base_task.tx_hash

        msg = f"Unsupported chain type for send_crypto: {chain.type}"
        raise NotImplementedError(msg)


class TxTaskType(models.TextChoices):
    """TxTask.tx_type 的枚举：仅描述系统内部主动发起的链上交易。"""

    Withdrawal = "withdrawal", "🏧 提币"
    VaultSlotDeploy = "vault_slot_deploy", "🏦 VaultSlot 部署"
    VaultSlotCollect = "vault_slot_collect", "💰 VaultSlot 归集"


class TransferType(models.TextChoices):
    """Transfer.type 的枚举：仅描述对一笔链上转账的业务归属。"""

    Invoice = "invoice", _("💳 支付")
    Deposit = "deposit", "💰 充币"
    Withdrawal = "withdrawal", "🏧 提币"
    Collect = "collect", "💰 归集"


class TxTaskStage(models.TextChoices):
    QUEUED = "queued", _("待广播")
    PENDING_CHAIN = "pending_chain", _("待上链")
    PENDING_CONFIRM = "pending_confirm", _("确认中")
    FINALIZED = "finalized", _("已完结")


class TxHash(models.Model):
    tx_task = models.ForeignKey(
        "TxTask",
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
        ordering = ("tx_task_id", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "hash"),
                name="uniq_tx_hash_chain_hash",
            ),
            models.UniqueConstraint(
                fields=("tx_task", "version"),
                name="uniq_tx_hash_task_version",
            ),
        ]
        verbose_name = _("交易哈希历史")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return self.hash

    def clean(self) -> None:
        super().clean()
        if self.tx_task_id and self.chain_id != self.tx_task.chain_id:
            raise ValidationError(
                {"chain": _("TxHash.chain 必须与 TxTask.chain 保持一致。")}
            )


class TxTask(UndeletableModel):
    """跨链统一的链上任务锚点。

    设计原则：
    - stage 只描述当前所处阶段：待广播 / 待上链 / 确认中 / 已完结。
    - success 只描述终局是否成功：None 表示未知，True 表示成功，False 表示失败。
    - 广播重试等实现细节继续留在各链子表，避免把"是否广播"污染到统一领域模型。
    - Withdrawal 等业务对象统一外键到该模型，不再直接依赖具体链实现或 tx hash。
    """

    address = models.ForeignKey(
        "Address",
        on_delete=models.PROTECT,
        verbose_name=_("地址"),
    )
    chain = models.ForeignKey(
        "Chain",
        on_delete=models.PROTECT,
        verbose_name=_("链"),
    )
    tx_type = models.CharField(
        _("类型"),
        choices=TxTaskType,
    )
    tx_hash = HashField(
        unique=False,
        verbose_name=_("交易哈希"),
        blank=True,
        null=True,
    )
    stage = models.CharField(
        _("阶段"),
        choices=TxTaskStage,
        default=TxTaskStage.QUEUED,
    )
    success = models.BooleanField(
        _("是否成功"),
        null=True,
        blank=True,
        default=None,
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "tx_hash"),
                name="uniq_tx_task_chain_hash",
            ),
            models.CheckConstraint(
                # 只要出现成功/失败终局结果，就必须已经进入已完结阶段。
                condition=models.Q(success__isnull=True)
                | models.Q(stage=TxTaskStage.FINALIZED),
                name="ck_tx_task_success_requires_finalized_stage",
            ),
            models.CheckConstraint(
                # 已完结任务不能继续保留未知结果，否则会把阶段和结果语义混在一起。
                condition=~models.Q(stage=TxTaskStage.FINALIZED)
                | models.Q(success__isnull=False),
                name="ck_tx_task_finalized_requires_success_value",
            ),
        ]
        verbose_name = _("链上任务")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.tx_hash or f"tx-task-{self.pk or 'unsaved'}"

    def clean(self) -> None:
        """在模型层显式约束阶段、成功标记二者的一致性。"""
        super().clean()
        errors = {}

        if self.success is not None:
            if self.stage != TxTaskStage.FINALIZED:
                errors["stage"] = _("成功/失败结果只能出现在已完结阶段。")

        if self.stage == TxTaskStage.FINALIZED and self.success is None:
            errors["success"] = _("已完结任务必须给出成功或失败结果。")

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # TxTask 是跨链主锚点，统一在保存前执行 full_clean，避免后台和脚本写入脏状态。
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def display_status(self) -> str:
        """把阶段与结果合成为一个稳定的人类可读状态。"""
        if self.stage != TxTaskStage.FINALIZED:
            return self.get_stage_display()
        return "成功" if self.success else "失败"

    @property
    def is_confirmed(self) -> bool:
        return self.stage == TxTaskStage.FINALIZED and self.success is True

    @db_transaction.atomic
    def append_tx_hash(self, tx_hash: str) -> TxHash:
        locked_task = TxTask.objects.select_for_update().get(pk=self.pk)
        # 并发广播可能产生相同 tx_hash（相同 nonce + gas_price 签名结果相同），
        # 若已存在则视为幂等，直接返回。
        existing = TxHash.objects.filter(chain=locked_task.chain, hash=tx_hash).first()
        if existing:
            self.tx_hash = tx_hash
            return existing
        max_version = (
            TxHash.objects.filter(tx_task=locked_task)
            .aggregate(max_version=models.Max("version"))
            .get("max_version")
        )
        next_version = 1 if max_version is None else int(max_version) + 1
        created = TxHash.objects.create(
            tx_task=locked_task,
            chain=locked_task.chain,
            hash=tx_hash,
            version=next_version,
        )
        TxTask.objects.filter(pk=locked_task.pk).update(
            tx_hash=tx_hash,
            updated_at=timezone.now(),
        )
        if locked_task.tx_type == TxTaskType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(tx_task=locked_task).update(
                hash=tx_hash,
                updated_at=timezone.now(),
            )
        self.tx_hash = tx_hash
        return created

    @staticmethod
    def resolve_by_hash(*, chain: Chain, tx_hash: str) -> TxTask | None:
        """通过 tx_hash 查找对应的 TxTask。

        优先从 TxHash 历史记录匹配（覆盖 gas 重签后的旧 hash），
        未命中时回退到 TxTask.tx_hash（当前 hash）。
        两张表均有 (chain, hash) 唯一约束，无需额外去重。
        """
        if not tx_hash:
            return None
        # 优先查历史记录（gas 重签后旧 hash 只存在于 TxHash 表）
        history = (
            TxHash.objects.select_related("tx_task")
            .filter(chain=chain, hash=tx_hash)
            .first()
        )
        if history is not None:
            return history.tx_task
        # 回退到当前 tx_hash
        return TxTask.objects.filter(chain=chain, tx_hash=tx_hash).first()

    @staticmethod
    def mark_finalized_success(*, chain: Chain, tx_hash: str) -> int:
        """将匹配的任务标记为成功终局。

        使用 .update() 绕过 save()/full_clean() 以避免逐行加载，
        依赖 DB CheckConstraint 保证状态三元组一致性。
        """
        if not tx_hash:
            return 0
        task = TxTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return 0
        updated = TxTask.objects.filter(pk=task.pk, success__isnull=True).update(
            tx_hash=tx_hash,
            stage=TxTaskStage.FINALIZED,
            success=True,
            updated_at=timezone.now(),
        )
        if updated and task.tx_type == TxTaskType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(tx_task=task).update(
                hash=tx_hash,
                updated_at=timezone.now(),
            )
        return updated

    @staticmethod
    def mark_finalized_failed(
        *,
        task_id: int,
        expected_stage: TxTaskStage | None = None,
    ) -> int:
        """将匹配的任务标记为失败终局。"""
        queryset = TxTask.objects.filter(
            pk=task_id,
            success__isnull=True,
        )
        if expected_stage is not None:
            queryset = queryset.filter(stage=expected_stage)
        return queryset.update(
            stage=TxTaskStage.FINALIZED,
            success=False,
            updated_at=timezone.now(),
        )

    @staticmethod
    def reset_to_pending_chain(*, chain: Chain, tx_hash: str) -> int:
        """将匹配的任务回退到待上链阶段（用于 Transfer drop / reorg 恢复）。

        使用 .update() 绕过 save()/full_clean() 以避免逐行加载，
        依赖 DB CheckConstraint 保证状态三元组一致性。
        """
        task = TxTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return 0
        updated = TxTask.objects.filter(
            pk=task.pk,
            stage=TxTaskStage.PENDING_CONFIRM,
            success__isnull=True,
        ).update(
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
            updated_at=timezone.now(),
        )
        if updated and task.tx_type == TxTaskType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(tx_task=task).update(
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
        task = TxTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return 0
        updated = (
            TxTask.objects.filter(pk=task.pk)
            .exclude(stage=TxTaskStage.FINALIZED)
            .update(
                tx_hash=tx_hash,
                stage=TxTaskStage.PENDING_CONFIRM,
                success=None,
                updated_at=timezone.now(),
            )
        )
        if updated and task.tx_type == TxTaskType.Withdrawal:
            from withdrawals.models import Withdrawal

            Withdrawal.objects.filter(tx_task=task).update(
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


class Transfer(models.Model):
    if TYPE_CHECKING:
        # Django 反向 OneToOne 描述符在运行时动态挂载；这里显式声明给 IDE 做静态解析。
        invoice: Invoice
        deposit: Deposit
        withdrawal: Withdrawal

    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    block = models.IntegerField(_("区块高度"))
    block_hash = HashField(
        verbose_name=_("区块哈希"),
        unique=False,
    )
    # 修复：真实链上 tx hash 与"同 tx 内事件明细"拆分建模，避免继续依赖 `hash:logIndex` 字符串协议。
    hash = HashField(unique=False, verbose_name=_("哈希"))
    # EVM 专用
    event_id = models.CharField(
        _("事件标识"),
        max_length=32,
        blank=True,
        default="",
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
        choices=TransferType,
        blank=True,
        default="",
    )
    confirm_mode = models.CharField(
        choices=ConfirmMode,
        default=ConfirmMode.FULL,
        max_length=8,
        verbose_name=_("确认模式"),
        help_text=_(
            "当前仅 Invoice 业务根据 fast_confirm_threshold 动态设置 QUICK/FULL；Deposit 与 Withdrawal 始终使用默认 FULL，走完整区块确认流程。"
        ),
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
        Transfer.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.processed_at:
            return

        # 优先通过 TxHash 匹配内部交易任务（提币等），
        # 一次 resolve 即可定位业务类型，避免逐一 try_match 重复查询。
        tx_task = TxTask.resolve_by_hash(chain=self.chain, tx_hash=self.hash)
        if tx_task is not None:
            if self._match_internal(tx_task):
                self._mark_processed()
                if self.confirm_mode == ConfirmMode.QUICK:
                    from .tasks import confirm_transfer

                    confirm_transfer.delay(self.pk)
                return

            logger.warning(
                "Transfer 命中 TxTask 但内部 handler 未认领，跳过外部匹配",
                transfer_id=self.pk,
                tx_task_id=tx_task.pk,
                tx_hash=self.hash,
            )
            self._mark_processed()
            return

        if self.chain.type == ChainType.EVM:
            sender_class = self._classify_evm_sender()
            if sender_class == _EvmSenderClass.SYSTEM:
                logger.warning(
                    "EVM Transfer 原交易发送方是系统地址但无 TxTask，跳过外部匹配",
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

        (
            InvoiceService.try_match_invoice(self)
            or DepositService.try_create_deposit(self)
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
                raw_from = getattr(tx, "from_", None) or getattr(
                    tx, "fromAddress", None
                )
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
        Transfer.objects.filter(pk=self.pk).update(processed_at=self.processed_at)

    def _match_internal(self, tx_task: TxTask) -> bool:
        """通过已解析的 TxTask 直接分发到对应内部业务处理器。"""
        if self.chain.type == ChainType.EVM:
            try:
                from evm.internal_tx.routing import get_handler

                handler = get_handler(TxTaskType(tx_task.tx_type))
            except (KeyError, ValueError):
                logger.warning(
                    "EVM 内部交易缺少 handler 注册",
                    transfer_id=self.pk,
                    tx_type=tx_task.tx_type,
                )
                return False
            return handler.match(self, tx_task)

        return self._legacy_match_internal_non_evm(tx_task)

    def _legacy_match_internal_non_evm(self, tx_task: TxTask) -> bool:
        from withdrawals.service import WithdrawalService

        tt = tx_task.tx_type
        if tt == TxTaskType.Withdrawal:
            return WithdrawalService.try_match_withdrawal(self, tx_task)
        return False

    @db_transaction.atomic
    def confirm(self):
        # 修复：确认前先加行锁并刷新，避免多个 worker 对同一笔转账重复确认和重复计费。
        Transfer.objects.select_for_update().get(pk=self.pk)
        self.refresh_from_db()
        if self.status == TransferStatus.CONFIRMED:
            return

        self.status = TransferStatus.CONFIRMED
        # Transfer 状态推进不依赖 post_save 更新逻辑，直接 update 可减少并发覆盖面。
        Transfer.objects.filter(pk=self.pk).update(status=TransferStatus.CONFIRMED)
        # 统一父任务在确认后进入稳定成功终局；业务层不需要感知广播细节。
        TxTask.mark_finalized_success(chain=self.chain, tx_hash=self.hash)

        self._dispatch_business_confirm()

    @db_transaction.atomic
    def drop(self):
        """回退关联业务状态，然后删除 Transfer 记录。

        删除记录以释放唯一约束 (chain, hash, event_id),
        使 reorg 后同一笔 tx 被重新打包时, 扫描器可以自然重建 Transfer。
        """
        # 先加行锁，防止并发处理；已删除的 Transfer 直接跳过。
        if not Transfer.objects.select_for_update().filter(pk=self.pk).exists():
            return
        self.refresh_from_db()

        self._dispatch_business_drop()

        # 当确认前已观察到的交易后来又查不到时, 按"回退到待上链"处理;
        # 让任务继续通过重广播自愈, 而不是直接进入失败终局。
        TxTask.reset_to_pending_chain(chain=self.chain, tx_hash=self.hash)

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
        from deposits.service import DepositService
        from invoices.service import InvoiceService

        if self.type == TransferType.Invoice:
            InvoiceService.confirm_invoice(self.invoice)
        elif self.type == TransferType.Deposit:
            DepositService.confirm_deposit(self.deposit)
        elif self.type == TransferType.Withdrawal:
            self._dispatch_withdrawal_confirm()
        elif self.type in {TransferType.Collect}:
            return

    def _dispatch_business_drop(self) -> None:
        """统一按已归类的业务类型分发回退动作，drop() 专用。"""
        from deposits.service import DepositService
        from invoices.service import InvoiceService

        if self.type == TransferType.Invoice:
            InvoiceService.drop_invoice(self.invoice)
        elif self.type == TransferType.Deposit:
            DepositService.drop_deposit(self.deposit)
        elif self.type == TransferType.Withdrawal:
            self._dispatch_withdrawal_drop()
        elif self.type in {TransferType.Collect}:
            return

    def _dispatch_withdrawal_confirm(self) -> None:
        if self.chain.type == ChainType.EVM:
            from evm.internal_tx.routing import get_handler

            with contextlib.suppress(KeyError):
                get_handler(TxTaskType.Withdrawal).confirm(self)
                return

        from withdrawals.models import Withdrawal
        from withdrawals.service import WithdrawalService

        with contextlib.suppress(Withdrawal.DoesNotExist):
            WithdrawalService.confirm_withdrawal(self)

    def _dispatch_withdrawal_drop(self) -> None:
        if self.chain.type == ChainType.EVM:
            from evm.internal_tx.routing import get_handler

            with contextlib.suppress(KeyError):
                get_handler(TxTaskType.Withdrawal).drop(self)
                return

        from withdrawals.models import Withdrawal
        from withdrawals.service import WithdrawalService

        with contextlib.suppress(Withdrawal.DoesNotExist):
            WithdrawalService.drop_withdrawal(self)
