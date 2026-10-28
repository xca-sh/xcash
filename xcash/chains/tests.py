from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.core import checks
from django.core.exceptions import ImproperlyConfigured
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db import connection
from django.db import transaction
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from chains.checks import chain_registry_check
from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ConfirmMode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import TxHash
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import VaultSlot
from chains.models import VaultSlotBalance
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.models import Wallet
from chains.registry import ChainFamily
from chains.registry import register_chain_family
from chains.tasks import process_transfer
from chains.tests_fixtures import make_evm_chain
from common.jobs import PRIORITY_DEFAULT
from common.jobs import PRIORITY_URGENT
from common.tests.job_helpers import queued_args
from common.tests.job_helpers import queued_jobs
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from deposits.models import Deposit
from projects.models import Customer
from projects.models import Project


class SharedWeb3Tests(SimpleTestCase):
    """每个任务都会重新查出 Chain 实例，Web3（及其 HTTP 会话）必须按 RPC 在进程内复用。"""

    def setUp(self):
        from chains import models as chain_models

        self.chain_models = chain_models
        chain_models._shared_web3_instances.clear()

    def test_separately_loaded_chains_share_web3_for_same_rpc(self):
        first = Chain(code=ChainCode.Ethereum, rpc="http://shared-w3.local")
        second = Chain(code=ChainCode.Ethereum, rpc="http://shared-w3.local")

        self.assertIs(first.w3, second.w3)

    def test_rpc_change_and_poa_flag_get_distinct_web3(self):
        ethereum = Chain(code=ChainCode.Ethereum, rpc="http://shared-w3.local")
        moved = Chain(code=ChainCode.Ethereum, rpc="http://shared-w3-new.local")
        bsc = Chain(code=ChainCode.BSC, rpc="http://shared-w3.local")

        self.assertIsNot(ethereum.w3, moved.w3)
        # BSC 是 PoA 链，必须拿到注入了 extraData 兼容中间件的实例，不能复用非 PoA 那份。
        self.assertIsNot(ethereum.w3, bsc.w3)
        self.assertIn(ExtraDataToPOAMiddleware, list(bsc.w3.middleware_onion))
        self.assertNotIn(ExtraDataToPOAMiddleware, list(ethereum.w3.middleware_onion))

    def test_web3_is_rebuilt_after_fork(self):
        parent_w3 = Chain(code=ChainCode.Ethereum, rpc="http://shared-w3.local").w3

        with patch.object(
            self.chain_models.os,
            "getpid",
            return_value=self.chain_models.os.getpid() + 1,
        ):
            child_w3 = Chain(code=ChainCode.Ethereum, rpc="http://shared-w3.local").w3

        self.assertIsNot(child_w3, parent_w3)


class ChainDerivedFieldsSaveTests(TestCase):
    def test_partial_save_persists_derived_chain_fields(self):
        chain = Chain.objects.create(code=ChainCode.Anvil, active=False)
        Chain.objects.filter(pk=chain.pk).update(is_testnet=False, type="")
        chain.refresh_from_db()

        chain.rpc = "http://127.0.0.1:8545"
        with patch.object(Chain, "clean", return_value=None):
            chain.save(update_fields=["rpc"])

        chain.refresh_from_db()
        self.assertEqual(chain.type, ChainType.EVM)
        self.assertTrue(chain.is_testnet)


class ChainScanCursorTests(TestCase):
    def test_advance_scan_cursor_never_rewinds_database_value(self):
        # 内存里仍是 100，库里已被推进到 150：推进到 120 不得回退库值。
        chain = Chain.objects.create(code=ChainCode.Anvil, active=False)
        chain.reset_scan_cursor(100)
        Chain.objects.filter(pk=chain.pk).update(scan_cursor_block=150)

        chain.advance_scan_cursor(120)

        chain.refresh_from_db()
        self.assertEqual(chain.scan_cursor_block, 150)


class ChainAdminScannerTests(TestCase):
    def setUp(self):
        from chains.admin import ChainAdmin

        self.chain = Chain.objects.create(code=ChainCode.Anvil, active=False)
        Chain.objects.filter(pk=self.chain.pk).update(
            latest_block_number=500,
            scan_cursor_block=100,
            scan_last_error="rpc down",
            scan_last_error_at=timezone.now(),
        )
        self.admin = ChainAdmin(Chain, AdminSite())
        self.admin.message_user = Mock()

    def test_scanner_actions_are_superuser_only(self):
        # 启停/追平会改动资金入账的扫描位点，不能靠 Chain 的 change 权限放行给普通管理员。
        superuser = Mock(is_active=True, is_superuser=True)
        staff = Mock(is_active=True, is_superuser=False)
        self.assertTrue(self.admin.has_manage_scanner_permission(Mock(user=superuser)))
        self.assertFalse(self.admin.has_manage_scanner_permission(Mock(user=staff)))
        for action_name in (
            "enable_selected_scanners",
            "disable_selected_scanners",
            "sync_selected_to_latest",
        ):
            action = getattr(self.admin, action_name)
            self.assertEqual(list(action.allowed_permissions), ["manage_scanner"])

    def test_sync_selected_to_latest_moves_cursor_to_chain_head_and_clears_error(self):
        self.admin.sync_selected_to_latest(
            Mock(), Chain.objects.filter(pk=self.chain.pk)
        )

        self.chain.refresh_from_db()
        self.assertEqual(self.chain.scan_cursor_block, 500)
        self.assertEqual(self.chain.scan_last_error, "")
        self.assertIsNone(self.chain.scan_last_error_at)

    def test_editing_chain_does_not_overwrite_scanner_progress(self):
        # 后台读出链之后、保存之前，扫描器推进了链高与位点、超管暂停了扫描；
        # 保存链配置不得把这些字段写回请求开始时读到的旧值。
        obj = Chain.objects.get(pk=self.chain.pk)
        Chain.objects.filter(pk=self.chain.pk).update(
            latest_block_number=600,
            scan_cursor_block=200,
            scan_enabled=False,
        )

        obj.sort_order = 5
        self.admin.save_model(
            Mock(), obj, Mock(changed_data=["sort_order"]), change=True
        )

        self.chain.refresh_from_db()
        self.assertEqual(self.chain.sort_order, 5)
        self.assertEqual(self.chain.latest_block_number, 600)
        self.assertEqual(self.chain.scan_cursor_block, 200)
        self.assertFalse(self.chain.scan_enabled)


class TxTaskValidationTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address="0x0000000000000000000000000000000000000001",
        )


class WalletBip44AccountMapTests(TestCase):
    def test_vault_maps_to_bip44_account_0(self):
        self.assertEqual(Wallet.get_bip44_account(AddressUsage.HotWallet), 0)

    def test_unknown_usage_raises_value_error(self):
        with self.assertRaises(ValueError):
            Wallet.get_bip44_account("nonexistent")


class TxHashModelTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address="0x0000000000000000000000000000000000000011",
        )
        self.task = TxTask.objects.create(
            chain=self.chain,
            sender=self.addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "a1" * 32,
            status=TxTaskStatus.QUEUED,
        )

    def test_tx_hash_unique_per_chain_hash(self):
        TxHash.objects.create(
            tx_task=self.task,
            chain=self.chain,
            hash="0x" + "b1" * 32,
            version=1,
        )

        with self.assertRaises(IntegrityError):
            TxHash.objects.create(
                tx_task=self.task,
                chain=self.chain,
                hash="0x" + "b1" * 32,
                version=2,
            )

    def test_tx_hash_unique_version_per_tx_task(self):
        TxHash.objects.create(
            tx_task=self.task,
            chain=self.chain,
            hash="0x" + "c1" * 32,
            version=1,
        )

        with self.assertRaises(IntegrityError):
            TxHash.objects.create(
                tx_task=self.task,
                chain=self.chain,
                hash="0x" + "c2" * 32,
                version=1,
            )

    def test_tx_hash_chain_must_match_tx_task_chain(self):
        other_chain = make_evm_chain(code=ChainCode.BSC)

        tx_hash = TxHash(
            tx_task=self.task,
            chain=other_chain,
            hash="0x" + "d1" * 32,
            version=1,
        )

        with self.assertRaises(ValidationError):
            tx_hash.full_clean()


class TxTaskTxHashHistoryTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address="0x0000000000000000000000000000000000000021",
        )
        self.task = TxTask.objects.create(
            chain=self.chain,
            sender=self.addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "e1" * 32,
            status=TxTaskStatus.QUEUED,
        )

    def test_append_tx_hash_updates_current_tx_hash_and_keeps_history(self):
        self.task.append_tx_hash(self.task.tx_hash)

        appended = self.task.append_tx_hash("0x" + "e2" * 32)

        self.task.refresh_from_db()
        history = list(self.task.tx_hashes.order_by("version"))
        self.assertEqual(self.task.tx_hash, "0x" + "e2" * 32)
        self.assertEqual(appended.version, 2)
        self.assertEqual(
            [item.hash for item in history], ["0x" + "e1" * 32, "0x" + "e2" * 32]
        )

    def test_append_existing_hash_for_same_task_updates_current_tx_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "e2" * 32)

        appended = self.task.append_tx_hash(old_hash)

        self.task.refresh_from_db()
        self.assertEqual(appended.tx_task_id, self.task.pk)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_resolve_tx_task_by_old_hash(self):
        self.task.append_tx_hash(self.task.tx_hash)
        self.task.append_tx_hash("0x" + "e2" * 32)

        resolved = TxTask.resolve_by_hash(
            chain=self.chain,
            tx_hash="0x" + "e1" * 32,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, self.task.pk)

    def test_resolve_tx_task_falls_back_to_current_tx_hash(self):
        resolved = TxTask.resolve_by_hash(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.pk, self.task.pk)


class AddressIdentityTests(TestCase):
    def test_address_identity_tuple_must_be_unique(self):
        # 同一钱包在同链同 usage + address_index 上只能有一个 Address。
        wallet = Wallet.objects.create()
        Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address="0x0000000000000000000000000000000000000001",
        )

        with self.assertRaises(IntegrityError):
            Address.objects.create(
                wallet=wallet,
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                bip44_account=0,
                address_index=0,
                address="0x0000000000000000000000000000000000000002",
            )

    @patch("chains.keys.derive_evm_address")
    def test_get_address_rejects_corrupted_existing_identity(self, derive_mock):
        # 历史脏数据若把同一 HD 身份写成错误地址，运行时必须立即报错而不是继续使用。
        derive_mock.return_value = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000aa"
        )
        wallet = Wallet.generate()
        Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address="0x0000000000000000000000000000000000000001",
        )

        with self.assertRaises(RuntimeError):
            wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                address_index=0,
            )

    @patch("chains.keys.derive_evm_address")
    def test_get_address_preserves_non_identity_integrity_error(self, derive_mock):
        # 若冲突的是别的唯一约束（如地址被其他地址记录占用），不能误判成 tuple 并发创建成功。
        expected_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000ab"
        )
        derive_mock.return_value = expected_address
        wallet = Wallet.generate()
        occupied_wallet = Wallet.generate()
        Address.objects.create(
            wallet=occupied_wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=9_999,
            address=expected_address,
        )

        with self.assertRaises(IntegrityError):
            wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                address_index=0,
            )

    @patch("chains.keys.derive_evm_address")
    def test_get_address_falls_back_to_locked_read_after_integrity_error(
        self, derive_mock
    ):
        # 模拟并发事务已落库但 get_or_create 撞 unique 约束失败的场景：
        # IntegrityError 后必须用 select_for_update 加锁回查，等对方事务提交后命中记录。
        expected_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000cc"
        )
        derive_mock.return_value = expected_address
        wallet = Wallet.generate()
        # 预创建等价于"对方事务已提交"的身份记录；bip44_account 必须与  Vault
        # 的 BIP44 映射一致，否则会触发 get_address 的身份完整性检查。
        bip44_account = Wallet.get_bip44_account(AddressUsage.HotWallet)
        Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=bip44_account,
            address_index=0,
            address=expected_address,
        )

        def fake_get_or_create(**_kwargs):
            raise IntegrityError("duplicate key value violates unique constraint")

        with patch.object(
            Address.objects, "get_or_create", side_effect=fake_get_or_create
        ):
            addr = wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                address_index=0,
            )

        self.assertEqual(addr.address, expected_address)

    @patch("chains.keys.derive_evm_address")
    def test_get_address_reraises_integrity_error_when_fallback_misses(
        self, derive_mock
    ):
        # 回查 DoesNotExist 时必须把原 IntegrityError 抛出，避免吞掉真错误。
        derive_mock.return_value = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000dd"
        )
        wallet = Wallet.generate()

        def fake_get_or_create(**_kwargs):
            raise IntegrityError("unrelated unique constraint")

        with (
            patch.object(
                Address.objects, "get_or_create", side_effect=fake_get_or_create
            ),
            self.assertRaises(IntegrityError),
        ):
            wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                address_index=0,
            )


class WalletGenerationTests(TestCase):
    """钱包生成与地址派生已在主系统内部闭环（chains/keys.py）。"""

    def test_generate_creates_distinct_encrypted_mnemonics(self):
        # 每个钱包持有独立助记词；密文非空、互不相同，且能解密回 24 词助记词。
        wallet_a = Wallet.generate()
        wallet_b = Wallet.generate()

        self.assertTrue(wallet_a.encrypted_mnemonic)
        self.assertTrue(wallet_b.encrypted_mnemonic)
        self.assertNotEqual(wallet_a.encrypted_mnemonic, wallet_b.encrypted_mnemonic)
        mnemonic_a = wallet_a.decrypt_mnemonic()
        self.assertEqual(len(mnemonic_a.split()), 24)
        self.assertNotEqual(mnemonic_a, wallet_b.decrypt_mnemonic())

    def test_get_address_matches_internal_derivation_and_is_idempotent(self):
        # get_address 用本钱包助记词按 HD 路径在进程内派生，落库后再次调用应命中同一条记录。
        from chains.keys import derive_evm_address

        wallet = Wallet.generate()
        expected = derive_evm_address(
            mnemonic=wallet.decrypt_mnemonic(),
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
        )

        first = wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )
        second = wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )

        self.assertEqual(first.address, expected)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(
            Address.objects.filter(
                wallet=wallet,
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                address_index=0,
            ).count(),
            1,
        )

    def test_address_sign_evm_transaction_round_trips(self):
        # Address 能用本钱包私钥签出 legacy 交易：tx_hash / raw 均为小写 0x 十六进制。
        wallet = Wallet.generate()
        address = wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )
        tx_dict = {
            "chainId": 1,
            "nonce": 0,
            "from": address.address,
            "to": "0x000000000000000000000000000000000000dEaD",
            "value": 1000,
            "data": "0x",
            "gas": 21000,
            "gasPrice": 20000000000,
        }

        signed = address.sign_evm_transaction(tx_dict=tx_dict)

        self.assertTrue(signed.tx_hash.startswith("0x"))
        self.assertEqual(signed.tx_hash, signed.tx_hash.lower())
        self.assertTrue(signed.raw_transaction.startswith("0x"))


class WalletMnemonicKeyCheckTests(TestCase):
    @override_settings(DEBUG=False, WALLET_MNEMONIC_ENCRYPTION_KEY="")
    def test_missing_key_in_production_raises_error(self):
        error_ids = {error.id for error in checks.run_checks()}
        self.assertIn("chains.E001", error_ids)

    @override_settings(DEBUG=False, WALLET_MNEMONIC_ENCRYPTION_KEY="configured-key")
    def test_configured_key_passes(self):
        error_ids = {error.id for error in checks.run_checks()}
        self.assertNotIn("chains.E001", error_ids)


class TransferServiceCreateObservedTests(TestCase):
    """覆盖 TransferService.create_observed_transfer 的幂等与冲突场景。"""

    def setUp(self):
        from chains.service import ObservedTransferPayload

        self.crypto = Crypto.objects.create(
            name="Ether OT",
            symbol="ETH-OT",
            coingecko_id="ether-ot",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.payload = ObservedTransferPayload(
            chain=self.chain,
            block=100,
            block_hash="0x" + "aa" * 32,
            tx_hash="0x" + "ab" * 32,
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000a1"
            ),
            to_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000a2"
            ),
            crypto=self.crypto,
            value=1000,
            amount=1,
            timestamp=1700000000,
            datetime=timezone.now(),
            source="test",
        )

    @patch("chains.service.TransferService.enqueue_processing")
    def test_first_create_returns_created_true(self, enqueue_mock):
        from chains.service import TransferService

        result = TransferService.create_observed_transfer(observed=self.payload)

        self.assertTrue(result.created)
        self.assertFalse(result.conflict)
        self.assertIsNotNone(result.transfer)
        enqueue_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_create_does_not_lock_chain_row(self, enqueue_mock):
        """落库不得对 Chain 行加锁。

        这是所有链、所有扫描器落库的唯一入口，链级排他锁会让本链全部外键插入
        （TxTask / TxHash / 余额快照）卡在 FK 的 FOR KEY SHARE 上，并与余额刷新
        路径构成反向加锁顺序。并发安全由 (chain, hash, event_index) 唯一约束加
        IntegrityError 回查保证，见 test_idempotent_replay_returns_created_false_no_conflict。
        """
        from chains.service import TransferService

        with patch(
            "chains.service.Chain.objects.select_for_update",
            wraps=Chain.objects.select_for_update,
        ) as lock_mock:
            TransferService.create_observed_transfer(observed=self.payload)

        lock_mock.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_idempotent_replay_returns_created_false_no_conflict(self, enqueue_mock):
        from chains.service import TransferService

        first = TransferService.create_observed_transfer(observed=self.payload)
        second = TransferService.create_observed_transfer(observed=self.payload)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertFalse(second.conflict)
        self.assertEqual(first.transfer.pk, second.transfer.pk)
        # 只有首次创建才触发 enqueue
        enqueue_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_oversized_observed_value_returns_conflict_without_breaking_transaction(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        oversized = ObservedTransferPayload(
            **{
                **self.payload.__dict__,
                "value": Decimal("1e32"),
                "amount": Decimal("1"),
            }
        )

        with transaction.atomic():
            result = TransferService.create_observed_transfer(observed=oversized)
            transfer_count = Transfer.objects.count()

        self.assertFalse(result.created)
        self.assertTrue(result.conflict)
        self.assertIsNone(result.transfer)
        self.assertEqual(transfer_count, 0)
        enqueue_mock.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_same_tx_different_event_index_creates_independent_transfers(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        first = ObservedTransferPayload(**{**self.payload.__dict__, "event_index": 0})
        second = ObservedTransferPayload(
            **{
                **self.payload.__dict__,
                "event_index": 1,
                "to_address": Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000a3"
                ),
            }
        )

        first_result = TransferService.create_observed_transfer(observed=first)
        second_result = TransferService.create_observed_transfer(observed=second)

        self.assertTrue(first_result.created)
        self.assertTrue(second_result.created)
        self.assertNotEqual(first_result.transfer.pk, second_result.transfer.pk)
        self.assertEqual(
            Transfer.objects.filter(
                chain=self.chain, hash=self.payload.tx_hash
            ).count(),
            2,
        )
        self.assertEqual(enqueue_mock.call_count, 2)

    @patch("chains.service.TransferService.enqueue_processing")
    def test_same_tx_same_event_index_is_idempotent(self, enqueue_mock):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        observed = ObservedTransferPayload(
            **{**self.payload.__dict__, "event_index": 0}
        )

        first = TransferService.create_observed_transfer(observed=observed)
        second = TransferService.create_observed_transfer(observed=observed)

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.transfer.pk, second.transfer.pk)
        enqueue_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_full_confirming_reorg_drops_existing_transfer_before_recreate(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        old_hash = self.payload.tx_hash
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=90,
            block_hash="0x" + "11" * 32,
            hash=old_hash,
            crypto=self.crypto,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
        )
        current = ObservedTransferPayload(
            chain=self.chain,
            block=100,
            block_hash="0x" + "22" * 32,
            tx_hash=old_hash,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            crypto=self.payload.crypto,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp + 1,
            datetime=self.payload.datetime + timedelta(seconds=1),
            source="test-reorg",
        )

        result = TransferService.create_observed_transfer(observed=current)

        self.assertTrue(result.created)
        self.assertFalse(result.conflict)
        self.assertFalse(Transfer.objects.filter(pk=old_transfer.pk).exists())
        replacement = Transfer.objects.get(chain=self.chain, hash=old_hash)
        self.assertEqual(replacement.pk, result.transfer.pk)
        self.assertEqual(replacement.block, 100)
        self.assertEqual(replacement.block_hash, "0x" + "22" * 32)
        enqueue_mock.assert_called_once()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_reorg_refreshes_full_confirmed_transfer_without_dropping_deposit(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        old_hash = self.payload.tx_hash
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=90,
            block_hash="0x" + "11" * 32,
            hash=old_hash,
            crypto=self.crypto,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
            confirm_mode=ConfirmMode.FULL,
            status=TransferStatus.CONFIRMED,
            processed_at=timezone.now(),
        )
        project = Project.objects.create(name="Confirmed Deposit Reorg")
        customer = Customer.objects.create(project=project, uid="confirmed-reorg")
        deposit = Deposit.objects.create(customer=customer, transfer=old_transfer)
        current = ObservedTransferPayload(
            chain=self.chain,
            block=100,
            block_hash="0x" + "22" * 32,
            tx_hash=old_hash,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            crypto=self.payload.crypto,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp + 1,
            datetime=self.payload.datetime + timedelta(seconds=1),
            source="test-full-confirmed-reorg",
        )

        result = TransferService.create_observed_transfer(observed=current)

        self.assertFalse(result.created)
        self.assertFalse(result.conflict)
        self.assertEqual(result.transfer.pk, old_transfer.pk)
        self.assertTrue(Transfer.objects.filter(pk=old_transfer.pk).exists())
        deposit.refresh_from_db()
        self.assertEqual(deposit.transfer_id, old_transfer.pk)
        old_transfer.refresh_from_db()
        self.assertEqual(old_transfer.block, 100)
        self.assertEqual(old_transfer.block_hash, "0x" + "22" * 32)
        self.assertEqual(old_transfer.timestamp, self.payload.timestamp + 1)
        self.assertEqual(
            old_transfer.datetime,
            self.payload.datetime + timedelta(seconds=1),
        )
        enqueue_mock.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_reorg_refreshes_quick_confirmed_transfer_metadata(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        old_hash = self.payload.tx_hash
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=90,
            block_hash="0x" + "11" * 32,
            hash=old_hash,
            crypto=self.crypto,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
            confirm_mode=ConfirmMode.QUICK,
            status=TransferStatus.CONFIRMED,
            processed_at=timezone.now(),
        )
        current = ObservedTransferPayload(
            chain=self.chain,
            block=100,
            block_hash="0x" + "22" * 32,
            tx_hash=old_hash,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            crypto=self.payload.crypto,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp + 1,
            datetime=self.payload.datetime + timedelta(seconds=1),
            source="test-reorg",
        )

        result = TransferService.create_observed_transfer(observed=current)

        self.assertFalse(result.created)
        self.assertFalse(result.conflict)
        self.assertEqual(result.transfer.pk, old_transfer.pk)
        self.assertEqual(
            Transfer.objects.filter(chain=self.chain, hash=old_hash).count(),
            1,
        )
        old_transfer.refresh_from_db()
        self.assertEqual(old_transfer.block, 100)
        self.assertEqual(old_transfer.block_hash, "0x" + "22" * 32)
        self.assertEqual(old_transfer.timestamp, self.payload.timestamp + 1)
        self.assertEqual(
            old_transfer.datetime,
            self.payload.datetime + timedelta(seconds=1),
        )
        enqueue_mock.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_quick_confirmed_same_tx_reobserve_is_idempotent_by_tx_hash(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        old_hash = self.payload.tx_hash
        old_transfer = Transfer.objects.create(
            chain=self.chain,
            block=self.payload.block,
            block_hash=self.payload.block_hash,
            hash=old_hash,
            crypto=self.crypto,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
            confirm_mode=ConfirmMode.QUICK,
            status=TransferStatus.CONFIRMED,
            processed_at=timezone.now(),
        )
        current = ObservedTransferPayload(
            chain=self.chain,
            block=self.payload.block,
            block_hash=self.payload.block_hash,
            tx_hash=old_hash,
            from_address=self.payload.from_address,
            to_address=self.payload.to_address,
            crypto=self.payload.crypto,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
            source="test-reobserve",
        )

        result = TransferService.create_observed_transfer(observed=current)

        self.assertFalse(result.created)
        self.assertFalse(result.conflict)
        self.assertEqual(result.transfer.pk, old_transfer.pk)
        self.assertEqual(
            Transfer.objects.filter(chain=self.chain, hash=old_hash).count(),
            1,
        )
        enqueue_mock.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_token_observed_inbound_does_not_schedule_vault_slot_deploy(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        project = Project.objects.create(name="Observed Token Deploy")
        slot = VaultSlot.objects.create(
            chain=self.chain,
            project=project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=1,
            address=self.payload.to_address,
            salt=b"\x01" * 32,
        )
        current = ObservedTransferPayload(
            chain=self.chain,
            block=101,
            block_hash="0x" + "bb" * 32,
            tx_hash="0x" + "bc" * 32,
            from_address=self.payload.from_address,
            to_address=slot.address,
            crypto=self.crypto,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
            source="test-token",
        )

        with (
            patch("chains.vault_slots.schedule_deploy") as schedule_deploy,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = TransferService.create_observed_transfer(observed=current)

        self.assertTrue(result.created)
        enqueue_mock.assert_called_once()
        schedule_deploy.assert_not_called()

    @patch("chains.service.TransferService.enqueue_processing")
    def test_native_observed_inbound_does_not_schedule_vault_slot_deploy(
        self,
        enqueue_mock,
    ):
        from chains.service import ObservedTransferPayload
        from chains.service import TransferService

        project = Project.objects.create(name="Observed Native Deploy")
        native_crypto = self.chain.native_coin
        slot = VaultSlot.objects.create(
            chain=self.chain,
            project=project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=2,
            address=self.payload.to_address,
            salt=b"\x02" * 32,
        )
        current = ObservedTransferPayload(
            chain=self.chain,
            block=102,
            block_hash="0x" + "cc" * 32,
            tx_hash="0x" + "cd" * 32,
            from_address=self.payload.from_address,
            to_address=slot.address,
            crypto=native_crypto,
            value=self.payload.value,
            amount=self.payload.amount,
            timestamp=self.payload.timestamp,
            datetime=self.payload.datetime,
            source="test-native",
        )

        with (
            patch("chains.vault_slots.schedule_deploy") as schedule_deploy,
            self.captureOnCommitCallbacks(execute=True),
        ):
            result = TransferService.create_observed_transfer(observed=current)

        self.assertTrue(result.created)
        enqueue_mock.assert_called_once()
        schedule_deploy.assert_not_called()


class TxTaskTransitionTests(TestCase):
    """验证 TxTask 封装的状态转换方法。"""

    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000d1"
            ),
        )
        self.task = TxTask.objects.create(
            chain=self.chain,
            sender=self.addr,
            tx_type=TxTaskType.VaultSlotCollect,
            tx_hash="0x" + "dd" * 32,
            status=TxTaskStatus.SUBMITTED,
        )

    def test_mark_submitted_transitions_from_queued(self):
        self.task.status = TxTaskStatus.QUEUED
        self.task.save(update_fields=["status"])

        updated = TxTask.mark_submitted(task_id=self.task.pk)

        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUBMITTED)

    def test_mark_submitted_does_not_resubmit_by_default(self):
        updated = TxTask.mark_submitted(task_id=self.task.pk)

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUBMITTED)

    def test_mark_submitted_allows_explicit_resubmission(self):
        updated = TxTask.mark_submitted(
            task_id=self.task.pk,
            allow_resubmitted=True,
        )

        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUBMITTED)

    def test_mark_submitted_does_not_override_terminal_state(self):
        self.task.status = TxTaskStatus.SUCCEEDED
        self.task.save(update_fields=["status"])

        updated = TxTask.mark_submitted(
            task_id=self.task.pk,
            allow_resubmitted=True,
        )

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUCCEEDED)

    def test_mark_finalized_success_transitions_correctly(self):
        updated = TxTask.mark_finalized_success(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUCCEEDED)

    def test_mark_finalized_success_can_resolve_old_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "ee" * 32)

        updated = TxTask.mark_finalized_success(
            chain=self.chain,
            tx_hash=old_hash,
        )

        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUCCEEDED)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_mark_finalized_failed_transitions_correctly(self):
        updated = TxTask.mark_finalized_failed(
            task_id=self.task.pk,
        )
        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.FAILED)

    def test_mark_finalized_failed_honors_expected_status(self):
        updated = TxTask.mark_finalized_failed(
            task_id=self.task.pk,
            expected_status=TxTaskStatus.QUEUED,
        )

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUBMITTED)

    def test_mark_finalized_success_does_not_override_failed_final_state(self):
        TxTask.mark_finalized_failed(
            task_id=self.task.pk,
        )

        updated = TxTask.mark_finalized_success(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.FAILED)

    def test_mark_finalized_failed_does_not_override_success_final_state(self):
        TxTask.mark_finalized_success(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        updated = TxTask.mark_finalized_failed(
            task_id=self.task.pk,
        )

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.SUCCEEDED)


class VaultSlotReceivedFlagTests(TestCase):
    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="VaultSlot Received Flag Coin",
            symbol="VRF",
            coingecko_id="vaultslot-received-flag",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        CryptoOnChain.objects.create(
            chain=self.chain,
            crypto=self.crypto,
            address=Web3.to_checksum_address("0x" + "34" * 20),
            decimals=6,
        )
        self.project = Project.objects.create(name="VaultSlot Received Flag")
        self.slot_address = Web3.to_checksum_address("0x" + "ab" * 20)
        self.slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            project=self.project,
            invoice_index=1,
            address=self.slot_address,
            salt=b"r" * 32,
        )

    def test_transfer_confirm_marks_vault_slot_has_received(self):
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "11" * 32,
            hash="0x" + "22" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "cd" * 20),
            to_address=self.slot_address,
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_000,
            datetime=timezone.now(),
        )

        self.slot.refresh_from_db()
        self.assertFalse(self.slot.has_received)

        transfer.confirm()

        self.slot.refresh_from_db()
        self.assertTrue(self.slot.has_received)

    def test_transfer_confirm_ignores_non_vault_slot_receiver(self):
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "33" * 32,
            hash="0x" + "44" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "ef" * 20),
            to_address=Web3.to_checksum_address("0x" + "12" * 20),
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_001,
            datetime=timezone.now(),
        )

        transfer.confirm()

        self.slot.refresh_from_db()
        self.assertFalse(self.slot.has_received)

    def test_transfer_confirm_refreshes_vault_slot_balance_from_chain(self):
        self.crypto.prices = {"USD": "2"}
        self.crypto.save(update_fields=["prices"])
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "55" * 32,
            hash="0x" + "66" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "cd" * 20),
            to_address=self.slot_address,
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_002,
            datetime=timezone.now(),
        )
        adapter = type("Adapter", (), {"get_balance": lambda *_args: 1_234_567})()

        # 余额刷新已挪到确认事务提交后（on_commit）执行，测试内需显式触发回调。
        with (
            patch(
                "chains.vault_slot_balances.AdapterFactory.get_adapter",
                return_value=adapter,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            transfer.confirm()

        balance = VaultSlotBalance.objects.get(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
        )
        self.assertEqual(balance.value, Decimal("1234567"))
        self.assertEqual(balance.amount, Decimal("1.234567"))
        # worth 字段精度为 4 位小数，链上真值精确存于 value；派生的 USD 快照按 4 位落库
        self.assertEqual(balance.worth, Decimal("2.4691"))
        self.assertEqual(balance.synced_block_number, transfer.block)
        self.assertEqual(balance.last_tx_hash, transfer.hash)

    def test_transfer_confirm_sets_vault_slot_balance_worth_zero_without_usd_price(
        self,
    ):
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "a1" * 32,
            hash="0x" + "b2" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "cd" * 20),
            to_address=self.slot_address,
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_003,
            datetime=timezone.now(),
        )
        adapter = type("Adapter", (), {"get_balance": lambda *_args: 1_234_567})()

        # 余额刷新已挪到确认事务提交后（on_commit）执行，测试内需显式触发回调。
        with (
            patch(
                "chains.vault_slot_balances.AdapterFactory.get_adapter",
                return_value=adapter,
            ),
            self.captureOnCommitCallbacks(execute=True),
        ):
            transfer.confirm()

        balance = VaultSlotBalance.objects.get(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
        )
        self.assertEqual(balance.amount, Decimal("1.234567"))
        self.assertEqual(balance.worth, Decimal("0"))

    def test_vault_slot_balance_refresh_skips_older_snapshot(self):
        from chains.vault_slot_balances import refresh_vault_slot_balance

        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("0"),
            amount=Decimal("0"),
            worth=Decimal("0"),
            synced_block_number=200,
            synced_at=timezone.now(),
            last_tx_hash="0x" + "99" * 32,
        )
        adapter = type("Adapter", (), {"get_balance": lambda *_args: 1_234_567})()

        with patch(
            "chains.vault_slot_balances.AdapterFactory.get_adapter",
            return_value=adapter,
        ):
            balance = refresh_vault_slot_balance(
                slot=self.slot,
                crypto=self.crypto,
                trigger_tx_hash="0x" + "aa" * 32,
                block_number=100,
            )

        self.assertEqual(balance.value, Decimal("0"))
        self.assertEqual(balance.amount, Decimal("0"))
        self.assertEqual(balance.synced_block_number, 200)
        self.assertEqual(balance.last_tx_hash, "0x" + "99" * 32)

    def test_transfer_confirm_does_not_refresh_source_vault_slot_balance(self):
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "77" * 32,
            hash="0x" + "88" * 32,
            crypto=self.crypto,
            from_address=self.slot_address,
            to_address=Web3.to_checksum_address("0x" + "de" * 20),
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_003,
            datetime=timezone.now(),
        )

        with patch(
            "chains.vault_slot_balances.AdapterFactory.get_adapter",
        ) as get_adapter:
            transfer.confirm()

        get_adapter.assert_not_called()
        self.assertFalse(
            VaultSlotBalance.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
            ).exists()
        )

    def test_reconcile_creates_pending_collect_for_positive_balance_gap(self):
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("1234567"),
            amount=Decimal("1.234567"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )

        summary = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(summary["created_count"], 1)
        self.assertEqual(summary["failed_blocked_count"], 0)
        schedule = VaultSlotCollectSchedule.objects.get(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            tx_task__isnull=True,
        )
        self.assertLessEqual(schedule.due_at, timezone.now())

    def test_reconcile_stops_after_deadline_and_leaves_rest_for_next_round(self):
        # 协作式预算：截止时刻已过也至少推进一行；未处理的缺口留在批次头部，下一轮接着补。
        import time

        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        other_slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            project=self.project,
            invoice_index=2,
            address=Web3.to_checksum_address("0x" + "bb" * 20),
            salt=b"s" * 32,
        )
        for slot in (self.slot, other_slot):
            VaultSlotBalance.objects.create(
                chain=self.chain,
                vault_slot=slot,
                crypto=self.crypto,
                value=Decimal("1234567"),
                amount=Decimal("1.234567"),
                worth=Decimal("0"),
                synced_at=timezone.now(),
            )

        first = reconcile_vault_slot_collect_balance_gaps(deadline=time.monotonic() - 1)
        self.assertEqual(first["created_count"], 1)
        self.assertEqual(
            VaultSlotCollectSchedule.objects.filter(chain=self.chain).count(), 1
        )

        second = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(second["created_count"], 1)
        self.assertEqual(
            VaultSlotCollectSchedule.objects.filter(chain=self.chain).count(), 2
        )

    def test_reconcile_skips_sub_threshold_dust_balance_gap(self):
        # 粉尘余额（实时价 < 最小归集阈值）不应被安全网补建计划，否则会与
        # try_dispatch_collect 的粉尘删除逻辑反复拉锯（建→删）永不收敛。worth 快照留 0
        # （模拟缺价降级或首次未估值），走 reconcile 的实时价复核路径。
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        self.crypto.prices = {"USD": "2"}
        self.crypto.save(update_fields=["prices"])
        # amount=0.1 × 2 USD = 0.2 < 阈值 1 USD，判定为粉尘。
        balance = VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("100000"),
            amount=Decimal("0.1"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )
        updated_at_before = balance.updated_at

        with patch(
            "core.runtime_settings.get_vault_slot_collect_min_worth_usd",
            return_value=Decimal("1"),
        ):
            summary = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(summary["created_count"], 0)
        self.assertEqual(summary["dust_skipped_count"], 1)
        self.assertFalse(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
            ).exists()
        )
        # 跳过必须回写实时 worth 并推进 updated_at 轮转到队尾：
        # gaps 按 updated_at 升序切 limit 批，原地跳过会让粉尘恒占批次头部。
        balance.refresh_from_db()
        self.assertEqual(balance.worth, Decimal("0.2"))
        self.assertGreater(balance.updated_at, updated_at_before)

    def test_reconcile_readmits_dust_after_price_rise(self):
        # 粉尘判定必须以实时价为唯一权威：worth 快照只在余额刷新时落库，行情任务
        # 只更新 Crypto.prices。若用快照过滤，"当时是粉尘、现已涨价达标"的余额会
        # 被永久挡在安全网之外（无新入账的槽位没有其他路径触发重估）。
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        self.crypto.prices = {"USD": "2"}
        self.crypto.save(update_fields=["prices"])
        balance = VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("100000"),
            amount=Decimal("0.1"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )

        with patch(
            "core.runtime_settings.get_vault_slot_collect_min_worth_usd",
            return_value=Decimal("1"),
        ):
            first = reconcile_vault_slot_collect_balance_gaps()
        self.assertEqual(first["dust_skipped_count"], 1)
        # 跳过已把粉尘 worth 快照回写为 0.2（< 阈值）；此时涨价，快照不会重算。
        balance.refresh_from_db()
        self.assertEqual(balance.worth, Decimal("0.2"))

        self.crypto.prices = {"USD": "20"}
        self.crypto.save(update_fields=["prices"])

        with patch(
            "core.runtime_settings.get_vault_slot_collect_min_worth_usd",
            return_value=Decimal("1"),
        ):
            second = reconcile_vault_slot_collect_balance_gaps()

        # amount=0.1 × 20 USD = 2 ≥ 阈值：尽管 worth 快照仍是粉尘值，
        # 实时价复核必须重新放行并补建归集计划。
        self.assertEqual(second["created_count"], 1)
        self.assertTrue(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

    def test_reconcile_dust_rotation_prevents_starvation(self):
        # 粉尘行跳过后必须轮转到 gaps 队尾：limit 切片被粉尘占满时，
        # 排在其后的真实缺口否则永远轮不到补建。
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        self.crypto.prices = {"USD": "2"}
        self.crypto.save(update_fields=["prices"])
        # 粉尘先建（updated_at 较早，恒排队首）。
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("100000"),
            amount=Decimal("0.1"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )
        real_slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            project=self.project,
            invoice_index=3,
            address=Web3.to_checksum_address("0x" + "ba" * 20),
            salt=b"h" * 32,
        )
        # 达标余额后建：amount=5 × 2 USD = 10 ≥ 阈值。
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=real_slot,
            crypto=self.crypto,
            value=Decimal("5000000"),
            amount=Decimal("5"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )

        with patch(
            "core.runtime_settings.get_vault_slot_collect_min_worth_usd",
            return_value=Decimal("1"),
        ):
            # limit=1 模拟批次被粉尘占满：首轮只轮到粉尘，跳过并轮转到队尾。
            first = reconcile_vault_slot_collect_balance_gaps(limit=1)
            self.assertEqual(first["created_count"], 0)
            self.assertEqual(first["dust_skipped_count"], 1)
            # 次轮粉尘已在队尾，达标余额获得补建，不被饿死。
            second = reconcile_vault_slot_collect_balance_gaps(limit=1)

        self.assertEqual(second["created_count"], 1)
        self.assertTrue(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=real_slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

    def test_reconcile_isolates_single_balance_schedule_errors(self):
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        good_slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            project=self.project,
            invoice_index=2,
            address=Web3.to_checksum_address("0x" + "ac" * 20),
            salt=b"g" * 32,
        )
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("1234567"),
            amount=Decimal("1.234567"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=good_slot,
            crypto=self.crypto,
            value=Decimal("7654321"),
            amount=Decimal("7.654321"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )
        original_ensure_pending_due_now = (
            VaultSlotCollectSchedule.ensure_pending_due_now
        )

        def ensure_pending_due_now_with_one_failure(*, chain, vault_slot, crypto):
            if vault_slot.pk == self.slot.pk:
                raise RuntimeError("bad schedule")
            return original_ensure_pending_due_now(
                chain=chain,
                vault_slot=vault_slot,
                crypto=crypto,
            )

        with patch.object(
            VaultSlotCollectSchedule,
            "ensure_pending_due_now",
            side_effect=ensure_pending_due_now_with_one_failure,
        ):
            summary = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(summary["created_count"], 1)
        self.assertEqual(summary["error_count"], 1)
        self.assertEqual(summary["recent_errors"][0]["vault_slot_id"], self.slot.pk)
        self.assertFalse(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )
        self.assertTrue(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=good_slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

    def test_reconcile_error_rotation_prevents_starvation(self):
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        bad_balance = VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("1234567"),
            amount=Decimal("1.234567"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )
        updated_at_before = bad_balance.updated_at
        good_slot = VaultSlot.objects.create(
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            project=self.project,
            invoice_index=4,
            address=Web3.to_checksum_address("0x" + "bc" * 20),
            salt=b"i" * 32,
        )
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=good_slot,
            crypto=self.crypto,
            value=Decimal("7654321"),
            amount=Decimal("7.654321"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )
        original_ensure_pending_due_now = (
            VaultSlotCollectSchedule.ensure_pending_due_now
        )

        def ensure_pending_due_now_with_one_failure(*, chain, vault_slot, crypto):
            if vault_slot.pk == self.slot.pk:
                raise RuntimeError("bad schedule")
            return original_ensure_pending_due_now(
                chain=chain,
                vault_slot=vault_slot,
                crypto=crypto,
            )

        with patch.object(
            VaultSlotCollectSchedule,
            "ensure_pending_due_now",
            side_effect=ensure_pending_due_now_with_one_failure,
        ):
            first = reconcile_vault_slot_collect_balance_gaps(limit=1)
            second = reconcile_vault_slot_collect_balance_gaps(limit=1)

        self.assertEqual(first["error_count"], 1)
        self.assertEqual(second["created_count"], 1)
        bad_balance.refresh_from_db()
        self.assertGreater(bad_balance.updated_at, updated_at_before)
        self.assertTrue(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=good_slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

    def test_reconcile_does_not_auto_requeue_failed_collect_balance_gap(self):
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        wallet = Wallet.objects.create()
        sender = Address.objects.create(
            wallet=wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address("0x" + "45" * 20),
        )
        failed_task = TxTask.objects.create(
            sender=sender,
            chain=self.chain,
            tx_type=TxTaskType.VaultSlotCollect,
            status=TxTaskStatus.FAILED,
        )
        failed_schedule = VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            due_at=timezone.now() - timedelta(minutes=1),
            tx_task=failed_task,
        )
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("1234567"),
            amount=Decimal("1.234567"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )

        summary = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(summary["created_count"], 0)
        self.assertEqual(summary["failed_blocked_count"], 1)
        self.assertFalse(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

        pending_schedule = failed_schedule.requeue_failed_collect()

        self.assertIsNotNone(pending_schedule)
        self.assertIsNone(pending_schedule.tx_task_id)
        self.assertLessEqual(pending_schedule.due_at, timezone.now())

    def finished_collect_schedule(self, *, status: TxTaskStatus):
        """建一条已绑定终局归集任务的计划（创建顺序即归集发生顺序）。"""
        sender = Address.objects.filter(usage=AddressUsage.HotWallet).first()
        if sender is None:
            sender = Address.objects.create(
                wallet=Wallet.objects.create(),
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
                address_index=0,
                address=Web3.to_checksum_address("0x" + "46" * 20),
            )
        task = TxTask.objects.create(
            sender=sender,
            chain=self.chain,
            tx_type=TxTaskType.VaultSlotCollect,
            status=status,
        )
        return VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            due_at=timezone.now() - timedelta(minutes=1),
            tx_task=task,
        )

    def create_positive_balance_gap(self):
        VaultSlotBalance.objects.create(
            chain=self.chain,
            vault_slot=self.slot,
            crypto=self.crypto,
            value=Decimal("1234567"),
            amount=Decimal("1.234567"),
            worth=Decimal("0"),
            synced_at=timezone.now(),
        )

    def test_reconcile_resumes_after_manual_requeue_succeeds(self):
        # 失败 → 人工重新排队并成功 → 之后再出现余额缺口：旧 FAILED 计划作为历史保留，
        # 但已不代表当前状态，安全网必须恢复自动补建，不能永久挡住该槽位×币种。
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        self.finished_collect_schedule(status=TxTaskStatus.FAILED)
        self.finished_collect_schedule(status=TxTaskStatus.SUCCEEDED)
        self.create_positive_balance_gap()

        summary = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(summary["failed_blocked_count"], 0)
        self.assertEqual(summary["created_count"], 1)
        self.assertTrue(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

    def test_reconcile_blocks_again_when_latest_collect_failed_after_success(self):
        # 最近一次归集以失败收场（成功之后又失败）仍需人工确认，避免永久 revert 被反复烧 gas。
        from chains.vault_slot_balances import reconcile_vault_slot_collect_balance_gaps

        self.finished_collect_schedule(status=TxTaskStatus.SUCCEEDED)
        self.finished_collect_schedule(status=TxTaskStatus.FAILED)
        self.create_positive_balance_gap()

        summary = reconcile_vault_slot_collect_balance_gaps()

        self.assertEqual(summary["failed_blocked_count"], 1)
        self.assertEqual(summary["created_count"], 0)
        self.assertFalse(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=self.slot,
                crypto=self.crypto,
                tx_task__isnull=True,
            ).exists()
        )

    def test_requeue_action_only_available_to_superuser(self):
        # 重新排队失败归集会触发链上归集交易、消耗热钱包 gas，属资金治理操作。
        # ReadOnlyModelAdmin 下 view 是所有查看者的基线权限，必须把该 action 收口到
        # 超管，避免只读审计员借 view 权限触发动钱动作。
        from django.contrib import admin as django_admin
        from django.test import RequestFactory

        from chains.admin import VaultSlotCollectScheduleAdmin
        from users.models import User

        admin_instance = VaultSlotCollectScheduleAdmin(
            VaultSlotCollectSchedule, django_admin.site
        )
        request_factory = RequestFactory()

        viewer = User.objects.create_user(
            username="collect-viewer", password="secret", is_staff=True
        )
        viewer_request = request_factory.get("/")
        viewer_request.user = viewer
        self.assertFalse(admin_instance.has_requeue_permission(viewer_request))
        self.assertNotIn(
            "requeue_failed_collect_schedules",
            admin_instance.get_actions(viewer_request),
        )

        superuser = User.objects.create_superuser(
            username="collect-admin", password="secret"
        )
        superuser_request = request_factory.get("/")
        superuser_request.user = superuser
        self.assertTrue(admin_instance.has_requeue_permission(superuser_request))
        self.assertIn(
            "requeue_failed_collect_schedules",
            admin_instance.get_actions(superuser_request),
        )


def locked_transfer_row(queries) -> bool:
    """本次调用是否对 chains_transfer 行发出了 SELECT ... FOR UPDATE。"""
    return any(
        "FOR UPDATE" in query["sql"] and '"chains_transfer"' in query["sql"]
        for query in queries.captured_queries
    )


class TransferProcessQuickConfirmDispatchTests(TestCase):
    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="Quick Confirm Coin",
            symbol="QCC",
            coingecko_id="quick-confirm-coin",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)

    def create_quick_transfer(self) -> Transfer:
        return Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "11" * 32,
            hash="0x" + "12" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "13" * 20),
            to_address=Web3.to_checksum_address("0x" + "14" * 20),
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_010,
            datetime=timezone.now(),
            confirm_mode=ConfirmMode.QUICK,
        )

    @patch("deposits.service.DepositService.try_match_deposit_transfer")
    @patch("invoices.service.InvoiceService.try_match_invoice")
    def test_quick_confirm_is_enqueued_urgently_in_process_transaction(
        self,
        match_invoice_mock,
        match_deposit_mock,
    ):
        match_invoice_mock.return_value = False
        match_deposit_mock.return_value = False
        transfer = self.create_quick_transfer()

        transfer.process()

        [job] = queued_jobs("chains.confirm_transfer")
        self.assertEqual(job.args, {"transfer_id": transfer.pk})
        self.assertEqual(job.priority, PRIORITY_URGENT)
        self.assertEqual(job.queueing_lock, f"chains.confirm_transfer:{transfer.pk}")

    @patch("deposits.service.DepositService.try_match_deposit_transfer")
    @patch("invoices.service.InvoiceService.try_match_invoice")
    def test_quick_confirm_is_discarded_when_outer_transaction_rolls_back(
        self,
        match_invoice_mock,
        match_deposit_mock,
    ):
        match_invoice_mock.return_value = False
        match_deposit_mock.return_value = False
        transfer = self.create_quick_transfer()

        with self.assertRaises(RuntimeError), transaction.atomic():
            transfer.process()
            raise RuntimeError("rollback")

        self.assertEqual(queued_jobs("chains.confirm_transfer"), [])

    @patch("invoices.service.InvoiceService.try_match_invoice")
    def test_process_on_stale_instance_skips_row_already_processed(
        self,
        match_invoice_mock,
    ):
        # 另一个 worker 已处理完同一行：拿着旧快照的实例必须在加锁时读到最新
        # processed_at，不能重新撮合业务。
        transfer = self.create_quick_transfer()
        Transfer.objects.filter(pk=transfer.pk).update(processed_at=timezone.now())

        with CaptureQueriesContext(connection) as queries:
            transfer.process()

        match_invoice_mock.assert_not_called()
        self.assertIsNotNone(transfer.processed_at)
        self.assertTrue(locked_transfer_row(queries))

    @patch("chains.models.Transfer._dispatch_business_confirm")
    def test_confirm_on_stale_instance_skips_row_already_confirmed(
        self,
        dispatch_confirm_mock,
    ):
        transfer = self.create_quick_transfer()
        Transfer.objects.filter(pk=transfer.pk).update(status=TransferStatus.CONFIRMED)

        with CaptureQueriesContext(connection) as queries:
            transfer.confirm()

        dispatch_confirm_mock.assert_not_called()
        self.assertEqual(transfer.status, TransferStatus.CONFIRMED)
        self.assertTrue(locked_transfer_row(queries))

    def test_drop_on_already_deleted_transfer_is_noop(self):
        transfer = self.create_quick_transfer()
        Transfer.objects.filter(pk=transfer.pk).delete()

        transfer.drop()

        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())


class ConfirmTransferMissingReceiptTests(TestCase):
    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="Confirm Missing Coin",
            symbol="CMC",
            coingecko_id="confirm-missing-coin",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "bb" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "13" * 20),
            to_address=Web3.to_checksum_address("0x" + "14" * 20),
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_004,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
            processed_at=timezone.now(),
        )

    def confirm_with_result(self, result, *, attempts: int = 0):
        from chains.tasks import confirm_transfer

        adapter = Mock()
        adapter.tx_result.return_value = result
        with patch("chains.tasks.AdapterFactory.get_adapter", return_value=adapter):
            confirm_transfer(
                SimpleNamespace(job=SimpleNamespace(attempts=attempts)),
                transfer_id=self.transfer.pk,
            )

    def test_missing_receipt_before_final_attempt_is_retried(self):
        from chains.tasks import CONFIRM_TRANSFER_RETRY
        from chains.tasks import ReceiptNotVisibleError

        with self.assertRaises(ReceiptNotVisibleError) as error:
            self.confirm_with_result(TxCheckResult(status=TxCheckStatus.MISSING))

        job = SimpleNamespace(attempts=0)
        self.assertIsNotNone(
            CONFIRM_TRANSFER_RETRY.get_retry_decision(
                exception=error.exception, job=job
            )
        )

    def test_missing_receipt_on_final_attempt_keeps_transfer_without_error(self):
        from chains.tasks import CONFIRM_TRANSFER_RETRY

        self.confirm_with_result(
            TxCheckResult(status=TxCheckStatus.MISSING),
            attempts=len(CONFIRM_TRANSFER_RETRY.delays),
        )

        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, TransferStatus.CONFIRMING)

    def test_chain_query_failure_is_raised_for_retry(self):
        with self.assertRaises(RuntimeError):
            self.confirm_with_result(RuntimeError("rpc down"))

        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, TransferStatus.CONFIRMING)

    def test_failed_receipt_drops_transfer_instead_of_blocking_confirm_batch(self):
        # 链上事实变为失败时必须丢弃：继续保留会让该转账永久停在 CONFIRMING、
        # 每次链高推进都被重新入队确认，且 reap 只告警不清理。
        self.confirm_with_result(TxCheckResult(status=TxCheckStatus.FAILED))

        # 记录被删除，(chain, hash, event_index) 唯一约束随之释放，
        # 日后若该 tx 再次被成功打包，扫描器可自然重建。
        self.assertFalse(Transfer.objects.filter(pk=self.transfer.pk).exists())


class EnqueueDueConfirmationsTests(TestCase):
    """链高推进时把到期的转账入队确认，判据与去重。"""

    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="Ether BN",
            symbol="ETH-BN",
            coingecko_id="ether-bn",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum, latest_block_number=200)
        self.sequence = 0

    def make_transfer(self, *, chain=None, **overrides) -> Transfer:
        self.sequence += 1
        fields = {
            "chain": chain or self.chain,
            "block": 190,
            "block_hash": "0x" + "aa" * 32,
            "hash": "0x" + f"{self.sequence:064x}",
            "crypto": self.crypto,
            "from_address": Web3.to_checksum_address("0x" + "e1" * 20),
            "to_address": Web3.to_checksum_address("0x" + "e2" * 20),
            "value": "1",
            "amount": "1",
            "timestamp": 1_700_000_000 + self.sequence,
            "datetime": timezone.now(),
            "status": TransferStatus.CONFIRMING,
            "confirm_mode": ConfirmMode.QUICK,
            "type": TransferType.Deposit,
            "processed_at": timezone.now(),
        }
        fields.update(overrides)
        transfer = Transfer.objects.create(**fields)
        # FULL 要求创建超过 10s；默认把创建时间拨到足够早。
        Transfer.objects.filter(pk=transfer.pk).update(
            created_at=timezone.now() - timedelta(minutes=1)
        )
        return transfer

    def enqueue(self) -> dict[int, int]:
        from chains.tasks import enqueue_due_confirmations

        enqueue_due_confirmations(self.chain)
        return {
            job.args["transfer_id"]: job.priority
            for job in queued_jobs("chains.confirm_transfer")
        }

    def test_quick_ignores_depth_and_full_requires_depth_and_age(self):
        depth = self.chain.confirm_block_count
        quick_shallow = self.make_transfer(block=200)
        full_deep = self.make_transfer(confirm_mode=ConfirmMode.FULL, block=200 - depth)
        full_shallow = self.make_transfer(
            confirm_mode=ConfirmMode.FULL, block=200 - depth + 1
        )
        full_fresh = self.make_transfer(confirm_mode=ConfirmMode.FULL, block=1)
        Transfer.objects.filter(pk=full_fresh.pk).update(created_at=timezone.now())

        enqueued = self.enqueue()

        self.assertEqual(
            enqueued,
            {quick_shallow.pk: PRIORITY_URGENT, full_deep.pk: PRIORITY_DEFAULT},
        )
        self.assertNotIn(full_shallow.pk, enqueued)

    def test_only_processed_confirming_transfers_of_this_chain(self):
        other_chain = make_evm_chain(code=ChainCode.BSC, latest_block_number=200)
        due = self.make_transfer()
        self.make_transfer(processed_at=None)
        self.make_transfer(status=TransferStatus.CONFIRMED)
        self.make_transfer(chain=other_chain)

        self.assertEqual(list(self.enqueue()), [due.pk])

    def test_already_pending_confirmation_is_not_enqueued_again(self):
        # 已有待执行（含等待中的重试）或执行中的确认任务时不再入队。
        from chains.tasks import confirm_transfer

        waiting_retry = self.make_transfer()
        running = self.make_transfer()
        confirm_transfer.configure(
            queueing_lock=f"chains.confirm_transfer:{waiting_retry.pk}",
            schedule_in={"minutes": 5},
        ).defer(transfer_id=waiting_retry.pk)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, queueing_lock, "
                "args, status) VALUES ('default', 'chains.confirm_transfer', %s, %s, "
                "'doing')",
                [f"chains.confirm_transfer:{running.pk}", '{"transfer_id": 0}'],
            )

        self.enqueue()

        self.assertEqual(
            sorted(job.queueing_lock for job in queued_jobs("chains.confirm_transfer")),
            sorted(
                [
                    f"chains.confirm_transfer:{waiting_retry.pk}",
                    f"chains.confirm_transfer:{running.pk}",
                ]
            ),
        )

    def test_batch_size_caps_each_round_oldest_first(self):
        transfers = [self.make_transfer() for _ in range(3)]

        with patch("chains.tasks.CONFIRM_ENQUEUE_BATCH_SIZE", 2):
            first = self.enqueue()
            second = self.enqueue()

        self.assertEqual(list(first), [transfers[0].pk, transfers[1].pk])
        self.assertEqual(
            list(second), [transfers[0].pk, transfers[1].pk, transfers[2].pk]
        )

    def test_dispatch_skips_when_chain_height_did_not_advance(self):
        from chains.tasks import dispatch_block_confirmation_checks_if_needed

        self.make_transfer()

        dispatch_block_confirmation_checks_if_needed(
            chain=self.chain, previous_latest_block=200
        )
        self.assertEqual(queued_jobs("chains.confirm_transfer"), [])

        dispatch_block_confirmation_checks_if_needed(
            chain=self.chain, previous_latest_block=199
        )
        self.assertEqual(len(queued_jobs("chains.confirm_transfer")), 1)


class ProcessTransferRetryTests(SimpleTestCase):
    """process_transfer 死锁自动重试。

    PostgreSQL 死锁的设计前提是被牺牲方应重试；高并发场景里 try_match_invoice /
    confirm_invoice 等行锁链路会偶发 deadlock。若不重试，单次死锁就会让 Transfer
    卡到兜底派发才被重新处理；其它异常不重试，避免确定性错误被反复执行。
    """

    def test_deadlock_is_retried_and_other_errors_are_not(self):
        from django.db import OperationalError

        strategy = process_transfer.retry_strategy
        job = SimpleNamespace(attempts=0)

        self.assertIsNotNone(
            strategy.get_retry_decision(
                exception=OperationalError("deadlock detected"), job=job
            )
        )
        self.assertIsNone(
            strategy.get_retry_decision(exception=ValueError("bad data"), job=job)
        )


class ReapStaleConfirmingTransfersTests(TestCase):
    """超龄 CONFIRMING 转账清理：删除必须以链上终验 MISSING 为前提。

    纯按时间删除会在确认管线自身故障超过时限时误删真实入账（invoice 被解绑、
    扫描游标已过无法重建）；链上仍 SUCCEEDED 的必须保留并补派确认。
    """

    def setUp(self):
        from chains.tasks import STALE_CONFIRMING_TRANSFER_MAX_AGE_HOURS

        self.crypto = Crypto.objects.create(
            name="Ether Reap",
            symbol="ETH-REAP",
            coingecko_id="ether-reap",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.stale_created_at = timezone.now() - timedelta(
            hours=STALE_CONFIRMING_TRANSFER_MAX_AGE_HOURS + 1
        )

    def make_confirming_transfer(self, *, suffix: str, created_at=None) -> Transfer:
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "aa" * 32,
            hash="0x" + suffix * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000b1"
            ),
            to_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000b2"
            ),
            value=1000,
            amount=1,
            timestamp=1700000000,
            datetime=timezone.now(),
        )
        Transfer.objects.filter(pk=transfer.pk).update(
            created_at=created_at or self.stale_created_at
        )
        return transfer

    def reap_with_tx_result(self, tx_result):
        from chains.tasks import reap_stale_confirming_transfers

        with patch("chains.tasks.AdapterFactory.get_adapter") as get_adapter:
            get_adapter.return_value.tx_result.return_value = tx_result
            return reap_stale_confirming_transfers()

    def test_reaps_stale_transfer_when_chain_fact_is_missing(self):
        transfer = self.make_confirming_transfer(suffix="1a")

        reaped = self.reap_with_tx_result(TxCheckResult(status=TxCheckStatus.MISSING))

        self.assertEqual(reaped, 1)
        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_keeps_stale_transfer_and_redispatches_confirm_when_succeeded(self):
        transfer = self.make_confirming_transfer(suffix="2a")
        # 已完成业务归类的转账才允许直接补派确认。
        Transfer.objects.filter(pk=transfer.pk).update(processed_at=timezone.now())

        reaped = self.reap_with_tx_result(TxCheckResult(status=TxCheckStatus.SUCCEEDED))

        self.assertEqual(reaped, 0)
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())
        self.assertEqual(
            queued_args("chains.confirm_transfer"), [{"transfer_id": transfer.pk}]
        )

    def test_succeeded_unprocessed_transfer_redispatches_process_not_confirm(self):
        # 业务归类未完成（processed_at 为空、type 仍 Unmatched）时绝不能直接确认：
        # confirm 会把状态推成 CONFIRMED 但业务分发空转，之后补上的匹配再也等不到
        # 确认动作，造成链上有钱、账上无单。必须改派 process，确认交回正常调度。
        transfer = self.make_confirming_transfer(suffix="7a")

        reaped = self.reap_with_tx_result(TxCheckResult(status=TxCheckStatus.SUCCEEDED))

        self.assertEqual(reaped, 0)
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())
        self.assertEqual(queued_args("chains.confirm_transfer"), [])
        self.assertEqual(
            queued_args("chains.process_transfer"), [{"transfer_id": transfer.pk}]
        )

    def test_stops_after_budget_and_leaves_rest_for_next_round(self):
        # 协作式预算：预算为 0 时也至少终验一条；剩余超龄转账按 created_at 顺序下一轮继续。
        older = self.make_confirming_transfer(suffix="8a")
        newer = self.make_confirming_transfer(
            suffix="8b",
            created_at=self.stale_created_at + timedelta(minutes=1),
        )

        with patch("chains.tasks.REAP_STALE_CONFIRMING_BUDGET_SECONDS", 0):
            reaped = self.reap_with_tx_result(
                TxCheckResult(status=TxCheckStatus.MISSING)
            )

        self.assertEqual(reaped, 1)
        self.assertFalse(Transfer.objects.filter(pk=older.pk).exists())
        self.assertTrue(Transfer.objects.filter(pk=newer.pk).exists())

        reaped = self.reap_with_tx_result(TxCheckResult(status=TxCheckStatus.MISSING))

        self.assertEqual(reaped, 1)
        self.assertFalse(Transfer.objects.filter(pk=newer.pk).exists())

    def test_keeps_stale_transfer_when_chain_query_fails(self):
        transfer = self.make_confirming_transfer(suffix="3a")

        reaped = self.reap_with_tx_result(RuntimeError("rpc down"))

        self.assertEqual(reaped, 0)
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_keeps_stale_transfer_when_chain_fact_is_failed(self):
        transfer = self.make_confirming_transfer(suffix="4a")

        reaped = self.reap_with_tx_result(TxCheckResult(status=TxCheckStatus.FAILED))

        self.assertEqual(reaped, 0)
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_ignores_fresh_confirming_transfer(self):
        transfer = self.make_confirming_transfer(
            suffix="5a",
            created_at=timezone.now(),
        )

        from chains.tasks import reap_stale_confirming_transfers

        with patch("chains.tasks.AdapterFactory.get_adapter") as get_adapter:
            reaped = reap_stale_confirming_transfers()

        self.assertEqual(reaped, 0)
        get_adapter.assert_not_called()
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())

    def test_ignores_confirmed_transfer_regardless_of_age(self):
        transfer = self.make_confirming_transfer(suffix="6a")
        Transfer.objects.filter(pk=transfer.pk).update(status=TransferStatus.CONFIRMED)

        reaped = self.reap_with_tx_result(TxCheckResult(status=TxCheckStatus.MISSING))

        self.assertEqual(reaped, 0)
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())


class TransferHandlerRegistryTests(TestCase):
    """chains 经注册表把入账交给业务处理器；装配缺失时必须失败，不能静默吞掉入账。"""

    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="Registry Coin",
            symbol="RGC",
            coingecko_id="registry-coin",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "21" * 32,
            hash="0x" + "22" * 32,
            crypto=self.crypto,
            from_address=Web3.to_checksum_address("0x" + "23" * 20),
            to_address=Web3.to_checksum_address("0x" + "24" * 20),
            value=Decimal("1000000"),
            amount=Decimal("1"),
            timestamp=1_700_000_020,
            datetime=timezone.now(),
        )

    def test_process_without_handlers_keeps_transfer_unprocessed(self):
        # 没有任何处理器时若照常标记已处理，这笔入账将永远不会再被匹配。
        with (
            patch.dict("chains.registry._transfer_handlers", clear=True),
            self.assertRaises(ImproperlyConfigured),
        ):
            self.transfer.process()

        self.transfer.refresh_from_db()
        self.assertIsNone(self.transfer.processed_at)

    @patch("deposits.service.DepositService.try_match_deposit_transfer")
    @patch("invoices.service.InvoiceService.try_match_invoice")
    def test_process_stops_at_first_matching_handler(
        self, match_invoice_mock, match_deposit_mock
    ):
        match_invoice_mock.return_value = True

        self.transfer.process()

        match_invoice_mock.assert_called_once()
        match_deposit_mock.assert_not_called()
        self.transfer.refresh_from_db()
        self.assertIsNotNone(self.transfer.processed_at)

    @patch("deposits.service.DepositService.try_match_deposit_transfer")
    @patch("invoices.service.InvoiceService.try_match_invoice")
    def test_process_falls_through_to_deposit_when_invoice_does_not_match(
        self, match_invoice_mock, match_deposit_mock
    ):
        match_invoice_mock.return_value = False
        match_deposit_mock.return_value = True

        self.transfer.process()

        match_invoice_mock.assert_called_once()
        match_deposit_mock.assert_called_once()

    def test_confirm_classified_transfer_without_handler_rolls_back(self):
        Transfer.objects.filter(pk=self.transfer.pk).update(type=TransferType.Deposit)
        self.transfer.refresh_from_db()

        with (
            patch.dict("chains.registry._transfer_handlers", clear=True),
            self.assertRaises(ImproperlyConfigured),
        ):
            self.transfer.confirm()

        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, TransferStatus.CONFIRMING)

    def test_conflicting_chain_family_registration_is_rejected(self):
        with (
            patch.dict("chains.registry._chain_families"),
            self.assertRaises(ImproperlyConfigured),
        ):
            register_chain_family(
                ChainFamily(
                    chain_type=ChainType.EVM,
                    adapter_class=TxCheckStatus,
                    vault_slot_backend=object(),
                    scan_round=Mock(),
                    poll_round=Mock(),
                    dispatch_round=Mock(),
                )
            )

    def test_registry_check_reports_missing_registrations(self):
        with (
            patch.dict("chains.registry._chain_families", clear=True),
            patch.dict("chains.registry._transfer_handlers", clear=True),
        ):
            errors = chain_registry_check()

        self.assertEqual(
            {error.id for error in errors},
            {"chains.E002", "chains.E003"},
        )
