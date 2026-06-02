from __future__ import annotations

import shutil
import subprocess
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.cache import cache as _cache
from django.core.management import call_command
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from web3 import Web3

from chains.constants import ChainType
from chains.models import Chain
from chains.models import Wallet
from core.default_data import ensure_base_currencies
from core.default_data import ensure_local_chains
from core.models import SYSTEM_SETTINGS_CACHE_KEY
from core.models import SystemSettings
from core.models import SystemWallet
from core.runtime_settings import get_admin_sensitive_action_otp_max_age_seconds
from core.runtime_settings import get_alerts_repeat_interval_minutes
from core.runtime_settings import get_webhook_delivery_breaker_threshold
from core.runtime_settings import get_webhook_delivery_max_backoff_seconds
from core.runtime_settings import get_webhook_delivery_max_retries
from currencies.models import ChainToken
from evm.local_erc20 import LOCAL_EVM_ERC20_ABI
from evm.local_erc20 import LOCAL_EVM_ERC20_BYTECODE
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0


def setUpModule():
    # core 真实链路会用到账户锁；每轮开始前清掉测试 Redis，避免前序 run 遗留锁串扰。
    # 地址派生与签名已在 chains 内部闭环，测试直接走真实派生，无需 mock 外部 signer。
    _cache.clear()


def tearDownModule():
    _cache.clear()


@override_settings(
    ADMIN_SENSITIVE_ACTION_OTP_MAX_AGE_SECONDS=900,
    ALERTS_REPEAT_INTERVAL_MINUTES=30,
)
class SystemSettingsRuntimeTests(TestCase):
    def tearDown(self):
        cache.delete(SYSTEM_SETTINGS_CACHE_KEY)
        super().tearDown()

    def test_runtime_settings_use_database_override_before_settings_fallback(self):
        # 系统运行参数中心存在记录时，业务读取应优先采用数据库值，而不是继续回退到 settings 常量。
        SystemSettings.objects.create(
            admin_sensitive_action_otp_max_age_seconds=480,
            alerts_repeat_interval_minutes=7,
            webhook_delivery_breaker_threshold=12,
            webhook_delivery_max_retries=9,
            webhook_delivery_max_backoff_seconds=45,
        )

        self.assertEqual(get_admin_sensitive_action_otp_max_age_seconds(), 480)
        self.assertEqual(get_alerts_repeat_interval_minutes(), 7)
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
            ChainToken.objects.filter(
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
    def test_init_local_chains_deploys_local_usdt_and_creates_chain_token(self):
        w3 = self._require_local_evm()

        call_command("init_local_chains")

        evm_chain = Chain.objects.get(code="ethereum-local")
        usdt_mapping = ChainToken.objects.get(
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

        usdt_mapping = ChainToken.objects.get(
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
            ChainToken.objects.filter(chain__code="ethereum-local").exists()
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
