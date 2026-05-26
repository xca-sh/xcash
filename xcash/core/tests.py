from __future__ import annotations

import shutil
import subprocess
from datetime import timedelta
from decimal import Decimal
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.cache import cache
from django.core.cache import cache as _cache
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3

from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTaskType
from chains.models import TransferType
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import Wallet
from chains.tasks import confirm_transfer
from chains.test_signer import build_test_remote_signer_backend
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
from currencies.models import Crypto
from evm.local_erc20 import LOCAL_EVM_ERC20_ABI
from evm.local_erc20 import LOCAL_EVM_ERC20_BYTECODE
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from projects.models import Project
from withdrawals.models import Withdrawal
from withdrawals.models import WithdrawalStatus

_CORE_TEST_PATCHERS = []


def setUpModule():
    # core 真实链路会用到账户锁；每轮开始前清掉测试 Redis，避免前序 run 遗留锁串扰。
    _cache.clear()
    backend = build_test_remote_signer_backend()
    # core 联调测试需要真实地址派生与签名，但不应额外依赖外部 signer 进程。
    for target in (
        "chains.signer.get_signer_backend",
        "evm.models.get_signer_backend",
    ):
        patcher = patch(target, return_value=backend)
        patcher.start()
        _CORE_TEST_PATCHERS.append(patcher)


def tearDownModule():
    while _CORE_TEST_PATCHERS:
        _CORE_TEST_PATCHERS.pop().stop()
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


class InitEnvScriptTests(TestCase):
    def test_init_env_creates_env_from_example_and_replaces_placeholders(self):
        repo_root = Path(__file__).resolve().parents[2]
        script_path = repo_root / "scripts" / "init_env.sh"

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            example_path = tmp_path / ".env.example"
            scripts_dir = tmp_path / "scripts"
            scripts_dir.mkdir()
            copied_script_path = scripts_dir / "init_env.sh"
            example_path.write_text(
                "DJANGO_SECRET_KEY=change-me-with-a-64-char-random-string\n"
                "POSTGRES_PASSWORD=change-me-main-db-password\n"
                "STATIC_VALUE=keep-me\n",
                encoding="utf-8",
            )
            shutil.copy2(script_path, copied_script_path)
            copied_script_path.chmod(0o755)

            result = subprocess.run(  # noqa: S603
                [str(copied_script_path)],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0)
            env_path = tmp_path / ".env"
            self.assertTrue(env_path.exists())
            env_content = env_path.read_text(encoding="utf-8")
            self.assertIn("STATIC_VALUE=keep-me", env_content)
            self.assertNotIn("change-me-with-a-64-char-random-string", env_content)
            self.assertNotIn("change-me-main-db-password", env_content)

            env_values = dict(
                line.split("=", maxsplit=1)
                for line in env_content.splitlines()
                if line and not line.startswith("#") and "=" in line
            )
            self.assertEqual(len(env_values["DJANGO_SECRET_KEY"]), 64)
            self.assertEqual(len(env_values["POSTGRES_PASSWORD"]), 32)
            self.assertRegex(env_values["DJANGO_SECRET_KEY"], r"^[A-Za-z0-9]+$")
            self.assertRegex(env_values["POSTGRES_PASSWORD"], r"^[A-Za-z0-9]+$")

    def test_init_env_does_not_overwrite_existing_env(self):
        repo_root = Path(__file__).resolve().parents[2]
        script_path = repo_root / "scripts" / "init_env.sh"

        with TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            (tmp_path / ".env.example").write_text(
                "DJANGO_SECRET_KEY=change-me-with-a-64-char-random-string\n",
                encoding="utf-8",
            )
            scripts_dir = tmp_path / "scripts"
            scripts_dir.mkdir()
            copied_script_path = scripts_dir / "init_env.sh"
            shutil.copy2(script_path, copied_script_path)
            copied_script_path.chmod(0o755)

            env_path = tmp_path / ".env"
            env_path.write_text("DJANGO_SECRET_KEY=existing-secret\n", encoding="utf-8")

            result = subprocess.run(  # noqa: S603
                [str(copied_script_path)],
                cwd=tmp_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 0)
            self.assertEqual(
                env_path.read_text(encoding="utf-8"),
                "DJANGO_SECRET_KEY=existing-secret\n",
            )


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


class LocalEvmScannerIntegrationTests(LocalChainIntegrationMixin, TestCase):










    def test_local_evm_missing_tx_is_dropped_and_reverts_withdrawal(self):
        # 节点查不到 hash 时，Transfer 被 drop，提币回退到 PENDING 等待重新匹配。
        self._require_anvil()
        crypto = Crypto.objects.create(
            name="Ethereum Missing Tx Local",
            symbol="ETHMX",
            coingecko_id="ethereum-missing-tx-local",
            decimals=18,
        )
        chain = Chain.objects.create(
            name="Anvil Missing Tx",
            code="anvil-missing-tx",
            type=ChainType.EVM,
            native_coin=crypto,
            chain_id=31337,
            rpc=self.EVM_RPC,
            active=True,
            confirm_block_count=1,
        )
        wallet = Wallet.generate()
        project = Project.objects.create(
            name="Local EVM Missing Tx Project",
            wallet=wallet,
        )
        addr = wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )
        recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000009"
        )
        tx_task = TxTask.objects.create(
            chain=chain,
            address=addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "9" * 64,
            stage=TxTaskStage.PENDING_CONFIRM,
            success=None,
        )
        transfer = Transfer.objects.create(
            chain=chain,
            block=1,
            hash=tx_task.tx_hash,
            event_id="native:tx",
            crypto=crypto,
            from_address=addr.address,
            to_address=recipient,
            value=Decimal("1"),
            amount=Decimal("0.01"),
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
            type=TransferType.Withdrawal,
            processed_at=timezone.now(),
        )
        withdrawal = Withdrawal.objects.create(
            project=project,
            out_no="local-missing-tx-order",
            chain=chain,
            crypto=crypto,
            amount=Decimal("0.01"),
            to=recipient,
            hash=tx_task.tx_hash,
            tx_task=tx_task,
            transfer=transfer,
            status=WithdrawalStatus.CONFIRMING,
        )

        old_retries = confirm_transfer.request.retries
        confirm_transfer.request.retries = confirm_transfer.max_retries
        try:
            with self.captureOnCommitCallbacks(execute=True):
                confirm_transfer.run(transfer.pk)
        finally:
            confirm_transfer.request.retries = old_retries

        # Transfer 被 drop 后直接删除，释放唯一约束以允许 reorg 后重建
        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())
        withdrawal.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.PENDING)
        self.assertIsNone(withdrawal.transfer_id)
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(tx_task.success)
