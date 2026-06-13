from __future__ import annotations

from datetime import timedelta
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
from eth_utils import keccak  # noqa
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from chains.constants import CHAIN_SPECS
from chains.constants import NATIVE_COIN_COINGECKO_IDS
from chains.constants import TRON_MAINNET_BASE_URL
from chains.constants import TRON_TESTNET_BASE_URL
from chains.constants import ChainCode
from chains.constants import ChainSpec
from chains.constants import ChainType  # noqa: F401  re-export 给下游模块过渡使用
from common.fields import AddressField
from common.fields import HashField
from common.models import UndeletableModel

if TYPE_CHECKING:
    from chains.keys import EvmSignedPayload
    from chains.keys import TronSignedPayload
    from deposits.models import Deposit
    from invoices.models import Invoice

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
        choices=ChainType,
        max_length=16,
        editable=False,
        help_text=_("由 code 常量自动决定，不可手动修改。"),
    )
    is_testnet = models.BooleanField(
        _("测试网"),
        default=False,
        editable=False,
        help_text=_("由 code 常量自动决定，不可手动修改。"),
    )
    latest_block_number = models.PositiveIntegerField(
        default=0, verbose_name=_("最新区块")
    )
    sort_order = models.PositiveIntegerField(default=0, verbose_name=_("排序序号"))
    active = models.BooleanField(default=False, verbose_name=_("启用"))

    # For EVM
    evm_log_max_block_range = models.PositiveIntegerField(
        _("EVM 单次日志请求最大区块数"),
        default=10,
        help_text=_("EVM 扫描器单次 eth_getLogs 请求允许覆盖的最大区块数。"),
    )
    rpc = models.CharField(_("RPC"), blank=True, default="")

    # For Tron
    tron_api_key = models.CharField(_("Tron API Key"), blank=True, default="")

    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    # 该链上一次扫描任务完成的时间。调度器据此与 scan_interval_seconds 对比，
    # 判断本链是否到期需要再次扫描。auto_now_add 让新建链以创建时刻为起点，
    # 经过一个扫描周期后自然进入首扫；后续由 mark_scanned() 显式推进。
    last_scanned_at = models.DateTimeField(_("最近扫描时间"), auto_now_add=True)

    class Meta:
        verbose_name = _("链")
        verbose_name_plural = _("链")
        constraints = [
            models.CheckConstraint(
                name="chain_active_requires_runtime_config",
                condition=(
                    models.Q(active=False)
                    | (models.Q(type=ChainType.EVM) & ~models.Q(rpc=""))
                    | (models.Q(type=ChainType.TRON) & ~models.Q(tron_api_key=""))
                ),
            ),
        ]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # type / is_testnet 是链固有属性的反规范化冗余，由 code 常量自动推导，
        # 不让业务层手动设置，避免脏数据。
        if self.code and self.code in CHAIN_SPECS:
            spec = CHAIN_SPECS[self.code]
            self.type = spec.type
            self.is_testnet = spec.is_testnet
            update_fields = kwargs.get("update_fields")
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"type", "is_testnet"}
        self.full_clean()
        with db_transaction.atomic():
            result = super().save(*args, **kwargs)
            self._sync_tron_scan_cursor()
        return result

    @property
    def spec(self) -> ChainSpec:
        return CHAIN_SPECS[self.code]

    @property
    def name(self) -> str:
        return ChainCode(self.code).label

    @property
    def icon(self) -> str:
        return self.spec.icon

    @property
    def chain_id(self) -> int | None:
        return self.spec.chain_id

    @property
    def is_poa(self) -> bool | None:
        return self.spec.is_poa

    @property
    def tron_base_url(self) -> str:
        """Tron HTTP 网关地址：测试网走 Nile、否则主网。仅 Tron 链有意义。

        按 is_testnet 动态选址，不落库成字段——全网就这两套端点，无需运维逐链配置。
        """
        return TRON_TESTNET_BASE_URL if self.is_testnet else TRON_MAINNET_BASE_URL

    @property
    def confirm_block_count(self) -> int:
        return self.spec.confirm_block_count

    @property
    def scan_interval_seconds(self) -> int:
        """本链两次扫描之间的最小间隔（秒），由链常量决定。"""
        return self.spec.scan_interval_seconds

    @property
    def is_due_for_scan(self) -> bool:
        """距上次扫描完成是否已达本链扫描周期，到期则需再次调度扫描。"""
        if self.last_scanned_at is None:
            return True
        elapsed = (timezone.now() - self.last_scanned_at).total_seconds()
        return elapsed >= self.scan_interval_seconds

    def mark_scanned(self) -> None:
        """扫描任务完成后推进 last_scanned_at。

        用 update 直接落库，绕过 save()/full_clean()，既避免逐行校验开销，
        也能改写 auto_now_add（创建后默认不可写）字段。
        """
        now = timezone.now()
        Chain.objects.filter(pk=self.pk).update(last_scanned_at=now)
        self.last_scanned_at = now

    @cached_property
    def native_coin(self):
        from currencies.models import Crypto  # noqa: PLC0415

        # Crypto.name 为 unique 字段，首次创建用 symbol 兜底（ChainSpec 中天然唯一）。
        # coingecko_id 必须是 CoinGecko 真实 slug：原生币自动建成 active=True，立刻进入
        # 币价依赖链路，回填 symbol.lower() 会被行情任务当真实 id 发出后静默拉空价格、
        # 留空又会 KeyError 卡死换算。故从 NATIVE_COIN_COINGECKO_IDS 取权威 slug
        # （该映射有覆盖性断言，漏配会在导入期报错）。
        symbol = self.spec.native_coin_symbol
        crypto, _ = Crypto.objects.get_or_create(
            symbol=symbol,
            defaults={
                "name": symbol,
                "coingecko_id": NATIVE_COIN_COINGECKO_IDS[symbol],
                "active": True,
                # 原生币精度落到 CryptoOnChain（见 ensure_native_crypto_mapping_for_chain）；
                # 这里仅在 Crypto 上标记原生币身份。
                "is_native": True,
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
        if self.spec.type != ChainType.EVM or not self.active or not self.rpc:
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

    def _sync_tron_scan_cursor(self) -> None:
        """活跃 Tron 链在配置层即持有按链唯一的扫描游标，避免依赖首次 beat 扫描显式创建。

        游标只锚定区块进度、与具体 TRC20 合约解耦：扫描器每轮按本链全量
        CryptoOnChain 逐块拉取，新增/下架代币不影响游标，故这里无需
        再依赖 USDT 是否已配置。
        """
        if self.type != ChainType.TRON or not self.active:
            return

        from tron.models import TronWatchCursor  # noqa: PLC0415

        TronWatchCursor.objects.get_or_create(
            chain=self,
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

    @property
    def adapter(self) -> AdapterInterface:  # noqa: F821
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


# BIP44 account' 层级与业务用途的映射，直接用于派生路径 m/44'/coin'/account'/0/address_index。
class AddressUsage(models.TextChoices):
    HotWallet = "hot_wallet", _("热钱包")


BIP44_ACCOUNT_MAP: dict[str, int] = {
    AddressUsage.HotWallet: 0,
}


class Wallet(UndeletableModel):
    # 每个钱包持有一份独立助记词（AES-256-GCM 加密入库，见 chains/keys.py）。
    # 一份助记词可派生该钱包在所有链、所有用途下的地址，故本字段链中立。
    # editable=False：助记词只在 generate() 时写入，禁止后台手动改写。
    encrypted_mnemonic = models.TextField(_("加密助记词"), editable=False)

    class Meta:
        verbose_name = _("钱包")
        verbose_name_plural = _("钱包")

    def __str__(self):
        wallet_identifier = self.pk if self.pk is not None else "unsaved"
        return f"Wallet-{wallet_identifier}"

    @classmethod
    def generate(cls) -> Wallet:
        """生成新钱包：在主系统内部创建助记词、加密后落库。

        密钥材料不出主系统：助记词密文存于本表，私钥仅在派生/签名时由 keys.py 在内存中重建，
        绝不写日志、不在系统外传输。
        """
        from chains.keys import encrypt_mnemonic
        from chains.keys import generate_mnemonic

        # 助记词明文只在内存中短暂存在，加密后立即落库。
        encrypted_mnemonic = encrypt_mnemonic(generate_mnemonic())
        return cls.objects.create(encrypted_mnemonic=encrypted_mnemonic)

    def decrypt_mnemonic(self) -> str:
        """解密本钱包助记词。仅供进程内派生/签名调用，结果绝不写日志、不出系统。"""
        from chains.keys import decrypt_mnemonic

        return decrypt_mnemonic(self.encrypted_mnemonic)

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

        from chains.keys import derive_evm_address
        from chains.keys import derive_tron_address

        bip44_account = self.get_bip44_account(usage)

        if chain_type == ChainType.EVM:
            expected_address = derive_evm_address(
                mnemonic=self.decrypt_mnemonic(),
                bip44_account=bip44_account,
                address_index=address_index,
            )
        elif chain_type == ChainType.TRON:
            expected_address = derive_tron_address(
                mnemonic=self.decrypt_mnemonic(),
                bip44_account=bip44_account,
                address_index=address_index,
            )
        else:
            raise NotImplementedError(f"unsupported chain_type={chain_type}")

        created = False  # noqa
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
            addr_obj.bip44_account != bip44_account  # noqa
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

    @classmethod
    def acquire_for_update(cls, *, address: Address) -> Address:
        """锁定地址本行，用于串行化该地址发起的链上交易。"""
        return cls.objects.select_for_update().get(pk=address.pk)

    def sign_evm_transaction(self, *, tx_dict: dict) -> EvmSignedPayload:
        """用本地址私钥对一笔 legacy EVM 交易签名，返回归一化签名结果。

        私钥由本地址所属钱包助记词按其 HD 路径在内存中临时重建，绝不写日志、不出系统。
        仅 EVM 链地址可走此方法；非 EVM 链由各自实现负责签名。
        """
        from chains.keys import derive_evm_private_key
        from chains.keys import sign_evm_transaction

        if self.chain_type != ChainType.EVM:
            raise NotImplementedError(
                f"sign_evm_transaction 不支持 chain_type={self.chain_type}"
            )

        private_key = derive_evm_private_key(
            mnemonic=self.wallet.decrypt_mnemonic(),
            bip44_account=self.bip44_account,
            address_index=self.address_index,
        )
        return sign_evm_transaction(private_key=private_key, tx_dict=tx_dict)

    def sign_tron_transaction(self, *, unsigned_transaction: dict) -> TronSignedPayload:
        """用本地址私钥对 TronGrid unsigned transaction 签名。

        私钥只在内存中按 m/44'/195'/{account}'/0/index 临时派生；调用方拿到的是
        可广播 payload 与 txID，不接触也不记录私钥材料。
        """
        from chains.keys import derive_tron_private_key
        from chains.keys import sign_tron_transaction

        if self.chain_type != ChainType.TRON:
            raise NotImplementedError(
                f"sign_tron_transaction 不支持 chain_type={self.chain_type}"
            )

        private_key = derive_tron_private_key(
            mnemonic=self.wallet.decrypt_mnemonic(),
            bip44_account=self.bip44_account,
            address_index=self.address_index,
        )
        return sign_tron_transaction(
            private_key=private_key,
            unsigned_transaction=unsigned_transaction,
        )


class TxTaskType(models.TextChoices):
    """TxTask.tx_type 的枚举：仅描述系统内部主动发起的链上交易。"""

    VaultSlotDeploy = "vault_slot_deploy", "🏦 VaultSlot 部署"
    VaultSlotCollect = "vault_slot_collect", "💰 VaultSlot 归集"


class TransferType(models.TextChoices):
    """Transfer.type 的枚举：仅描述对一笔链上转账的业务归属。"""

    Unmatched = "unmatched", _("未归类")
    Invoice = "invoice", _("💳 账单收款")
    Deposit = "deposit", _("💰 充值收款")


class TxTaskStatus(models.TextChoices):
    QUEUED = "queued", _("待提交")
    SUBMITTED = "submitted", _("已提交")
    SUCCEEDED = "succeeded", _("成功")
    FAILED = "failed", _("失败")


# 终局状态集合：链上交易已得出确定执行结果（成功或永久失败），不再推进。
# 区块链上链周期是固定的线性流程 + 末端分叉，因此用单枚举表达，
# 终局态用该集合判定，避免再维护 stage/success 两字段的跨字段不变式。
TERMINAL_TX_TASK_STATUSES = frozenset({TxTaskStatus.SUCCEEDED, TxTaskStatus.FAILED})


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
    expires_at_ms = models.PositiveBigIntegerField(
        _("过期时间(ms)"),
        null=True,
        blank=True,
    )
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
    - status 用单枚举描述上链生命周期：待提交 → 已提交 →（成功 | 失败）。
      上链周期是固定的线性流程加末端成功/失败分叉，故无需把"阶段"与"结果"
      拆成两个字段再用跨字段约束维持一致；终局态由 TERMINAL_TX_TASK_STATUSES 判定。
    - 广播重试等实现细节继续留在各链子表，避免把"是否广播"污染到统一领域模型。
    - VaultSlotCollect 等业务对象统一外键到该模型，不再直接依赖具体链实现或 tx hash。
    """

    sender = models.ForeignKey(
        "Address",
        on_delete=models.PROTECT,
        verbose_name=_("发送地址"),
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
        null=True,
        blank=True,
    )
    status = models.CharField(
        _("状态"),
        choices=TxTaskStatus,
        default=TxTaskStatus.QUEUED,
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
        ]
        verbose_name = _("链上任务")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.tx_hash or f"tx-task-{self.pk or 'unsaved'}"

    def save(self, *args, **kwargs):
        # TxTask 是跨链主锚点，统一在保存前执行 full_clean，校验 status 取值合法等，
        # 避免后台和脚本写入脏状态。status 为单枚举，已无需再做跨字段一致性校验。
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def display_status(self) -> str:
        """面向运营的人类可读状态，直接取单枚举的展示文案。"""
        return self.get_status_display()

    @db_transaction.atomic
    def append_tx_hash(
        self, tx_hash: str, *, expires_at_ms: int | None = None
    ) -> TxHash:
        locked_task = TxTask.objects.select_for_update().get(pk=self.pk)
        # 并发广播可能产生相同 tx_hash（相同 nonce + gas_price 签名结果相同），
        # 若已存在则视为幂等，直接返回。
        existing = TxHash.objects.filter(chain=locked_task.chain, hash=tx_hash).first()
        if existing:
            if existing.tx_task_id != locked_task.pk:
                raise IntegrityError(
                    "TxHash 已归属其他 TxTask："
                    f"chain_id={locked_task.chain_id} hash={tx_hash} "
                    f"existing_task_id={existing.tx_task_id} current_task_id={locked_task.pk}"
                )
            TxTask.objects.filter(pk=locked_task.pk).update(
                tx_hash=tx_hash,
                updated_at=timezone.now(),
            )
            if expires_at_ms is not None and existing.expires_at_ms != expires_at_ms:
                TxHash.objects.filter(pk=existing.pk).update(
                    expires_at_ms=expires_at_ms
                )
                existing.expires_at_ms = expires_at_ms
            self.tx_hash = tx_hash
            return existing
        max_version = (
            TxHash.objects.filter(tx_task=locked_task)
            .aggregate(max_version=models.Max("version"))
            .get("max_version")
        )
        next_version = 1 if max_version is None else int(max_version) + 1  # noqa
        created = TxHash.objects.create(
            tx_task=locked_task,
            chain=locked_task.chain,
            hash=tx_hash,
            version=next_version,
            expires_at_ms=expires_at_ms,
        )
        TxTask.objects.filter(pk=locked_task.pk).update(
            tx_hash=tx_hash,
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
    def mark_submitted(*, task_id: int, allow_resubmitted: bool = False) -> bool:
        """将任务标记为已提交，返回是否真正写入。

        QUEUED -> SUBMITTED 是正常首提交流程；Tron 交易过期重签会在
        SUBMITTED 阶段再次提交，此时允许调用方显式传入 allow_resubmitted。
        """
        allowed_statuses = [TxTaskStatus.QUEUED]
        if allow_resubmitted:
            allowed_statuses.append(TxTaskStatus.SUBMITTED)
        return bool(
            TxTask.objects.filter(pk=task_id, status__in=allowed_statuses).update(
                status=TxTaskStatus.SUBMITTED,
                updated_at=timezone.now(),
            )
        )

    @staticmethod
    def mark_finalized_success(*, chain: Chain, tx_hash: str) -> bool:
        """将匹配的任务标记为成功终局，返回是否真正发生了状态推进。

        使用 .update() 绕过 save()/full_clean() 以避免逐行加载；
        排除已处于终局态的任务，保证终局幂等且不被覆盖。
        按单个主键过滤，故 .update() 至多命中一条，结果收敛为布尔。
        """
        if not tx_hash:
            return False
        task = TxTask.resolve_by_hash(chain=chain, tx_hash=tx_hash)
        if task is None:
            return False
        updated = (
            TxTask.objects.filter(pk=task.pk)
            .exclude(status__in=TERMINAL_TX_TASK_STATUSES)
            .update(
                tx_hash=tx_hash,
                status=TxTaskStatus.SUCCEEDED,
                updated_at=timezone.now(),
            )
        )
        return bool(updated)

    @staticmethod
    def mark_finalized_failed(
        *,
        task_id: int,
        expected_status: TxTaskStatus | None = None,
    ) -> bool:
        """将匹配的任务标记为失败终局，返回是否真正发生了状态推进。

        按单个主键过滤，故 .update() 至多命中一条，结果收敛为布尔。
        """
        queryset = TxTask.objects.filter(pk=task_id).exclude(
            status__in=TERMINAL_TX_TASK_STATUSES
        )
        if expected_status is not None:
            queryset = queryset.filter(status=expected_status)
        return bool(
            queryset.update(
                status=TxTaskStatus.FAILED,
                updated_at=timezone.now(),
            )
        )


class VaultSlotUsage(models.TextChoices):
    DEPOSIT = "deposit", _("充值收款")
    INVOICE = "invoice", _("账单收款")


class VaultSlot(models.Model):
    """项目在指定链上的 XcashVaultSlot 智能合约收款槽位。

    本模型只表达跨链一致的业务身份和生命周期锚点。地址预测、部署交易构造、
    归集合约调用等链专属执行细节由 chains.vault_slots 分发到 evm/tron 模块。
    """

    chain = models.ForeignKey("Chain", on_delete=models.CASCADE, verbose_name=_("链"))
    usage = models.CharField(
        _("用途"),
        choices=VaultSlotUsage,
        max_length=16,
        db_index=True,
    )
    project = models.ForeignKey(
        "projects.Project",
        on_delete=models.CASCADE,
        verbose_name=_("项目"),
    )
    customer = models.ForeignKey(
        "projects.Customer",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name=_("客户"),
    )
    invoice_index = models.PositiveIntegerField(
        _("账单槽位序号"),
        null=True,
        blank=True,
    )
    address = AddressField(_("收款地址"))
    salt = models.BinaryField(_("CREATE2 Salt"), max_length=32)
    deploy_tx_task = models.OneToOneField(
        "chains.TxTask",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="deployed_vault_slot",
        verbose_name=_("部署交易任务"),
    )
    is_deployed = models.BooleanField(
        _("是否已部署"),
        default=False,
        help_text=_("链上已观测到该 VaultSlot 合约 code。"),
    )
    has_received = models.BooleanField(
        _("是否收到过资金"),
        default=False,
    )
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("customer", "chain"),
                name="uniq_vault_slot_customer_chain",
            ),
            models.UniqueConstraint(
                fields=("project", "usage", "chain", "invoice_index"),
                name="uniq_vault_slot_project_usage_chain_invoice_index",
            ),
            models.UniqueConstraint(
                fields=("chain", "address"),
                name="uniq_vault_slot_chain_address",
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
                name="ck_vault_slot_usage_customer",
            ),
        ]
        verbose_name = _("收款地址")
        verbose_name_plural = verbose_name

    def __str__(self):
        return self.address

    def save(self, *args, **kwargs):
        if self.chain_id and self.chain.type not in {ChainType.EVM, ChainType.TRON}:
            raise ValueError("VaultSlot 仅支持 EVM / Tron 链")
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

    @staticmethod
    def build_salt(
        *,
        usage: VaultSlotUsage,
        chain_type: ChainType | str = ChainType.EVM,
        customer=None,
        project_id: int | None = None,
        invoice_index: int | None = None,
    ) -> bytes:
        if chain_type == ChainType.TRON:
            namespace = b"xcash:tron-vault-slot"
        elif chain_type == ChainType.EVM:
            namespace = b"xcash:evm-vault-slot"
        else:
            raise ValueError(f"unsupported VaultSlot chain_type: {chain_type}")

        if usage == VaultSlotUsage.DEPOSIT:
            if customer is None:
                raise ValueError("customer is required for deposit salt")
            # 不掺 chain.code：同一链类型内的 factory/template/归集地址一致时，
            # 同一业务身份在该链类型所有网络上得到一致 VaultSlot 地址。
            return keccak(
                namespace
                + b":deposit:"
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
                namespace
                + b":invoice:"
                + str(project_id).encode()
                + b":"
                + str(invoice_index).encode()
            )

        raise ValueError(f"unsupported VaultSlot usage: {usage}")

    @staticmethod
    def ensure_deposit_address(
        chain: Chain,
        customer,
        *,
        crypto,
    ) -> str:
        from chains.vault_slots import ensure_deposit_address

        return ensure_deposit_address(
            chain=chain,
            customer=customer,
            crypto=crypto,
        )

    @staticmethod
    def ensure_invoice_address(
        *,
        project,
        chain: Chain,
        invoice_index: int,
        crypto,
    ) -> str:
        from chains.vault_slots import ensure_invoice_address

        return ensure_invoice_address(
            project=project,
            chain=chain,
            invoice_index=invoice_index,
            crypto=crypto,
        )

    @staticmethod
    def schedule_deploy(slot_pk: int) -> TxTask | None:
        from chains.vault_slots import schedule_deploy

        return schedule_deploy(slot_pk)

    @staticmethod
    def schedule_collect_for_deposit(
        deposit_pk: int,
    ) -> VaultSlotCollectSchedule | None:
        from chains.vault_slots import schedule_collect_for_deposit

        return schedule_collect_for_deposit(deposit_pk)

    @staticmethod
    def schedule_collect_for_invoice(
        invoice_pk: int,
    ) -> VaultSlotCollectSchedule | None:
        from chains.vault_slots import schedule_collect_for_invoice

        return schedule_collect_for_invoice(invoice_pk)

    @staticmethod
    def schedule_collect_for_slot(
        *, chain: Chain, crypto, slot
    ) -> VaultSlotCollectSchedule | None:
        from chains.vault_slots import schedule_collect_for_slot

        return schedule_collect_for_slot(chain=chain, crypto=crypto, slot=slot)

    @staticmethod
    def matched_addresses_for_candidates(
        *, chain: Chain, candidates: set[str]
    ) -> set[str]:
        if not candidates:
            return set()
        return set(
            VaultSlot.objects.filter(
                chain=chain,
                address__in=candidates,
            ).values_list("address", flat=True)
        )


class VaultSlotBalance(models.Model):
    """VaultSlot 在某条链、某个币种上的链上余额快照。

    这是展示和归集决策用的状态快照，不是账本流水；更新时必须读取链上余额真值后
    覆盖写入，避免靠 Transfer 加减推导导致 reorg、漏扫或手动归集后长期漂移。
    """

    vault_slot = models.ForeignKey(
        VaultSlot,
        on_delete=models.CASCADE,
        related_name="balances",
        verbose_name=_("收款地址"),
    )
    chain = models.ForeignKey("Chain", on_delete=models.CASCADE, verbose_name=_("链"))
    crypto = models.ForeignKey(
        "currencies.Crypto",
        on_delete=models.PROTECT,
        verbose_name=_("币种"),
    )
    value = models.DecimalField(
        _("链上最小单位余额"),
        max_digits=80,
        decimal_places=0,
        default=0,
    )
    amount = models.DecimalField(
        _("余额"),
        max_digits=80,
        decimal_places=30,
        default=0,
    )
    worth = models.DecimalField(
        _("价值(USD)"),
        max_digits=80,
        decimal_places=30,
        default=0,
    )
    synced_block_number = models.PositiveIntegerField(
        _("同步区块"),
        null=True,
        blank=True,
    )
    synced_at = models.DateTimeField(_("同步时间"), null=True, blank=True)
    last_tx_hash = HashField(
        verbose_name=_("最近触发交易"),
        unique=False,
        null=True,
        blank=True,
    )
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("chain", "vault_slot", "crypto"),
                name="uniq_vault_slot_balance_chain_slot_crypto",
            ),
        ]
        ordering = ("vault_slot_id", "crypto_id")
        verbose_name = _("收款地址余额")
        verbose_name_plural = verbose_name

    def __str__(self) -> str:
        return f"{self.vault_slot_id}:{self.crypto_id}:{self.amount}"

    def clean(self) -> None:
        super().clean()
        if self.vault_slot_id and self.chain_id != self.vault_slot.chain_id:
            raise ValidationError(
                {"chain": _("余额所属链必须与 VaultSlot.chain 一致。")}
            )


class VaultSlotCollectSchedule(models.Model):
    """VaultSlot 归集窗口计划。

    计划只负责把同一链、同一 VaultSlot、同一币种的多次到账聚合到 due_at；
    到期后创建的 TxTask 继续承载广播、确认和失败状态。
    """

    vault_slot = models.ForeignKey(
        VaultSlot,
        on_delete=models.CASCADE,
        related_name="collect_schedules",
        verbose_name=_("收款地址"),
    )
    chain = models.ForeignKey("Chain", on_delete=models.CASCADE, verbose_name=_("链"))
    crypto = models.ForeignKey(
        "currencies.Crypto",
        on_delete=models.PROTECT,
        verbose_name=_("币种"),
    )
    due_at = models.DateTimeField(_("计划执行时间"), db_index=True)
    tx_task = models.OneToOneField(
        "chains.TxTask",
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

        from core.runtime_settings import get_vault_slot_collect_delay

        due_at = timezone.now() + get_vault_slot_collect_delay(chain.type)
        try:
            with db_transaction.atomic():
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

    # 暂不可执行/执行失败的计划退避间隔：归集本身是延迟容忍操作，10 分钟既给
    # 暂时性故障（RPC 抖动、币种临时停用）留出恢复窗口，又不明显拖慢资金归集。
    RETRY_BACKOFF = timedelta(minutes=10)

    def create_tx_task(self) -> TxTask:
        from chains.vault_slots import create_collect_tx_task_for_slot

        return create_collect_tx_task_for_slot(
            chain=self.chain,
            crypto=self.crypto,
            slot=self.vault_slot,
        )

    def defer_retry(self) -> None:
        """把计划推迟到退避窗口后重试。

        execute_due 按 due_at 升序取批（limit 封顶），失败计划若不推迟会恒占
        批次头部；同因失败的计划一旦攒满 limit 个，所有链的归集调度都会被饿死。
        """
        self.due_at = timezone.now() + self.RETRY_BACKOFF
        self.save(update_fields=["due_at", "updated_at"])

    @classmethod
    def ensure_pending_due_now(
        cls,
        *,
        chain: Chain,
        vault_slot: VaultSlot,
        crypto,
    ) -> VaultSlotCollectSchedule:
        """确保存在一条立即可执行的 pending 归集计划。"""
        schedule = cls.ensure_pending(
            chain=chain,
            vault_slot=vault_slot,
            crypto=crypto,
        )
        now = timezone.now()
        if schedule.due_at > now:
            cls.objects.filter(pk=schedule.pk).update(due_at=now, updated_at=now)
            schedule.due_at = now
            schedule.updated_at = now
        return schedule

    def requeue_failed_collect(self) -> VaultSlotCollectSchedule | None:
        """把失败终局的归集计划重新排成一条 pending 计划，供人工恢复使用。"""
        if self.tx_task_id is None:
            return None
        if self.tx_task.status != TxTaskStatus.FAILED:
            return None
        return self.ensure_pending_due_now(
            chain=self.chain,
            vault_slot=self.vault_slot,
            crypto=self.crypto,
        )

    @classmethod
    def execute_due(cls, *, limit: int = 32) -> int:
        from chains.vault_slot_balances import refresh_vault_slot_balance_safely
        from chains.vault_slots import can_create_collect_tx_task

        now = timezone.now()
        created_count = 0
        with db_transaction.atomic():
            schedules = list(
                cls.objects.select_for_update(skip_locked=True)
                .select_related(
                    "chain",
                    "crypto",
                    "vault_slot",
                    "vault_slot__project",
                )
                .filter(tx_task__isnull=True, due_at__lte=now)
                .order_by("due_at", "pk")[:limit]
            )
            for schedule in schedules:
                if not can_create_collect_tx_task(
                    chain=schedule.chain,
                    slot=schedule.vault_slot,
                ):
                    schedule.defer_retry()
                    continue
                balance = refresh_vault_slot_balance_safely(
                    slot=schedule.vault_slot,
                    crypto=schedule.crypto,
                    reason="before_collect",
                )
                if balance is None:
                    schedule.defer_retry()
                    continue
                if balance.value <= 0:
                    schedule.delete()
                    continue
                # 单条用 savepoint 隔离:个别计划建/绑任务失败不回滚整批归集调度。
                try:
                    with db_transaction.atomic():
                        tx_task = schedule.create_tx_task()
                        schedule.tx_task = tx_task
                        schedule.save(update_fields=["tx_task", "updated_at"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "VaultSlot 归集计划创建或绑定任务失败,退避重试",
                        schedule_id=schedule.pk,
                        error=str(exc),
                    )
                    schedule.defer_retry()
                    continue
                created_count += 1
        return created_count


class DepositVaultSlot(VaultSlot):
    class Meta:
        proxy = True
        verbose_name = _("充值收款地址")
        verbose_name_plural = verbose_name


class InvoiceVaultSlot(VaultSlot):
    class Meta:
        proxy = True
        verbose_name = _("账单收款地址")
        verbose_name_plural = verbose_name


class TransferStatus(models.TextChoices):
    CONFIRMING = "confirming", _("确认中")
    CONFIRMED = "confirmed", _("已确认")


class ConfirmMode(models.TextChoices):
    FULL = "full", _("完全")
    QUICK = "quick", _("快速")


class Transfer(models.Model):
    if TYPE_CHECKING:
        # Django 反向 OneToOne 描述符在运行时动态挂载；这里显式声明给 IDE 做静态解析。
        invoice: Invoice
        deposit: Deposit

    chain = models.ForeignKey(Chain, on_delete=models.CASCADE, verbose_name=_("链"))
    block = models.IntegerField(_("区块高度"))
    block_hash = HashField(
        verbose_name=_("区块哈希"),
        unique=False,
    )
    hash = HashField(unique=False, verbose_name=_("哈希"))
    event_index = models.PositiveIntegerField(
        _("交易内事件序号"),
        null=True,
        blank=True,
        help_text=_("同一笔链上交易内第几条入账事件；旧数据或手工记录可为空。"),
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
        default=TransferType.Unmatched,
    )
    confirm_mode = models.CharField(
        choices=ConfirmMode,
        default=ConfirmMode.FULL,
        max_length=8,
        verbose_name=_("确认模式"),
        help_text=_(
            "当前仅 Invoice 业务根据 fast_confirm_threshold 动态设置 QUICK/FULL；Deposit 始终使用默认 FULL，走完整区块确认流程。"
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
                fields=("chain", "hash", "event_index"),
                name="uniq_transfer_chain_hash_event_index",
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

        # Transfer 只承载外部收款事实。若误落库为内部 TxTask 的 hash，直接跳过业务匹配，
        # 避免系统归集/部署等主动交易污染 Invoice / Deposit 入账状态。
        tx_task = TxTask.resolve_by_hash(chain=self.chain, tx_hash=self.hash)
        if tx_task is not None:
            logger.warning(
                "Transfer 命中内部 TxTask，跳过外部收款匹配",
                transfer_id=self.pk,
                tx_task_id=tx_task.pk,
                tx_hash=self.hash,
            )
            self._mark_processed()
            return

        # 未命中内部任务的转账统一按外部收款逻辑逐一尝试匹配。
        from deposits.service import DepositService
        from invoices.service import InvoiceService

        (
            InvoiceService.try_match_invoice(self)
            or DepositService.try_match_deposit_transfer(self)
        )
        self._mark_processed()

        if self.confirm_mode == ConfirmMode.QUICK:
            from .tasks import confirm_transfer

            db_transaction.on_commit(
                lambda transfer_id=self.pk: confirm_transfer.delay(transfer_id)
            )

    def _mark_processed(self) -> None:
        self.processed_at = timezone.now()
        Transfer.objects.filter(pk=self.pk).update(processed_at=self.processed_at)

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
        self._mark_vault_slot_received()
        self._refresh_vault_slot_balances()

        self._dispatch_business_confirm()

    @db_transaction.atomic
    def drop(self):
        """删除 Transfer 记录。

        删除记录以释放唯一约束 (chain, hash, event_index),
        使 reorg 后同一笔 tx 被重新打包时, 扫描器可以自然重建 Transfer。
        """
        # 先加行锁，防止并发处理；已删除的 Transfer 直接跳过。
        if not Transfer.objects.select_for_update().filter(pk=self.pk).exists():
            return
        self.refresh_from_db()

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
            DepositService.create_confirmed_deposit(self)

    def _mark_vault_slot_received(self) -> None:
        """确认后的转入事实才标记 VaultSlot 曾经收到过资金。"""
        VaultSlot.objects.filter(
            chain=self.chain,
            address=self.to_address,
            has_received=False,
        ).update(has_received=True)

    def _refresh_vault_slot_balances(self) -> None:
        """确认后按链上余额真值刷新受影响 VaultSlot 余额快照。"""
        from chains.vault_slot_balances import (  # noqa: PLC0415
            refresh_vault_slot_balance_for_transfer,
        )

        refresh_vault_slot_balance_for_transfer(self)
