from __future__ import annotations

import shutil
import subprocess
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.cache import cache as _cache
from django.core.management import call_command
from django.test import SimpleTestCase
from django.test import TestCase
from tron.models import TronTxTask
from web3 import Web3

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from chains.tests_fixtures import make_evm_chain
from chains.tests_fixtures import make_tron_chain
from core.dashboard_metrics import build_dashboard_metrics
from core.default_data import ensure_base_currencies
from core.default_data import ensure_local_chains
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from core.models import SystemSettings
from core.models import SystemWallet
from core.monitoring import OperationalRiskService
from core.runtime_settings import get_webhook_delivery_breaker_threshold
from core.runtime_settings import get_webhook_delivery_max_backoff_seconds
from core.runtime_settings import get_webhook_delivery_max_retries
from currencies.models import CryptoOnChain
from evm.local_erc20 import LOCAL_EVM_ERC20_ABI
from evm.local_erc20 import LOCAL_EVM_ERC20_BYTECODE
from evm.models import EvmTxTask
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from invoices.models import Invoice
from invoices.models import InvoiceStatus
from projects.models import Project


def setUpModule():
    # core 真实链路会用到账户锁；每轮开始前清掉测试 Redis，避免前序 run 遗留锁串扰。
    # 地址派生与签名已在 chains 内部闭环，测试直接走真实派生，无需 mock 外部 signer。
    _cache.clear()


def tearDownModule():
    _cache.clear()


class SystemSettingsRuntimeTests(TestCase):
    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    def test_runtime_settings_use_database_override_before_settings_fallback(self):
        # 系统运行参数中心存在记录时，业务读取应优先采用数据库值，而不是继续回退到 settings 常量。
        SystemSettings.objects.create(
            webhook_delivery_breaker_threshold=12,
            webhook_delivery_max_retries=9,
            webhook_delivery_max_backoff_seconds=45,
        )

        self.assertEqual(get_webhook_delivery_breaker_threshold(), 12)
        self.assertEqual(get_webhook_delivery_max_retries(), 9)
        self.assertEqual(get_webhook_delivery_max_backoff_seconds(), 45)


class SystemWalletTests(TestCase):
    def test_get_current_creates_single_system_wallet(self):
        system_wallet = SystemWallet.get_current()

        self.assertIsNotNone(system_wallet.wallet_id)
        self.assertEqual(SystemWallet.objects.count(), 1)
        self.assertEqual(Wallet.objects.count(), 1)

        same_system_wallet = SystemWallet.get_current()

        self.assertEqual(same_system_wallet.pk, system_wallet.pk)
        self.assertEqual(same_system_wallet.wallet_id, system_wallet.wallet_id)
        self.assertEqual(SystemWallet.objects.count(), 1)
        self.assertEqual(Wallet.objects.count(), 1)


class OperationalRiskResourceTests(TestCase):
    def test_evm_low_native_balance_alerts_when_sender_cannot_cover_queue(self):
        chain = make_evm_chain(code=ChainCode.Ethereum)
        w3 = SimpleNamespace(
            eth=SimpleNamespace(
                gas_price=10,
                get_balance=lambda _address: 1_000,
            )
        )
        wallet = Wallet.objects.create()
        sender = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address("0x" + "66" * 20),
        )
        base_task = TxTask.objects.create(
            sender=sender,
            chain=chain,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        EvmTxTask.objects.create(
            base_task=base_task,
            sender=sender,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "67" * 20),
            value=0,
            gas=100,
            gas_price=10,
        )

        with patch.object(Chain, "_build_w3", return_value=w3):
            alerts = OperationalRiskService.evm_low_native_balance_alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["current_balance"], 1_000)
        self.assertEqual(alerts[0]["required_balance"], 2_000)
        self.assertEqual(alerts[0]["sender"], sender)

    def test_tron_low_resource_alerts_when_energy_is_below_pending_queue(self):
        chain = make_tron_chain()
        wallet = Wallet.objects.create()
        sender = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.TRON,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )
        base_task = TxTask.objects.create(
            sender=sender,
            chain=chain,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.QUEUED,
        )
        TronTxTask.objects.create(
            base_task=base_task,
            sender=sender,
            chain=chain,
            to="TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb",
            function_selector="collect(address)",
            parameter="0" * 64,
            fee_limit=150_000_000,
        )

        with (
            patch("tron.client.TronHttpClient") as client_class,
            patch("tron.resources.estimate_contract_call_energy", return_value=100),
        ):
            client_class.return_value.get_account_resource.return_value = {
                "EnergyLimit": 100,
                "EnergyUsed": 95,
                "freeNetLimit": 0,
                "freeNetUsed": 0,
                "NetLimit": 0,
                "NetUsed": 0,
            }
            alerts = OperationalRiskService.tron_low_resource_alerts()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["available_energy"], 5)
        self.assertEqual(alerts[0]["required_energy"], 120)
        self.assertEqual(alerts[0]["sender"], sender)


class EnvironmentBadgeResourceRiskTests(TestCase):
    """badge 的资源风险来自异步巡检写入的缓存，而非渲染时实时 RPC。

    回归守卫：修复前 badge 调 build_summary(limit=0)，evm/tron 计数恒为 0，
    资源告警是死代码——badge 永远不会因资源风险变红。
    """

    def setUp(self):
        _cache.clear()

    def tearDown(self):
        _cache.clear()

    def test_cached_resource_risk_counts_round_trip(self):
        self.assertEqual(
            OperationalRiskService.cached_resource_risk_counts(),
            {"evm_low_native_balance_count": 0, "tron_low_resource_count": 0},
        )
        OperationalRiskService.cache_resource_risk_counts(
            evm_low_native_balance_count=2,
            tron_low_resource_count=3,
        )
        self.assertEqual(
            OperationalRiskService.cached_resource_risk_counts(),
            {"evm_low_native_balance_count": 2, "tron_low_resource_count": 3},
        )

    def test_badge_flags_danger_from_cached_resource_risk(self):
        from django.test import RequestFactory

        from core.dashboard import environment_callback

        # 无 webhook 堆积，但缓存里有 EVM 资源风险 -> badge 必须告警。
        OperationalRiskService.cache_resource_risk_counts(
            evm_low_native_balance_count=1,
            tron_low_resource_count=0,
        )
        request = RequestFactory().get("/")

        badge = environment_callback(request)

        self.assertEqual(badge[1], "danger")

    def test_badge_normal_without_any_risk(self):
        from django.test import RequestFactory

        from core.dashboard import environment_callback

        request = RequestFactory().get("/")

        badge = environment_callback(request)

        self.assertEqual(badge[1], "success")


class DashboardMetricsTests(TestCase):
    def make_invoice(
        self,
        *,
        project,
        out_no,
        worth,
        status,
        created_at,
        updated_at,
    ):
        invoice = Invoice.objects.create(
            project=project,
            out_no=out_no,
            title="Dashboard metric test",
            currency="USD",
            amount=Decimal(worth),
            worth=Decimal(worth),
            status=status,
            expires_at=created_at + timedelta(hours=1),
        )
        Invoice.objects.filter(pk=invoice.pk).update(
            created_at=created_at,
            started_at=created_at,
            updated_at=updated_at,
        )
        return invoice

    @patch("core.dashboard_metrics.timezone.localdate")
    @patch("core.dashboard_metrics.timezone.now")
    def test_completed_metrics_use_completion_time_not_creation_time(
        self,
        timezone_now,
        timezone_localdate,
    ):
        fixed_now = datetime(2026, 6, 11, 12, tzinfo=UTC)
        timezone_now.return_value = fixed_now
        timezone_localdate.return_value = fixed_now.date()
        project = Project.objects.create(name="Dashboard Project")

        # 旧账单今天才完成：必须计入今日/30日成交额，但不属于近30日创建 cohort。
        self.make_invoice(
            project=project,
            out_no="old-completed-today",
            worth="100",
            status=InvoiceStatus.COMPLETED,
            created_at=datetime(2026, 5, 1, tzinfo=UTC),
            updated_at=datetime(2026, 6, 11, 8, tzinfo=UTC),
        )
        # 近30日创建且已完成：计入成交额，也计入转化率分子。
        self.make_invoice(
            project=project,
            out_no="created-and-completed",
            worth="25",
            status=InvoiceStatus.COMPLETED,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            updated_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        self.make_invoice(
            project=project,
            out_no="created-waiting",
            worth="50",
            status=InvoiceStatus.WAITING,
            created_at=datetime(2026, 6, 11, 9, tzinfo=UTC),
            updated_at=datetime(2026, 6, 11, 9, tzinfo=UTC),
        )
        self.make_invoice(
            project=project,
            out_no="created-expired-today",
            worth="75",
            status=InvoiceStatus.EXPIRED,
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            updated_at=datetime(2026, 6, 11, 10, tzinfo=UTC),
        )

        metrics = build_dashboard_metrics()
        snapshot = metrics["snapshot"]

        self.assertEqual(snapshot["today_completed_count"], 1)
        self.assertEqual(snapshot["today_completed_worth"], Decimal("100"))
        self.assertEqual(snapshot["rolling_30d_completed_count"], 2)
        self.assertEqual(snapshot["rolling_30d_completed_worth"], Decimal("125"))
        self.assertEqual(snapshot["created_30d_count"], 3)
        self.assertEqual(snapshot["conversion_rate_30d"], Decimal("33.3"))

        today_chart = metrics["chart_rows"][-1]
        self.assertEqual(today_chart["created_count"], 1)
        self.assertEqual(today_chart["completed_count"], 1)
        self.assertEqual(today_chart["expired_count"], 1)
        self.assertEqual(today_chart["completed_worth"], Decimal("100"))

        self.assertEqual(metrics["top_projects"][0]["gmv"], Decimal("125"))
        self.assertEqual(metrics["top_projects"][0]["completed_orders"], 2)
        self.assertEqual(metrics["top_projects"][0]["total_orders"], 3)
        self.assertEqual(metrics["top_projects"][0]["conversion_completed_orders"], 1)


@pytest.mark.skip(
    reason="Out of scope, follow-up: 依赖 core/default_data.py init_local_chains 命令的旧字段链路，待 Task 7 重写"
)
class LocalChainBootstrapCommandTests(TestCase):
    def _require_local_evm(self) -> Web3:
        w3 = Web3(
            Web3.HTTPProvider(
                "http://127.0.0.1:8545",
                request_kwargs={"timeout": 5},
            )
        )
        if not w3.is_connected():
            self.skipTest("本地 anvil 未启动，跳过本地链初始化部署测试")
        return w3

    @patch.dict(
        environ,
        {
            "LOCAL_EVM_CHAIN_CODE": "ethereum-local",
            "LOCAL_EVM_CHAIN_NAME": "Ethereum Local",
            "LOCAL_EVM_RPC": "http://127.0.0.1:8545",
            "LOCAL_EVM_CHAIN_ID": "31337",
        },
        clear=False,
    )
    def test_init_local_chains_creates_local_chain_records(self):
        # 本地链初始化必须独立于生产 init，直接生成本地 Ethereum 配置与原生币映射。
        call_command("init_local_chains")

        evm_chain = Chain.objects.get(code="ethereum-local")

        self.assertEqual(evm_chain.type, ChainType.EVM)
        self.assertEqual(evm_chain.chain_id, 31337)
        self.assertEqual(evm_chain.confirm_block_count, 1)
        self.assertTrue(
            CryptoOnChain.objects.filter(
                chain=evm_chain,
                crypto__symbol="ETH",
                address="",
            ).exists()
        )

    @patch.dict(
        environ,
        {
            "LOCAL_EVM_CHAIN_CODE": "ethereum-local",
            "LOCAL_EVM_CHAIN_NAME": "Ethereum Local",
            "LOCAL_EVM_RPC": "http://127.0.0.1:8545",
            "LOCAL_EVM_CHAIN_ID": "31337",
            "LOCAL_EVM_USDT_ADDRESS": "",
        },
        clear=False,
    )
    def test_init_local_chains_deploys_local_usdt_and_creates_crypto_on_chain(self):
        w3 = self._require_local_evm()

        call_command("init_local_chains")

        evm_chain = Chain.objects.get(code="ethereum-local")
        usdt_mapping = CryptoOnChain.objects.get(
            chain=evm_chain,
            crypto__symbol="USDT",
        )

        self.assertTrue(Web3.is_address(usdt_mapping.address))
        self.assertEqual(usdt_mapping.decimals, 6)
        self.assertGreater(len(w3.eth.get_code(usdt_mapping.address)), 0)

    @patch.dict(
        environ,
        {
            "LOCAL_EVM_CHAIN_CODE": "ethereum-local",
            "LOCAL_EVM_CHAIN_NAME": "Ethereum Local",
            "LOCAL_EVM_RPC": "http://127.0.0.1:8545",
            "LOCAL_EVM_CHAIN_ID": "31337",
            "LOCAL_EVM_USDT_ADDRESS": "",
        },
        clear=False,
    )
    def test_init_local_chains_deploys_standard_erc20_usdt(self):
        w3 = self._require_local_evm()

        call_command("init_local_chains")

        usdt_mapping = CryptoOnChain.objects.get(
            chain__code="ethereum-local",
            crypto__symbol="USDT",
        )
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(usdt_mapping.address),
            abi=LOCAL_EVM_ERC20_ABI,
        )
        mint_hash = contract.functions.mint(
            w3.eth.accounts[0],
            1_000_000,
        ).transact({"from": w3.eth.accounts[0]})
        w3.eth.wait_for_transaction_receipt(mint_hash)
        transfer_hash = contract.functions.transfer(
            w3.eth.accounts[1],
            250_000,
        ).transact({"from": w3.eth.accounts[0]})
        receipt = w3.eth.wait_for_transaction_receipt(transfer_hash)

        self.assertGreaterEqual(len(receipt["logs"]), 1)
        self.assertEqual(
            Web3.to_hex(receipt["logs"][0]["topics"][0]),
            ERC20_TRANSFER_TOPIC0,
        )

    @patch.dict(
        environ,
        {
            "LOCAL_EVM_CHAIN_CODE": "ethereum-local",
            "LOCAL_EVM_CHAIN_NAME": "Ethereum Local",
            "LOCAL_EVM_RPC": "http://127.0.0.1:8545",
            "LOCAL_EVM_CHAIN_ID": "31337",
        },
        clear=False,
    )
    @patch("core.default_data.ensure_local_evm_usdt_contract_address")
    def test_init_local_chains_rolls_back_db_when_local_usdt_deploy_fails(
        self,
        ensure_local_usdt_contract_address,
    ):
        ensure_local_usdt_contract_address.side_effect = RuntimeError("deploy failed")
        Chain.objects.filter(code="ethereum-local").delete()

        ensure_base_currencies()
        with self.assertRaisesMessage(RuntimeError, "deploy failed"):
            ensure_local_chains()

        self.assertFalse(Chain.objects.filter(code="ethereum-local").exists())
        self.assertFalse(
            CryptoOnChain.objects.filter(chain__code="ethereum-local").exists()
        )


class InitEnvScriptTests(SimpleTestCase):
    # 地址派生与签名已在主系统内部闭环：钱包助记词加密密钥随主应用 .env 一起加载，
    # 不再有独立 .env.signer。该密钥一旦生成必须稳定不变（改动即等同种子失守）。
    def run_init_env(self, tmp_path):
        """把 init_env.sh 复制进临时 scripts/ 并以 tmp_path 为项目根执行。"""
        repo_root = Path(__file__).resolve().parents[2]
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(exist_ok=True)
        copied_script_path = scripts_dir / "init_env.sh"
        shutil.copy2(repo_root / "scripts" / "init_env.sh", copied_script_path)
        copied_script_path.chmod(0o755)
        return subprocess.run(  # noqa: S603
            [str(copied_script_path)],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )

    @staticmethod
    def parse_env(path: Path) -> dict:
        return dict(
            line.split("=", maxsplit=1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )

    def test_generates_env_with_wallet_mnemonic_key(self):
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            result = self.run_init_env(tmp_path)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            env_path = tmp_path / ".env"
            self.assertTrue(env_path.exists())
            # 不再生成独立 signer 环境文件
            self.assertFalse((tmp_path / ".env.signer").exists())

            env = self.parse_env(env_path)
            # 随机密钥按约定长度生成
            self.assertEqual(len(env["DJANGO_SECRET_KEY"]), 64)
            self.assertEqual(len(env["POSTGRES_PASSWORD"]), 32)
            self.assertRegex(env["DJANGO_SECRET_KEY"], r"^[A-Za-z0-9]+$")
            # 助记词加密密钥随主应用 .env 加载
            self.assertEqual(len(env["WALLET_MNEMONIC_ENCRYPTION_KEY"]), 64)

    def test_does_not_overwrite_existing_env(self):
        # 已有 .env 视为密钥复用源：再次运行不得覆盖（避免改掉助记词加密密钥）。
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            original = (
                "DJANGO_SECRET_KEY=existing-secret\n"
                "WALLET_MNEMONIC_ENCRYPTION_KEY=do-not-touch\n"
            )
            env_path.write_text(original, encoding="utf-8")

            result = self.run_init_env(tmp_path)

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(env_path.read_text(encoding="utf-8"), original)


class LocalChainIntegrationMixin:
    EVM_RPC = "http://127.0.0.1:8545"

    def _require_anvil(self) -> Web3:
        w3 = Web3(Web3.HTTPProvider(self.EVM_RPC, request_kwargs={"timeout": 5}))
        if not w3.is_connected():
            self.skipTest("本地 anvil 未启动，跳过真实 EVM 联调测试")
        return w3

    def _deploy_test_erc20(self, w3: Web3, *, supply_raw: int):
        token_factory = w3.eth.contract(
            abi=LOCAL_EVM_ERC20_ABI,
            bytecode=LOCAL_EVM_ERC20_BYTECODE,
        )
        deployer = w3.eth.accounts[0]
        tx_hash = token_factory.constructor().transact({"from": deployer})
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
        token = w3.eth.contract(
            address=receipt.contractAddress,
            abi=LOCAL_EVM_ERC20_ABI,
        )
        if supply_raw > 0:
            mint_hash = token.functions.mint(deployer, supply_raw).transact(
                {"from": deployer}
            )
            w3.eth.wait_for_transaction_receipt(mint_hash)
        return token







class LocalEvmContractCompatibilityTests(LocalChainIntegrationMixin, TestCase):
    def test_deploy_test_erc20_emits_standard_transfer_event(self):
        # 联调 helper 部署出的测试 ERC20 必须兼容标准 Transfer 事件，否则扫描器无法观测到日志。
        w3 = self._require_anvil()
        token_contract = self._deploy_test_erc20(w3, supply_raw=1_000_000)

        receipt = w3.eth.wait_for_transaction_receipt(
            token_contract.functions.transfer(w3.eth.accounts[1], 250_000).transact(
                {"from": w3.eth.accounts[0]}
            )
        )

        self.assertGreaterEqual(len(receipt["logs"]), 1)
        self.assertEqual(
            Web3.to_hex(receipt["logs"][0]["topics"][0]),
            ERC20_TRANSFER_TOPIC0,
        )
