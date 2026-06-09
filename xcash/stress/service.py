# xcash/stress/service.py
import hashlib
import hmac
import json
import random
import re
import time
from datetime import timedelta
from decimal import Decimal

import httpx
import structlog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from common.consts import APPID_HEADER
from common.consts import NONCE_HEADER
from common.consts import SIGNATURE_HEADER
from common.consts import TIMESTAMP_HEADER
from invoices.models import Invoice
from projects.models import Project

from .models import DepositStressCase
from .models import InvoiceStressCase
from .models import StressRun
from .models import StressRunStatus

logger = structlog.get_logger()

# 账单压测只使用本地 ERC20。当前 EVM 外部入账 scanner 基于日志：
# ERC20 Transfer 可稳定观测；发往未部署 VaultSlot 的 ETH 没有 native receive 事件，
# 不能作为账单压测支付方式。
STRESS_FIXED_METHODS = {
    "USDT": ["anvil"],
}
STRESS_FIXED_METHOD_CHOICES = (
    ("USDT", "anvil"),
)
STRESS_SAAS_PERMISSION_CACHE_TTL = 24 * 60 * 60


class StressService:
    @staticmethod
    def prepare(stress: StressRun) -> None:
        """创建专用 Project 和批量测试 Case。由 Celery 任务异步调用。"""
        if stress.status != StressRunStatus.PREPARING:
            return

        with transaction.atomic():
            _ensure_stress_crypto_prices()
            _cleanup_orphan_stress_project(stress)
            project = _create_stress_project(stress)
            cases = _build_stress_cases(stress)

            # 充币和账单都需要 Wallet + EVM Vault。
            if stress.deposit_count > 0 or stress.count > 0:
                _setup_wallet_for_vault(project)

            stress.project = project
            stress.error = ""
            stress.finished_at = None
            stress.save(update_fields=["project", "error", "finished_at"])

            InvoiceStressCase.objects.bulk_create(cases)

            if stress.deposit_count > 0:
                dep_cases = _build_deposit_cases(stress)
                DepositStressCase.objects.bulk_create(dep_cases)

            stress.status = StressRunStatus.READY
            stress.save(update_fields=["status"])

        # Vault 注资在事务提交后执行，确保数据库记录已落库
        if stress.deposit_count > 0 or stress.count > 0:
            _fund_vault_for_stress(stress.project)

        logger.info(
            "stress.prepared",
            stress_id=stress.pk,
            count=stress.count,
            deposit_count=stress.deposit_count,
        )

    @staticmethod
    def start(stress: StressRun) -> None:
        """触发测试执行。"""
        from .evm import sync_chain_clock
        from .tasks import execute_deposit_case
        from .tasks import execute_stress_case
        from .tasks import finalize_stress_timeout
        from .tasks import verify_deposit_collection

        # 每轮压测启动时把 Anvil 链时钟拉齐到系统时间，避免 block.timestamp
        # 漂移导致 try_match_invoice 的时间窗口过滤失败。详见 evm.sync_chain_clock。
        sync_chain_clock()

        stress.status = StressRunStatus.RUNNING
        stress.started_at = timezone.now()
        stress.save(update_fields=["status", "started_at"])

        max_offset = 0.0
        for case in stress.cases.all().only("id", "scheduled_offset"):
            eta = stress.started_at + timedelta(seconds=case.scheduled_offset)
            execute_stress_case.apply_async(args=[case.pk], eta=eta)
            max_offset = max(max_offset, case.scheduled_offset)

        for case in stress.deposit_cases.all().only("id", "scheduled_offset"):
            eta = stress.started_at + timedelta(seconds=case.scheduled_offset)
            execute_deposit_case.apply_async(args=[case.pk], eta=eta)
            max_offset = max(max_offset, case.scheduled_offset)

        # 合约账单归集验证兜底：max_offset + 20 分钟
        # webhook 触发是主路径，此处是安全网，verify_invoice_collection 幂等。
        if stress.count > 0:
            from .tasks import verify_invoice_collection

            invoice_eta = stress.started_at + timedelta(seconds=max_offset + 20 * 60)
            verify_invoice_collection.apply_async(args=[stress.pk], eta=invoice_eta)

        # 充币归集验证兜底：最大调度偏移 + 20 分钟
        # webhook 触发是主路径，这里是安全网，verify_deposit_collection 本身幂等。
        if stress.deposit_count > 0:
            collection_eta = stress.started_at + timedelta(seconds=max_offset + 20 * 60)
            verify_deposit_collection.apply_async(args=[stress.pk], eta=collection_eta)

        # 兜底超时：最大调度偏移 + 30 分钟
        timeout_seconds = max_offset + 30 * 60
        finalize_stress_timeout.apply_async(
            args=[stress.pk],
            eta=stress.started_at + timedelta(seconds=timeout_seconds),
        )

        logger.info("stress.started", stress_id=stress.pk)

    @staticmethod
    def on_case_finished(case) -> None:
        """InvoiceStressCase 或 DepositStressCase 进入终态后，更新 StressRun 统计并检查是否全部完成。

        使用 select_for_update 锁定 StressRun 行，在同一事务内完成
        计数递增、终态判定和状态更新，防止并发 worker 竞态。
        """
        with transaction.atomic():
            stress_run = StressRun.objects.select_for_update().get(
                pk=case.stress_run_id
            )

            if case.status == "failed":
                stress_run.failed += 1
            elif case.status == "succeeded":
                stress_run.succeeded += 1
            elif case.status == "skipped":
                stress_run.skipped += 1

            update_fields = ["succeeded", "failed", "skipped"]

            total_expected = stress_run.count + stress_run.deposit_count
            if stress_run.total_finished >= total_expected:
                stress_run.status = StressRunStatus.COMPLETED
                stress_run.finished_at = timezone.now()
                update_fields += ["status", "finished_at"]
                logger.info(
                    "stress.completed",
                    stress_run_id=stress_run.pk,
                    succeeded=stress_run.succeeded,
                    failed=stress_run.failed,
                )

            stress_run.save(update_fields=update_fields)

    @staticmethod
    def _build_hmac_headers(project: Project, body_str: str) -> dict[str, str]:
        """构造带 HMAC 签名的 API 请求头。"""
        nonce = hashlib.md5(  # noqa: S324
            f"{time.time()}{random.random()}".encode()  # noqa: S311
        ).hexdigest()
        timestamp = str(int(timezone.now().timestamp()))
        message = f"{nonce}{timestamp}{body_str}"
        signature = hmac.new(
            project.hmac_key.encode(), message.encode(), hashlib.sha256
        ).hexdigest()

        return {
            "Content-Type": "application/json",
            APPID_HEADER: project.appid,
            NONCE_HEADER: nonce,
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: signature,
        }

    @staticmethod
    def create_invoice(case: InvoiceStressCase) -> dict:
        """调用 API 创建 Invoice，仅使用 Stress Project 已准备好的本地链 methods。"""
        stress_run = case.stress_run
        project = stress_run.project
        methods = _require_stress_methods_ready(project)

        amount = str(round(random.uniform(1, 10), 2))  # noqa: S311
        out_no = f"STRESS-{stress_run.pk}-{case.sequence}"

        body = json.dumps(
            {
                "out_no": out_no,
                "title": f"Stress Test #{case.sequence}",
                "currency": "USD",
                "amount": amount,
                "duration": 30,
                "methods": methods,
            }
        )

        headers = StressService._build_hmac_headers(project, body)
        base_url = settings.STRESS_WEBHOOK_BASE_URL
        url = f"{base_url}/v1/invoice"

        resp = httpx.post(url, content=body, headers=headers, timeout=10)
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise RuntimeError(f"创建 Invoice API {resp.status_code}: {detail}")
        return resp.json()

    @staticmethod
    def select_method(case: InvoiceStressCase) -> dict:
        """调用 API 选择支付方式，只在压测固定的本地链组合中选择。"""
        stress_run = case.stress_run
        project = stress_run.project
        base_url = settings.STRESS_WEBHOOK_BASE_URL

        crypto_symbol, chain_code = random.choice(  # noqa: S311
            STRESS_FIXED_METHOD_CHOICES
        )

        body = json.dumps({"crypto": crypto_symbol, "chain": chain_code})
        url = f"{base_url}/v1/invoice/{case.invoice_sys_no}/select-method"

        resp = httpx.post(
            url,
            content=body,
            headers={
                "Content-Type": "application/json",
                APPID_HEADER: project.appid,
            },
            timeout=10,
        )
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise RuntimeError(f"选择支付方式 API {resp.status_code}: {detail}")
        return resp.json()

    @staticmethod
    def ensure_deposit_address(case: DepositStressCase) -> str:
        """调用 API 获取客户充值地址。"""
        project = case.stress_run.project
        base_url = settings.STRESS_WEBHOOK_BASE_URL
        url = f"{base_url}/v1/deposit/address"

        # GET 请求 body 为空，HMAC 签名仍需携带
        headers = StressService._build_hmac_headers(project, "")
        resp = httpx.get(
            url,
            params={
                "uid": case.customer_uid,
                "chain": case.chain,
                "crypto": case.crypto,
            },
            headers=headers,
            timeout=60,
        )
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            raise RuntimeError(f"获取充值地址 API {resp.status_code}: {detail}")
        return resp.json()["deposit_address"]

# 压测用的法币价格兜底值，仅在 prices 为空时回填，避免依赖外部行情源。
_STRESS_FALLBACK_PRICES = {
    "ETH": {"USD": 3000},
}


def _ensure_stress_crypto_prices() -> None:
    """确保压测币种有可用的 USD 价格，避免 select_method 法币换算时 KeyError。"""
    from currencies.models import Crypto

    for symbol, fallback in _STRESS_FALLBACK_PRICES.items():
        try:
            crypto = Crypto.objects.get(symbol=symbol)
        except Crypto.DoesNotExist:
            continue
        if "USD" not in crypto.prices:
            crypto.prices.update(fallback)
            crypto.save(update_fields=["prices"])
            logger.info("stress.backfill_price", symbol=symbol, prices=crypto.prices)


def _cleanup_orphan_stress_project(stress: StressRun) -> None:
    """清理 prepare 历史失败后遗留的未绑定专用 Project。"""
    if stress.project_id is not None:
        return
    Project.objects.filter(
        name=f"Stress-{stress.pk}",
        stressrun__isnull=True,
    ).delete()


def _create_stress_project(stress: StressRun) -> Project:
    """创建当前 StressRun 专用 Project。"""
    webhook_url = f"{settings.STRESS_WEBHOOK_BASE_URL}/stress/webhook"
    project = Project.objects.create(
        name=f"Stress-{stress.pk}",
        webhook=webhook_url,
        ip_white_list="*",
        active=True,
        # 设置极大阈值使所有 Invoice 都走 QUICK 确认模式，
        # 因为 Anvil 本地测试链不会自动产生新区块，FULL 模式无法完成确认。
        fast_confirm_threshold=Decimal("99999999"),
    )
    transaction.on_commit(lambda: _seed_stress_saas_permission_cache(project))
    return project


def _seed_stress_saas_permission_cache(project: Project) -> None:
    """为压测专用 Project 预置 SaaS 权限缓存，避免业务压测打到 SaaS 权限服务。"""
    perm = {
        "appid": project.appid,
        "frozen": False,
        "enable_deposit": True,
    }
    cache_key = f"saas:permission:{project.appid}"
    cache.set(f"{cache_key}:stale", perm, STRESS_SAAS_PERMISSION_CACHE_TTL)
    cache.set(cache_key, perm, STRESS_SAAS_PERMISSION_CACHE_TTL)


def _setup_wallet_for_vault(project: Project) -> None:
    """为 Stress Project 分配系统钱包派生的独立 EVM vault 地址。

    合约账单（CONTRACT）依赖 project.evm_vault：它既是 CREATE2 派生 VaultSlot 的不可变归集地址，
    也是 VaultSlot 归集（sweep）的最终终点。Project.evm_vault 有唯一约束，不能让多轮 StressRun
    复用 address_index=0 的系统热钱包地址；压测专用 vault 使用系统钱包按 project.pk 派生的地址，
    仍由主系统托管私钥。address_index=0 继续作为系统热钱包支付 VaultSlot 部署与归集 gas。
    Project.evm_vault 一旦写入不可修改（见 Project.save 校验），故只在本次首次准备时赋值。
    """
    from chains.models import AddressUsage
    from chains.models import ChainType
    from core.models import SystemWallet

    system_wallet = SystemWallet.get_current()
    if project.pk is None:
        raise RuntimeError("Stress Project 必须先落库，才能按项目 ID 派生 vault 地址")

    evm_vault_address = system_wallet.wallet.get_address(
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        address_index=project.pk,
    ).address
    project.evm_vault = evm_vault_address
    project.save(update_fields=["evm_vault"])


def _build_stress_cases(stress: StressRun) -> list[InvoiceStressCase]:
    """构建本轮待执行的 InvoiceStressCase 列表。"""
    total_seconds = stress.count / 10.0
    mu = total_seconds / 2
    sigma = total_seconds / 6

    cases = []
    for i in range(1, stress.count + 1):
        offset = max(0.0, min(total_seconds, random.gauss(mu, sigma)))
        cases.append(
            InvoiceStressCase(
                stress_run=stress,
                sequence=i,
                scheduled_offset=offset,
            )
        )

    random.shuffle(cases)
    for idx, case in enumerate(cases, 1):
        case.sequence = idx
    return cases


STRESS_DEPOSIT_METHOD_CHOICES = (
    ("ETH", "anvil"),
    ("USDT", "anvil"),
)

_DEPOSIT_AMOUNT_RANGES = {
    "ETH": (Decimal("0.001"), Decimal("0.05")),
    "USDT": (Decimal("1"), Decimal("100")),
}


def _sample_decimal_amount(
    *, lo: Decimal, hi: Decimal, decimal_places: int = 8
) -> Decimal:
    """按固定小数精度采样金额，避免 float/uniform 路径引入尾差。"""
    scale = 10**decimal_places
    lo_units = int(lo * scale)
    hi_units = int(hi * scale)
    sampled_units = random.randint(lo_units, hi_units)  # noqa: S311
    return Decimal(sampled_units).scaleb(-decimal_places)


def _build_deposit_cases(stress: StressRun) -> list[DepositStressCase]:
    """构建本轮待执行的 DepositStressCase 列表。

    将 deposit_count 均匀分配到 deposit_customer_count 个客户，
    每个 case 随机选择 ETH 或 USDT on anvil。
    """
    customer_count = stress.deposit_customer_count
    deposit_count = stress.deposit_count

    # 生成客户 UID 列表
    customer_uids = [f"STRESS-{stress.pk}-C{i}" for i in range(customer_count)]

    # 均匀分配充值次数到各客户
    base_per_customer = deposit_count // customer_count
    remainder = deposit_count % customer_count
    customer_deposits = []
    for i in range(customer_count):
        n = base_per_customer + (1 if i < remainder else 0)
        customer_deposits.extend([customer_uids[i]] * n)

    total_seconds = deposit_count / 10.0
    mu = total_seconds / 2
    sigma = total_seconds / 6

    cases = []
    for i, uid in enumerate(customer_deposits, 1):
        offset = max(0.0, min(total_seconds, random.gauss(mu, sigma)))
        crypto_symbol, chain_code = random.choice(STRESS_DEPOSIT_METHOD_CHOICES)  # noqa: S311
        lo, hi = _DEPOSIT_AMOUNT_RANGES[crypto_symbol]
        amount = _sample_decimal_amount(lo=lo, hi=hi, decimal_places=8)

        cases.append(
            DepositStressCase(
                stress_run=stress,
                sequence=i,
                scheduled_offset=offset,
                customer_uid=uid,
                crypto=crypto_symbol,
                chain=chain_code,
                amount=amount,
            )
        )

    random.shuffle(cases)
    for idx, case in enumerate(cases, 1):
        case.sequence = idx
    return cases


def _require_stress_methods_ready(project: Project) -> dict[str, list[str]]:
    """校验本地链合约账单 methods 已完整可用。

    项目可以支持更多 methods；账单压测只返回 STRESS_FIXED_METHODS，避免生成当前
    scanner 无法观测的 ETH 账单。
    """
    methods = Invoice.available_methods(project)
    for crypto_symbol, required_chains in STRESS_FIXED_METHODS.items():
        available_chains = set(methods.get(crypto_symbol, []))
        if not set(required_chains).issubset(available_chains):
            raise RuntimeError(
                "Stress Project 收款地址未准备完整，必须支持 USDT 本地链支付"
            )
    return STRESS_FIXED_METHODS


def _extract_error_detail(resp: httpx.Response) -> str:
    """从 API 错误响应中提取关键信息。

    JSON 响应直接返回；Django HTML 调试页提取异常类型和 traceback 摘要。
    """
    text = resp.text
    try:
        return json.dumps(resp.json(), ensure_ascii=False)[:2000]
    except Exception:
        logger.debug("Response is not valid JSON, fallback to HTML extraction")
    # Django debug HTML: 提取 <title> 和 <pre class="exception_value">
    title = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
    exc_value = re.search(r'class="exception_value">(.*?)</pre>', text, re.DOTALL)
    # 兜底提取所有 <pre> 块中的 traceback 片段
    tracebacks = re.findall(r"<pre[^>]*>(.*?)</pre>", text, re.DOTALL)
    parts = []
    if title:
        parts.append(title.group(1).strip())
    if exc_value:
        parts.append(exc_value.group(1).strip())
    if not exc_value and tracebacks:
        # 取最后一个 <pre> 往往是 traceback 摘要
        parts.append(tracebacks[-1].strip()[:1000])
    return " | ".join(parts)[:2000] if parts else text[:2000]


def _fund_vault_for_stress(project: Project) -> None:
    """为 Stress Project 的 EVM Vault 地址注入测试币。

    在 prepare 事务提交后调用，确保 Wallet 和 Address 记录已落库。
    """
    _fund_evm_vault(project)


def _fund_evm_vault(project: Project) -> None:
    """EVM 本地压测注资：项目 vault 收测试资产，系统热钱包支付 VaultSlot 部署 gas。"""
    from web3 import Web3

    from chains.models import Address
    from chains.models import AddressUsage
    from chains.models import Chain
    from chains.models import ChainType
    from core.models import SystemWallet
    from currencies.models import Crypto
    from evm.local_erc20 import LOCAL_EVM_ERC20_ABI

    from .evm import _get_w3
    from .evm import _require_contract
    from .evm import _set_balance

    vault_address = project.evm_vault
    if not vault_address:
        raise RuntimeError("Stress Project Vault 地址未配置，无法注资")

    w3 = _get_w3()

    # 1. 注入 10000 ETH（覆盖归集 gas 和本地压测资产）
    eth_amount_wei = 10000 * 10**18
    _set_balance(w3, vault_address, eth_amount_wei)
    logger.info("stress.vault.evm_eth_funded", vault=vault_address, amount_eth=10000)

    system_wallet = SystemWallet.get_current()
    system_hot = Address.objects.filter(
        wallet=system_wallet.wallet,
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        address_index=0,
    ).first()
    if system_hot is None:
        system_hot = system_wallet.wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )
    system_hot_address = system_hot.address
    _set_balance(w3, system_hot_address, eth_amount_wei)
    logger.info(
        "stress.system_wallet.evm_eth_funded",
        address=system_hot_address,
        amount_eth=10000,
    )

    # 2. 为 Vault 铸造 USDT
    # 直接用本地 anvil 链：其 USDT CryptoOnChain 指向本地部署的 mock 合约，mint 才能在本地 RPC 生效。
    evm_chain = Chain.objects.get(code="anvil")
    usdt = Crypto.objects.get(symbol="USDT")
    usdt_contract_address = usdt.address(evm_chain)
    if not usdt_contract_address:
        raise RuntimeError("USDT 在本地 anvil 链上没有合约地址，无法为 Vault 铸币")

    checksum_token = _require_contract(w3, usdt_contract_address)
    contract = w3.eth.contract(address=checksum_token, abi=LOCAL_EVM_ERC20_ABI)

    usdt_decimals = usdt.get_decimals(evm_chain)
    mint_amount = int(Decimal("10000000") * Decimal(10**usdt_decimals))

    # 用临时账户签名 mint 交易（本地测试合约的 mint 是公开方法）
    minter = w3.eth.account.create()
    _set_balance(w3, minter.address, 10**17)  # 0.1 ETH gas

    mint_tx = contract.functions.mint(
        Web3.to_checksum_address(vault_address), mint_amount
    ).build_transaction(
        {
            "from": minter.address,
            "nonce": w3.eth.get_transaction_count(minter.address, "pending"),
            "gasPrice": w3.eth.gas_price,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = minter.sign_transaction(mint_tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    logger.info(
        "stress.vault.evm_usdt_funded",
        vault=vault_address,
        amount="10000000",
        tx_hash=tx_hash.hex(),
    )
