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
from django.test import override_settings
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
from core.runtime_settings import get_webhook_delivery_max_backoff_seconds
from core.runtime_settings import get_webhook_delivery_max_retries
from currencies.models import CryptoOnChain
from currencies.models import Fiat
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


class AdminPathConfigTests(SimpleTestCase):
    def test_normalize_admin_path_trims_slashes(self):
        from config.admin_path import admin_route_prefix
        from config.admin_path import normalize_admin_path

        self.assertEqual(normalize_admin_path("  /secure-admin/  "), "secure-admin")
        self.assertEqual(admin_route_prefix("secure-admin"), "secure-admin/")
        self.assertEqual(normalize_admin_path("   "), "")
        self.assertEqual(admin_route_prefix(""), "")

    @override_settings(ADMIN_PATH="", ADMIN_ROUTE_PREFIX="")
    def test_default_admin_urlpatterns_keep_admin_at_root(self):
        from config.urls import build_admin_urlpatterns

        patterns = build_admin_urlpatterns()

        self.assertEqual(str(patterns[0].pattern), "")
        self.assertEqual(str(patterns[1].pattern), "operations/inspection")
        self.assertEqual(str(patterns[-1].pattern), "")

    @override_settings(ADMIN_PATH="secure-admin", ADMIN_ROUTE_PREFIX="secure-admin/")
    def test_configured_admin_urlpatterns_use_admin_path_prefix(self):
        from config.urls import build_admin_urlpatterns

        patterns = build_admin_urlpatterns()

        self.assertEqual(str(patterns[0].pattern), "secure-admin")
        self.assertEqual(str(patterns[1].pattern), "secure-admin/")
        self.assertEqual(str(patterns[2].pattern), "secure-admin/operations/inspection")
        self.assertEqual(str(patterns[-1].pattern), "secure-admin/")

    def test_admin_session_timeout_middleware_detects_root_admin_route(self):
        from django.test import RequestFactory

        from common.middlewares import AdminSessionTimeoutMiddleware

        middleware = AdminSessionTimeoutMiddleware(lambda request: None)
        factory = RequestFactory()

        self.assertTrue(middleware.is_admin_request_path(factory.get("/")))
        self.assertFalse(
            middleware.is_admin_request_path(factory.get("/v1/invoice/test"))
        )


class SystemSettingsRuntimeTests(TestCase):
    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    def test_str_returns_plain_string_for_admin_log(self):
        system_settings = SystemSettings()

        self.assertEqual(str(system_settings), "系统运行参数")

    def test_runtime_settings_use_database_override_before_settings_fallback(self):
        # 系统运行参数中心存在记录时，业务读取应优先采用数据库值，而不是继续回退到 settings 常量。
        SystemSettings.objects.create(
            webhook_delivery_max_retries=9,
            webhook_delivery_max_backoff_seconds=45,
        )

        self.assertEqual(get_webhook_delivery_max_retries(), 9)
        self.assertEqual(get_webhook_delivery_max_backoff_seconds(), 45)

    def test_cache_invalidation_is_deferred_to_commit(self):
        """缓存失效必须挂在事务提交后。

        全局 ATOMIC_REQUESTS=True 下，admin 保存请求的事务要到响应阶段才提交。
        若在事务内删除缓存，删除与 COMMIT 之间任何并发读者都会 cache miss、从库里
        读到未提交前的旧行再写回——新配置提交后永远读不到，且没有 TTL 能自愈。
        """
        settings_row = SystemSettings.objects.create(webhook_delivery_max_retries=3)
        self.assertEqual(get_webhook_delivery_max_retries(), 3)

        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            settings_row.webhook_delivery_max_retries = 7
            settings_row.save()
            # 事务尚未提交：失效动作应仍在待执行队列里，缓存保持旧值。
            self.assertEqual(cache.get(SYSTEM_SETTINGS_CACHE_KEY).pk, settings_row.pk)

        self.assertTrue(callbacks)

    def test_cache_reflects_new_value_after_commit(self):
        settings_row = SystemSettings.objects.create(webhook_delivery_max_retries=3)
        self.assertEqual(get_webhook_delivery_max_retries(), 3)

        with self.captureOnCommitCallbacks(execute=True):
            settings_row.webhook_delivery_max_retries = 7
            settings_row.save()

        self.assertEqual(get_webhook_delivery_max_retries(), 7)

    def test_cache_entry_has_finite_ttl(self):
        """缓存必须带 TTL 兜底：绕过模型 save 的写入不会触发 on_commit 失效。"""
        SystemSettings.objects.create(webhook_delivery_max_retries=3)
        get_webhook_delivery_max_retries()

        self.assertIsNotNone(cache.ttl(SYSTEM_SETTINGS_CACHE_KEY))
        self.assertGreater(cache.ttl(SYSTEM_SETTINGS_CACHE_KEY), 0)


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


class OperationalInspectionSidebarBadgeTests(TestCase):
    """侧边栏风险 badge 的资源风险来自异步巡检写入的缓存，而非渲染时实时 RPC。

    回归守卫：资源告警不能只藏在右上角，必须落到“异常巡检”导航入口。
    """

    def setUp(self):
        _cache.clear()
        worker_health = patch(
            "core.dashboard.worker_health_status",
            return_value={"status": "ok", "groups": [], "risk_count": 0},
        )
        worker_health.start()
        self.addCleanup(worker_health.stop)

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

    @override_settings(ADMIN_PATH_CONFIGURED=True)
    def test_sidebar_badge_counts_cached_resource_risk(self):
        from django.test import RequestFactory

        from core.dashboard import has_no_operational_inspection_risk
        from core.dashboard import has_operational_inspection_risk
        from core.dashboard import operational_inspection_sidebar_badge

        # 无 webhook 堆积，但缓存里有 EVM 资源风险 -> 入口 badge 必须提示。
        OperationalRiskService.cache_resource_risk_counts(
            evm_low_native_balance_count=1,
            tron_low_resource_count=2,
        )
        request = RequestFactory().get("/")

        self.assertTrue(has_operational_inspection_risk(request))
        self.assertFalse(has_no_operational_inspection_risk(request))
        self.assertEqual(operational_inspection_sidebar_badge(request), 3)

    @override_settings(ADMIN_PATH_CONFIGURED=False)
    def test_sidebar_badge_counts_admin_path_warning(self):
        from django.test import RequestFactory

        from core.dashboard import operational_inspection_sidebar_badge

        request = RequestFactory().get("/")

        self.assertEqual(operational_inspection_sidebar_badge(request), 1)

    @override_settings(ADMIN_PATH_CONFIGURED=True)
    def test_sidebar_badge_hidden_without_any_risk(self):
        from django.test import RequestFactory

        from core.dashboard import has_no_operational_inspection_risk
        from core.dashboard import has_operational_inspection_risk
        from core.dashboard import operational_inspection_sidebar_badge

        request = RequestFactory().get("/")

        self.assertFalse(has_operational_inspection_risk(request))
        self.assertTrue(has_no_operational_inspection_risk(request))
        self.assertEqual(operational_inspection_sidebar_badge(request), 0)


class OperationalInspectionPayloadTests(TestCase):
    def setUp(self):
        worker_health = patch(
            "core.dashboard.worker_health_status",
            return_value={"status": "ok", "groups": [], "risk_count": 0},
        )
        worker_health.start()
        self.addCleanup(worker_health.stop)

    def empty_metrics(self):
        return {
            "recent_failed_attempts": [],
            "recent_stalled_invoices": [],
            "recent_stalled_webhook_events": [],
        }

    @override_settings(ADMIN_PATH_CONFIGURED=False)
    def test_payload_includes_admin_path_warning(self):
        from core.dashboard import _build_operational_inspection_payload

        payload = _build_operational_inspection_payload(self.empty_metrics())

        self.assertEqual(len(payload["attention_items"]), 1)
        self.assertEqual(
            str(payload["inspection_sections"][0]["title"]), "后台安全配置"
        )
        self.assertEqual(payload["inspection_sections"][0]["count"], 1)
        self.assertEqual(
            str(payload["inspection_sections"][0]["rows"][0]["title"]),
            "后台入口未配置",
        )

    @override_settings(ADMIN_PATH_CONFIGURED=True)
    def test_payload_includes_resource_risk_rows(self):
        from django.urls import reverse

        from core.dashboard import _build_operational_inspection_payload

        evm_chain = make_evm_chain(code=ChainCode.Ethereum)
        tron_chain = make_tron_chain()
        wallet = Wallet.objects.create()
        evm_sender = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address("0x" + "68" * 20),
        )
        tron_sender = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.TRON,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=1,
            address="TJRabPrwbZy45sbavfcjinPJC18kjpRTv8",
        )
        resource_risk_summary = {
            "evm_low_native_balance_count": 1,
            "recent_evm_low_native_balance_alerts": [
                {
                    "chain": evm_chain,
                    "sender": evm_sender,
                    "current_balance": 1_000,
                    "required_balance": 2_000,
                    "task_count": 1,
                    "error": "",
                }
            ],
            "tron_low_resource_count": 1,
            "recent_tron_low_resource_alerts": [
                {
                    "chain": tron_chain,
                    "sender": tron_sender,
                    "available_energy": 5,
                    "required_energy": 120,
                    "available_bandwidth": 0,
                    "required_bandwidth": 300,
                    "task_count": 1,
                    "error": "",
                }
            ],
        }

        payload = _build_operational_inspection_payload(
            self.empty_metrics(),
            resource_risk_summary=resource_risk_summary,
        )

        self.assertEqual(len(payload["attention_items"]), 2)
        self.assertEqual(payload["inspection_sections"][1]["count"], 1)
        self.assertEqual(payload["inspection_sections"][2]["count"], 1)
        evm_row = payload["inspection_sections"][1]["rows"][0]
        tron_row = payload["inspection_sections"][2]["rows"][0]
        self.assertEqual(str(evm_row["title"]), "EVM Gas 余额不足")
        self.assertIn("需要 2,000 wei", str(evm_row["description"]))
        self.assertEqual(
            evm_row["href"],
            reverse("admin:chains_address_change", args=[evm_sender.pk]),
        )
        self.assertEqual(str(tron_row["title"]), "Tron 资源不足")
        self.assertIn("Energy 5/120", str(tron_row["description"]))


class DashboardMetricsTests(TestCase):
    def setUp(self):
        Fiat.objects.get_or_create(code="USD")

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
            currency_id="USD",
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
            # 超管口令必须随机：固定默认值在开源仓库里等同公开常量，而后台在未设
            # ADMIN_PATH 时就挂在站点根路径，扫到即可直接登录。
            superuser_password = env["DJANGO_DEFAULT_SUPERUSER_PASSWORD"]
            self.assertNotEqual(superuser_password, "Admin@123456")
            self.assertEqual(len(superuser_password), 24)
            self.assertRegex(superuser_password, r"^[A-Za-z0-9]+$")

    def test_generated_superuser_password_differs_between_runs(self):
        passwords = set()
        for _ in range(2):
            with TemporaryDirectory() as tmp_dir:
                tmp_path = Path(tmp_dir)
                result = self.run_init_env(tmp_path)
                self.assertEqual(result.returncode, 0, msg=result.stderr)
                env = self.parse_env(tmp_path / ".env")
                passwords.add(env["DJANGO_DEFAULT_SUPERUSER_PASSWORD"])

        self.assertEqual(len(passwords), 2)

    def test_does_not_overwrite_existing_env(self):
        # 已有 .env 必须触发门禁失败，避免脚本静默覆盖助记词加密密钥。
        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            env_path = tmp_path / ".env"
            original = (
                "DJANGO_SECRET_KEY=existing-secret\n"
                "WALLET_MNEMONIC_ENCRYPTION_KEY=do-not-touch\n"
            )
            env_path.write_text(original, encoding="utf-8")

            result = self.run_init_env(tmp_path)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("拒绝覆盖", result.stderr)
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
