from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from aml.tasks import screen_invoice_aml
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from chains.models import ConfirmMode
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.service import ChainService
from chains.service import TransferService
from chains.vault_slots import schedule_collect_for_slot
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback
from common.utils.math import format_decimal_stripped
from currencies.models import PriceUnavailableError
from currencies.service import CryptoService
from webhooks.service import WebhookService

from .exceptions import InvoiceStatusError
from .models import DifferRecipientAddress
from .models import Invoice
from .models import InvoiceProtocol
from .models import InvoiceStatus

if TYPE_CHECKING:
    from chains.models import Transfer

logger = structlog.get_logger()

# TRC20 转账 feeLimit 上限（单位 sun，100 TRX），够付绝大多数 TRC20 转账所需的能量/带宽。
TRON_TRC20_FEE_LIMIT_SUN = 100_000_000

INVOICE_PUBLIC_CACHE_KEY_PREFIX = "invoice:public:v1"
INVOICE_PUBLIC_WAITING_CACHE_TTL = 2
INVOICE_PUBLIC_NON_WAITING_CACHE_TTL = 60 * 60


def invoice_public_cache_key(sys_no: str) -> str:
    return f"{INVOICE_PUBLIC_CACHE_KEY_PREFIX}:{sys_no}"


def invoice_public_cache_ttl(status_value: str) -> int:
    if status_value == InvoiceStatus.WAITING:
        return INVOICE_PUBLIC_WAITING_CACHE_TTL
    return INVOICE_PUBLIC_NON_WAITING_CACHE_TTL


def expire_invoice_public_cache_on_commit(sys_no: str) -> None:
    """事务提交后失效公开详情缓存。

    绑定链上付款、账单确认都可能发生在 EXPIRED 长 TTL 缓存期间（EXPIRED 不是
    终局状态，迟到确认会翻 COMPLETED）；必须在提交后删除，若在事务内删除，
    并发 retrieve 可能在提交前又把旧状态写回缓存，失效即丢失。
    """
    transaction.on_commit(lambda: cache.delete(invoice_public_cache_key(sys_no)))


class InvoiceService:
    @staticmethod
    def finalize_methods(
        *,
        project,
        requested,
    ) -> dict[str, list[str]]:
        """生成/收敛账单最终 methods。

        Invoice.available_methods(project) 是真正可付款的 crypto -> chain 集合；
        调用方未指定 methods 时直接采用全集，指定时必须是全集子集。计价货币恒为
        法币，只决定 amount 的计价单位，不参与收款币种的收敛。
        """
        available = Invoice.available_methods(project)
        if not available:
            raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS)

        if not requested:
            finalized = available
        else:
            if not isinstance(requested, dict):
                raise APIError(ErrorCode.PARAMETER_ERROR, detail="methods")

            finalized: dict[str, list[str]] = {}
            for crypto_symbol, chain_codes in requested.items():
                if not isinstance(chain_codes, (list, tuple)):
                    raise APIError(ErrorCode.PARAMETER_ERROR, detail=crypto_symbol)

                try:
                    CryptoService.get_by_symbol(crypto_symbol)
                except ObjectDoesNotExist as exc:
                    raise APIError(
                        ErrorCode.INVALID_CRYPTO,
                        detail=crypto_symbol,
                    ) from exc

                available_chains = set(available.get(crypto_symbol, []))
                if not available_chains:
                    raise APIError(
                        ErrorCode.NO_RECIPIENT_ADDRESS,
                        detail=crypto_symbol,
                    )

                normalized_codes: list[str] = []
                for chain_code in chain_codes:
                    if not isinstance(chain_code, str):
                        raise APIError(
                            ErrorCode.PARAMETER_ERROR,
                            detail=crypto_symbol,
                        )

                    try:
                        ChainService.get_by_code(chain_code)
                    except ObjectDoesNotExist as exc:
                        raise APIError(
                            ErrorCode.INVALID_CHAIN,
                            detail=chain_code,
                        ) from exc
                    if chain_code not in available_chains:
                        raise APIError(
                            ErrorCode.NO_RECIPIENT_ADDRESS,
                            detail=f"{crypto_symbol}:{chain_code}",
                        )
                    normalized_codes.append(chain_code)

                if normalized_codes:
                    finalized[crypto_symbol] = normalized_codes

            if not finalized:
                raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS)

        return {
            crypto_symbol: InvoiceService.sort_chain_codes(chain_codes)
            for crypto_symbol, chain_codes in finalized.items()
        }

    @staticmethod
    def sort_chain_codes(chain_codes: list[str]) -> list[str]:
        """按 Chain.sort_order 升序排列链；同序号用 code 保持稳定顺序。"""
        if len(chain_codes) <= 1:
            return list(chain_codes)

        order_by_code = {
            chain.code: (chain.sort_order, chain.code)
            for chain in Chain.objects.filter(code__in=chain_codes).only(
                "code",
                "sort_order",
            )
        }
        return sorted(
            chain_codes,
            key=lambda chain_code: order_by_code.get(chain_code, (0, chain_code)),
        )

    @staticmethod
    def refresh_initial_worth(invoice: Invoice) -> None:
        """在账单创建后立即固化基础 worth，避免继续依赖隐式 post_save signal。"""
        # worth 表达账单计价法币面额的 USD 价值；支付币种只决定链上支付指引，
        # 不再让 pay_amount 的实时加密货币报价反向改变账单基础价值。
        try:
            worth = invoice.calculate_worth_usd()
        except PriceUnavailableError:
            # 非 USD 计价需要 USDT 对该法币的报价（epay 协议默认 CNY）。价格源尚未
            # 刷新、或该法币不在行情源覆盖范围时，worth 暂缺不能让整个建单请求 500：
            # worth 只用于风控阈值与统计，账单本身的收款能力不依赖它。
            logger.warning(
                "invoice worth unavailable, keep default",
                invoice=invoice.sys_no,
                currency=invoice.currency_id,
            )
            return

        # worth 只更新自身字段，直接 update 可避免把整行实例再次写回。
        Invoice.objects.filter(pk=invoice.pk).update(
            worth=worth,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

    @staticmethod
    def try_auto_select_single_method(invoice: Invoice) -> None:
        """仅当 methods 唯一时自动分配当前支付指引，替代历史 post_save signal。"""
        methods = invoice.methods or {}
        if len(methods) != 1:
            return

        symbol, chain_codes = next(iter(methods.items()))
        if len(chain_codes) != 1:
            return

        try:
            crypto = CryptoService.get_by_symbol(symbol)
            chain = ChainService.get_by_code(chain_codes[0])
            invoice.select_method(crypto, chain)
        except ObjectDoesNotExist:
            logger.warning(
                "initialize_invoice: resource missing",
                symbol=symbol,
                chain=chain_codes[0],
            )
        except Invoice.InvoiceAllocationError as exc:
            logger.warning("initialize_invoice allocation failed", detail=str(exc))
        except InvoiceStatusError as exc:
            # 创建即初始化的账单理论上必为 WAITING 且未绑定付款；此分支仅防御
            # select_method 锁内状态复查的意外抛出，避免账单创建请求被打成 500。
            logger.warning("initialize_invoice status recheck failed", detail=str(exc))
        except PriceUnavailableError as exc:
            # 缺该币报价时跳过自动选币即可：账单仍以 WAITING 建成，买家之后可在
            # 价格恢复后自行选择支付方式。让它冒泡会使整个建单请求 500。
            logger.warning(
                "initialize_invoice price unavailable",
                symbol=symbol,
                detail=str(exc),
            )

    @staticmethod
    def schedule_expiration_check(invoice: Invoice) -> None:
        """在事务提交后注册过期检查任务，避免回滚后仍派发悬空 Celery 任务。"""
        from functools import partial

        from .tasks import check_expired

        # 提前捕获值，避免闭包延迟绑定陷阱。
        eta = invoice.expires_at + timedelta(seconds=1)
        dispatch = partial(check_expired.apply_async, (invoice.id,), eta=eta)
        transaction.on_commit(dispatch)

    @classmethod
    def initialize_invoice(cls, invoice: Invoice) -> Invoice:
        """账单创建后的显式初始化入口：worth、自动支付方式、过期任务。"""
        cls.refresh_initial_worth(invoice)
        cls.try_auto_select_single_method(invoice)
        cls.schedule_expiration_check(invoice)
        return invoice

    @staticmethod
    def build_webhook_payload(invoice: Invoice) -> dict:
        """构建 webhook 推送给商户的 payload，与 model 层解耦。

        将 payload 结构集中在 service 层管理，便于未来版本化或按场景差异化。
        """
        return {
            "type": "invoice",
            "data": {
                "sys_no": invoice.sys_no,
                "out_no": invoice.out_no,
                "crypto": invoice.crypto.symbol if invoice.crypto else None,
                "chain": invoice.transfer.chain.code if invoice.transfer_id else None,
                "pay_address": invoice.pay_address,
                "pay_amount": (
                    format_decimal_stripped(invoice.pay_amount)
                    if invoice.pay_amount is not None
                    else None
                ),
                "hash": invoice.transfer.hash if invoice.transfer_id else None,
                "block": invoice.transfer.block if invoice.transfer_id else None,
                "confirmed": invoice.status == InvoiceStatus.COMPLETED,
                "risk_level": invoice.risk_level,
                "risk_score": (
                    format_decimal_stripped(invoice.risk_score)
                    if invoice.risk_score is not None
                    else None
                ),
            },
        }

    @staticmethod
    def wallet_payment_base_units(invoice: Invoice):
        """一键支付的链无关前置校验与金额换算，供 EVM / Tron 各链型复用。

        返回 (base_units, crypto_on_chain) 或 None。把「账单字段齐全 + 取链上币种
        记录 + 金额按 decimals 换算为最小单位整数」这套与具体链型无关的逻辑收口在
        此处，各链型只在调用前后做自己的门控与参数拼装，避免重复实现易漂移。

        校验/换算口径（任一不满足即返回 None，由调用方降级）：
        - 账单必须已分配支付指引（chain/crypto/pay_address/pay_amount 齐全）。
        - 金额按该币在该链的 decimals 换算为最小单位整数；若除不尽（报价精度
          超过链上可表示精度）返回 None 而非截断——绝不产出与 pay_amount 不一致
          的金额，否则付款必然无法匹配，比不编码金额更糟。
        """
        if (
            invoice.chain_id is None
            or invoice.crypto_id is None
            or not invoice.pay_address
            or invoice.pay_amount is None
        ):
            return None

        # decimals 与合约地址同属一条链上币种记录，一次取出，避免高频 retrieve
        # 端点对同一行重复查询。lazy import 规避潜在循环依赖。
        from currencies.models import CryptoOnChain

        try:
            crypto_on_chain = CryptoOnChain.objects.get(
                crypto=invoice.crypto, chain=invoice.chain
            )
        except CryptoOnChain.DoesNotExist:
            return None

        scaled = invoice.pay_amount * (Decimal(10) ** crypto_on_chain.decimals)
        if scaled != scaled.to_integral_value():
            # 报价精度超过链上精度，无法精确编码，降级为地址二维码。
            return None
        return int(scaled), crypto_on_chain

    @staticmethod
    def evm_payment_params(invoice: Invoice) -> dict | None:
        """提炼 EVM 一键支付所需的规范化参数，供 URI 与注入式钱包两路复用。

        集中所有「可一键支付」前置校验与金额换算，返回结构化结果或 None：

            原生币  {chain_id, is_native=True,  base_units, token_address=None, recipient}
            代币    {chain_id, is_native=False, base_units, token_address,      recipient}

        校验/换算口径（任一不满足即返回 None，由调用方降级）：
        - 仅 EVM 链有 chainId；非 EVM（如 Tron）无统一标准，不产出参数。
        - 账单齐全校验与金额换算统一委托 wallet_payment_base_units。
        - 标记为非原生却查不到合约地址属配置异常，同样降级。
        """
        # 未分配支付指引时 invoice.chain 为 None，先挡住再读链属性，避免空引用。
        if invoice.chain_id is None:
            return None

        chain = invoice.chain

        # 仅 EVM 链有 chain_id 与 EIP-681；其余链型暂不产出结构化支付参数。
        if chain.type != ChainType.EVM or chain.chain_id is None:
            return None

        base = InvoiceService.wallet_payment_base_units(invoice)
        if base is None:
            return None
        base_units, crypto_on_chain = base

        if invoice.crypto.is_native:
            return {
                "chain_id": chain.chain_id,
                "is_native": True,
                "base_units": base_units,
                "token_address": None,
                "recipient": invoice.pay_address,
            }

        if not crypto_on_chain.address:
            # 标记为非原生却查不到合约地址属配置异常，降级而非产出错误参数。
            return None
        return {
            "chain_id": chain.chain_id,
            "is_native": False,
            "base_units": base_units,
            "token_address": crypto_on_chain.address,
            "recipient": invoice.pay_address,
        }

    @staticmethod
    def build_payment_uri(invoice: Invoice) -> str | None:
        """为已分配支付指引的账单生成钱包可扫描的支付 URI（EIP-681）。

        把链(chainId)、代币(合约地址/原生币)、收款地址与精确金额一并编码进
        二维码，最大限度减少买家手输金额导致的「付款金额不符」。目前仅 EVM 链
        有统一标准（EIP-681）：

            原生币  ethereum:<收款地址>@<chainId>?value=<wei>
            代币    ethereum:<合约>@<chainId>/transfer?address=<收款地址>&uint256=<最小单位>

        非 EVM 链（如 Tron 无跨钱包标准）返回 None，由前端降级为纯地址二维码。

        金额换算与各 None 降级分支统一委托 evm_payment_params，本方法只负责把
        规范化参数拼成 EIP-681 字符串，保证与注入式钱包支付走同一套校验口径。
        """
        params = InvoiceService.evm_payment_params(invoice)
        if params is None:
            return None

        if params["is_native"]:
            return (
                f"ethereum:{params['recipient']}@{params['chain_id']}"
                f"?value={params['base_units']}"
            )
        return (
            f"ethereum:{params['token_address']}@{params['chain_id']}"
            f"/transfer?address={params['recipient']}&uint256={params['base_units']}"
        )

    @staticmethod
    def build_evm_wallet_payment(invoice: Invoice) -> dict | None:
        """为注入式 EVM 钱包（MetaMask 等）一键支付输出结构化交易参数。

        前端拿到该字段后可直接构造 eth_sendTransaction，无需买家手输任何金额，
        从源头消除「付款金额不符」。后端结算仍走盯地址扫链匹配，本字段只是同一
        付款意图的另一种结构化表达，不改变结算模型。

        复用 evm_payment_params 的全部前置校验与金额换算；不可一键支付时返回 None：

            原生币  {chain_id, to=<收款地址>, value=hex(wei),  data=None}
            代币    {chain_id, to=<合约地址>, value="0x0",      data=ERC-20 transfer calldata}

        代币 calldata 按 ERC-20 transfer(address,uint256) ABI 编码：函数选择器
        a9059cbb + 收款地址（去 0x、小写、左补零到 64 hex）+ 金额（hex、左补零到
        64 hex），与链上实际转账完全一致。
        """
        params = InvoiceService.evm_payment_params(invoice)
        if params is None:
            return None

        if params["is_native"]:
            return {
                "chain_id": params["chain_id"],
                "to": params["recipient"],
                "value": hex(params["base_units"]),
                "data": None,
            }

        # ERC-20 transfer(address,uint256)：选择器 + 32 字节地址 + 32 字节金额。
        selector = "a9059cbb"
        recipient_word = params["recipient"].lower().removeprefix("0x").zfill(64)
        amount_word = format(params["base_units"], "064x")
        return {
            "chain_id": params["chain_id"],
            "to": params["token_address"],
            "value": "0x0",
            "data": "0x" + selector + recipient_word + amount_word,
        }

    @staticmethod
    def build_tron_wallet_payment(invoice: Invoice) -> dict | None:
        """为 TronLink 等 Tron 钱包一键支付输出结构化交易参数；不可一键支付时返回 None。

        Tron 没有 chainId 与 EIP-1193，无法照搬 EVM 那套参数。前端拿到这些字段后用
        TronWeb 自行构造并签名交易（原生 TRX 走 sendTrx、TRC20 走合约 transfer），
        后端只负责给出收款地址、最小单位金额、合约地址与网络标识；结算仍走盯地址扫链
        匹配，本字段只是同一付款意图的另一种结构化表达，不改变结算模型。

            原生 TRX  {is_native=True,  to, contract=None,        amount, fee_limit, is_testnet}
            TRC20    {is_native=False, to, contract=<合约地址>,   amount, fee_limit, is_testnet}

        amount 为最小单位整数字符串（TRX 用 sun、TRC20 用代币精度）；is_testnet 供前端
        校验 TronLink 当前网络（主网 / Nile）。账单齐全校验与金额换算统一委托
        wallet_payment_base_units；标记为非原生却查不到合约地址属配置异常，降级为 None。
        """
        # 未分配支付指引时 invoice.chain 为 None，先挡住再读链属性，避免空引用。
        if invoice.chain_id is None:
            return None

        chain = invoice.chain

        # 仅 Tron 链产出该结构化支付参数，其余链型不适用。
        if chain.type != ChainType.TRON:
            return None

        base = InvoiceService.wallet_payment_base_units(invoice)
        if base is None:
            return None
        base_units, crypto_on_chain = base

        # fee_limit 对原生 TRX 转账无意义（前端忽略即可），仍统一带上以保持字段稳定。
        payment = {
            "is_native": invoice.crypto.is_native,
            "to": invoice.pay_address,
            "contract": None,
            "amount": str(base_units),
            "fee_limit": TRON_TRC20_FEE_LIMIT_SUN,
            "is_testnet": chain.is_testnet,
        }
        if invoice.crypto.is_native:
            return payment

        if not crypto_on_chain.address:
            # 标记为非原生却查不到合约地址属配置异常，降级而非产出错误参数。
            return None
        payment["contract"] = crypto_on_chain.address
        return payment

    @staticmethod
    @transaction.atomic
    def try_match_invoice(
        transfer: Transfer,
    ):
        # 只匹配账单当前支付指引。用户切换支付方式后，旧指引不再作为该账单的
        # 自动入账入口，避免为低概率误付款保留多槽位状态机。
        base_filter = Invoice.objects.filter(
            chain=transfer.chain,
            crypto=transfer.crypto,
            pay_address=transfer.to_address,
            started_at__lte=transfer.datetime,
            expires_at__gte=transfer.datetime,
            status__in=[InvoiceStatus.WAITING, InvoiceStatus.EXPIRED],
        ).filter(Q(transfer__isnull=True) | Q(transfer=transfer))

        candidate = (
            base_filter.filter(pay_amount=transfer.amount)
            .order_by("-started_at", "-pk")
            .values("pk")
            .first()
        )
        if candidate is None:
            return False

        # 锁住 Invoice（与 select_method 保持相同的加锁对象，防止切换支付方式
        # 与链上匹配并发覆盖）。
        # of=("self",) 限定行锁只作用于 invoices_invoice，避免 select_related 触发
        # PostgreSQL 把 projects_project / currencies_crypto 等 join 父表也锁成
        # FOR UPDATE，与并发 INSERT/UPDATE 子表时 PG 自动加的 FK FOR KEY SHARE 互斥而死锁。
        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("project", "crypto", "chain")
            .filter(pk=candidate["pk"])
            .first()
        )
        if invoice is None:
            return False

        if not InvoiceService._transfer_matches_current_payment(invoice, transfer):
            return False

        return InvoiceService.bind_transfer_to_invoice(
            invoice=invoice,
            transfer=transfer,
        )

    @staticmethod
    @transaction.atomic
    def try_match_vault_slot_initial_native_balance(
        *,
        transfer: Transfer,
        slot: VaultSlot,
        payment_datetime,
    ) -> bool:
        """匹配 VaultSlot 部署前打入 CREATE2 地址的 EVM 原生币账单付款。

        initialNativeBalance 是部署瞬间的余额快照，不携带真实付款时间与付款人；
        因此只能在已知部署任务、slot 业务身份和单账单占用约束下，用 TxTask.created_at
        作为这类付款事实的归因时间。
        """
        if slot.usage != VaultSlotUsage.INVOICE:
            return False
        if transfer.chain_id != slot.chain_id:
            return False
        if transfer.crypto_id != transfer.chain.native_coin.pk:
            return False
        if transfer.to_address != slot.address:
            return False

        base_filter = Invoice.objects.filter(
            project=slot.project,
            chain=transfer.chain,
            crypto=transfer.crypto,
            pay_address=slot.address,
            pay_amount=transfer.amount,
            started_at__lte=payment_datetime,
            expires_at__gte=payment_datetime,
            status__in=[InvoiceStatus.WAITING, InvoiceStatus.EXPIRED],
        ).filter(Q(transfer__isnull=True) | Q(transfer=transfer))

        candidate = base_filter.order_by("-started_at", "-pk").values("pk").first()
        if candidate is None:
            return False

        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("project", "crypto", "chain")
            .filter(pk=candidate["pk"])
            .first()
        )
        if invoice is None:
            return False
        if not InvoiceService.vault_slot_initial_native_balance_matches_invoice(
            invoice=invoice,
            transfer=transfer,
            slot=slot,
            payment_datetime=payment_datetime,
        ):
            return False

        return InvoiceService.bind_transfer_to_invoice(
            invoice=invoice,
            transfer=transfer,
        )

    @staticmethod
    def bind_transfer_to_invoice(*, invoice: Invoice, transfer: Transfer) -> bool:
        confirm_mode = (
            ConfirmMode.QUICK
            if invoice.project.fast_confirm_threshold > invoice.worth
            else ConfirmMode.FULL
        )
        transfer = TransferService.assign_type_and_mode(
            transfer, TransferType.Invoice, confirm_mode
        )

        # 观察到链上付款时只绑定 Transfer，账单业务状态仍保持 WAITING/EXPIRED。
        # 真正完成、归集、webhook、AML 等业务副作用统一等 Transfer.confirm() 后执行。
        Invoice.objects.filter(pk=invoice.pk).update(
            transfer_id=transfer.pk,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

        expire_invoice_public_cache_on_commit(invoice.sys_no)

        return True

    @staticmethod
    def vault_slot_initial_native_balance_matches_invoice(
        *,
        invoice: Invoice,
        transfer: Transfer,
        slot: VaultSlot,
        payment_datetime,
    ) -> bool:
        if invoice.status not in [InvoiceStatus.WAITING, InvoiceStatus.EXPIRED]:
            return False
        if invoice.transfer_id is not None and invoice.transfer_id != transfer.pk:
            return False
        if invoice.project_id != slot.project_id:
            return False
        if (
            invoice.crypto_id != transfer.crypto_id
            or invoice.chain_id != transfer.chain_id
        ):
            return False
        if invoice.pay_address != slot.address or transfer.to_address != slot.address:
            return False
        if not (invoice.started_at <= payment_datetime <= invoice.expires_at):
            return False
        return invoice.pay_amount == transfer.amount

    @staticmethod
    def _transfer_matches_current_payment(invoice: Invoice, transfer: Transfer) -> bool:
        if invoice.status not in [InvoiceStatus.WAITING, InvoiceStatus.EXPIRED]:
            return False
        if invoice.transfer_id is not None and invoice.transfer_id != transfer.pk:
            return False
        if (
            invoice.crypto_id != transfer.crypto_id
            or invoice.chain_id != transfer.chain_id
        ):
            return False
        if invoice.pay_address != transfer.to_address:
            return False
        if not (invoice.started_at <= transfer.datetime <= invoice.expires_at):
            return False
        # 账单合约收款要求 pay_amount 与到账金额精确相等：候选已在 try_match_invoice
        # 用精确金额选出，复核保持同一口径，杜绝溢付被误判匹配。
        # pay_amount 为 None 时 == 比较天然返回 False，无需单独判空。
        return invoice.pay_amount == transfer.amount

    @classmethod
    @transaction.atomic
    def confirm_invoice(
        cls,
        invoice: Invoice,
    ):
        # 必须在本方法内对 Invoice 加行锁，不能仅依赖调用方（Transfer.confirm）持有
        # Transfer 锁——其他调用路径可能绕开 Transfer 锁直接调用此方法。
        # of=("self",) 把锁限定在 invoices_invoice，避免连带锁 projects_project 引发死锁。
        invoice = (
            Invoice.objects.select_for_update(of=("self",))
            .select_related("project", "chain", "crypto", "transfer")
            .get(pk=invoice.pk)
        )
        if invoice.status == InvoiceStatus.COMPLETED:
            return
        if invoice.status not in [InvoiceStatus.WAITING, InvoiceStatus.EXPIRED]:
            raise InvoiceStatusError(f"Invoice must be payable, {invoice.sys_no}")
        if invoice.protocol == InvoiceProtocol.NATIVE and invoice.transfer_id is None:
            raise InvoiceStatusError(f"Invoice must bind transfer, {invoice.sys_no}")
        if (
            invoice.protocol == InvoiceProtocol.NATIVE
            and invoice.transfer.status != TransferStatus.CONFIRMED
        ):
            raise InvoiceStatusError(
                f"Invoice transfer must be confirmed, {invoice.sys_no}"
            )

        # 账单确认不依赖 save() 信号，直接 update 可减少并发覆盖面。
        Invoice.objects.filter(pk=invoice.pk).update(
            status=InvoiceStatus.COMPLETED,
            updated_at=timezone.now(),
        )
        invoice.refresh_from_db()

        expire_invoice_public_cache_on_commit(invoice.sys_no)

        cls.schedule_collection_if_needed(invoice)
        transaction.on_commit(lambda: screen_invoice_aml.delay(invoice.pk))

        if invoice.protocol == InvoiceProtocol.EPAY_V1:
            from .epay.service import EpaySubmitService

            EpaySubmitService.enqueue_paid_notify(invoice)
        elif invoice.protocol == InvoiceProtocol.NATIVE:
            WebhookService.create_event(
                project=invoice.project,
                payload=cls.build_webhook_payload(invoice),
                delivery_url=invoice.notify_url,
            )
        # 设计决策：开源版本不计算内部手续费或月成交量统计，
        # 账单状态机在 COMPLETED 即为终局，无需后续财务核算步骤。

        send_saas_callback(
            SaasCallback(
                event=CallbackEvent.INVOICE_CONFIRMED,
                appid=invoice.project.appid,
                sys_no=invoice.sys_no,
                worth=str(invoice.worth),
                currency=invoice.crypto.symbol,
            )
        )

    @staticmethod
    def schedule_collection_if_needed(invoice: Invoice) -> None:
        """按账单实际收款地址身份决定是否需要 VaultSlot 归集。"""
        if invoice.chain_id is None or not invoice.pay_address:
            return

        if VaultSlot.objects.filter(
            chain=invoice.chain,
            project=invoice.project,
            usage=VaultSlotUsage.INVOICE,
            address=invoice.pay_address,
        ).exists():
            try:
                InvoiceService.schedule_collect_for_invoice(invoice.pk)
            except Exception:
                logger.exception(
                    "调度 Invoice VaultSlot 归集任务失败",
                    invoice_id=invoice.pk,
                )
            return

        if DifferRecipientAddress.objects.filter(
            project=invoice.project,
            chain_type=invoice.chain.type,
            address=invoice.pay_address,
        ).exists():
            return

        logger.warning(
            "账单确认后无法识别收款地址类型，跳过归集调度",
            invoice_id=invoice.pk,
            chain=invoice.chain.code,
            pay_address=invoice.pay_address,
        )

    @staticmethod
    def schedule_collect_for_invoice(
        invoice_pk: int,
    ) -> VaultSlotCollectSchedule | None:
        """按账单收款地址定位其 VaultSlot，登记（或复用）该槽位该币种的待归集计划。"""
        invoice = Invoice.objects.select_related(
            "project",
            "chain",
            "crypto",
        ).get(pk=invoice_pk)

        if (
            invoice.chain_id is None
            or invoice.crypto_id is None
            or not invoice.pay_address
        ):
            return None

        chain = invoice.chain
        crypto = invoice.crypto

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

        return schedule_collect_for_slot(chain=chain, crypto=crypto, slot=slot)
