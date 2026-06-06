from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import checks
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Address
from chains.models import AddressChainState
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
from chains.models import VaultSlotUsage
from chains.models import Wallet
from chains.tasks import process_transfer
from chains.tests_fixtures import make_evm_chain
from chains.transfer_matching import addresses_equal
from chains.transfer_matching import raw_amount
from chains.transfer_matching import transfer_matches
from currencies.models import ChainCryptoDeployment
from currencies.models import Crypto
from projects.models import Project


class TransferMatchingTests(TestCase):
    def test_addresses_equal_normalizes_evm_addresses(self):
        chain = make_evm_chain(code=ChainCode.Ethereum)
        checksum = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000abc"
        )

        self.assertTrue(addresses_equal(checksum.lower(), checksum, chain=chain))

    def test_transfer_matches_uses_raw_value_and_chain_specific_address_rules(self):
        native = Crypto.objects.create(
            name="Transfer Match Coin",
            symbol="TMC",
            coingecko_id="transfer-match-coin",
        )
        chain = make_evm_chain(code=ChainCode.BSC)
        # 精度以 ChainCryptoDeployment 为唯一真相；非空合约地址避免与原生币 address="" 行冲突。
        ChainCryptoDeployment.objects.create(
            crypto=native,
            chain=chain,
            address=Web3.to_checksum_address("0x" + "11" * 20),
            decimals=6,
        )
        from_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000001"
        )
        to_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000002"
        )
        transfer = Transfer.objects.create(
            chain=chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "1" * 64,
            crypto=native,
            from_address=from_address.lower(),
            to_address=to_address.lower(),
            value=Decimal("1234567"),
            amount=Decimal("1.234567"),
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMED,
            type=TransferType.Collect,
        )

        expected_value = raw_amount(
            amount=Decimal("1.234567"),
            crypto=native,
            chain=chain,
        )

        self.assertTrue(
            transfer_matches(
                transfer,
                chain=chain,
                crypto=native,
                from_address=from_address,
                to_address=to_address,
                value=expected_value,
            )
        )

    def test_raw_amount_truncates_when_business_amount_exceeds_chain_decimals(self):
        # 业务 amount 的小数位超过 crypto.decimals 时，
        # 链上 raw value 必然被向下截断；raw_amount 必须与 broadcast 端 `int(...)` 对齐，
        # 否则 transfer_matches 的严格 == 比对会永久失败，已上链的转账无法被认领。
        crypto = Crypto.objects.create(
            name="Raw Amount Trunc",
            symbol="RAT",
            coingecko_id="raw-amount-trunc",
        )
        chain = make_evm_chain(code=ChainCode.Polygon)
        # 精度以 ChainCryptoDeployment 为唯一真相；非空合约地址避免与原生币 address="" 行冲突。
        ChainCryptoDeployment.objects.create(
            crypto=crypto,
            chain=chain,
            address=Web3.to_checksum_address("0x" + "22" * 20),
            decimals=6,
        )

        value_raw = int(Decimal("0.01480216") * Decimal(10**6))
        self.assertEqual(value_raw, 14802)
        self.assertEqual(
            raw_amount(amount=Decimal("0.01480216"), crypto=crypto, chain=chain),
            Decimal(14802),
        )

        from_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000003"
        )
        to_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000004"
        )
        transfer = Transfer.objects.create(
            chain=chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "2" * 64,
            crypto=crypto,
            from_address=from_address.lower(),
            to_address=to_address.lower(),
            value=Decimal(value_raw),
            amount=Decimal("0.014802"),
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMED,
            type=TransferType.Collect,
        )

        self.assertTrue(
            transfer_matches(
                transfer,
                chain=chain,
                crypto=crypto,
                from_address=from_address,
                to_address=to_address,
                value=raw_amount(
                    amount=Decimal("0.01480216"),
                    crypto=crypto,
                    chain=chain,
                ),
            )
        )


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

        with patch.object(
            Address.objects, "get_or_create", side_effect=fake_get_or_create
        ), self.assertRaises(IntegrityError):
            wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
                address_index=0,
            )


class AddressChainStateAcquireTests(TestCase):
    def setUp(self):
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000f0"
            ),
        )

    def test_acquire_for_update_falls_back_to_locked_read_after_integrity_error(self):
        # 模拟并发首次创建撞唯一约束的场景：get_or_create 抛 IntegrityError 时
        # 必须用 select_for_update 加锁回查到已提交的 state，而不是抛 DoesNotExist。
        existing_state = AddressChainState.objects.create(
            address=self.address,
            chain=self.chain,
        )

        def fake_get_or_create(**_kwargs):
            raise IntegrityError("duplicate key value violates unique constraint")

        with patch.object(
            AddressChainState.objects, "get_or_create", side_effect=fake_get_or_create
        ):
            state = AddressChainState.acquire_for_update(
                address=self.address,
                chain=self.chain,
            )

        self.assertEqual(state.pk, existing_state.pk)
        self.assertEqual(state.address, self.address)
        self.assertEqual(state.chain, self.chain)

    def test_acquire_for_update_raises_doesnotexist_when_state_truly_absent(self):
        # IntegrityError 后 state 确实不存在时（非身份冲突的其他约束错误），
        # 加锁回查必须抛 DoesNotExist，供上层感知真错误，而不是吞掉。
        def fake_get_or_create(**_kwargs):
            raise IntegrityError("unrelated unique constraint")

        with patch.object(
            AddressChainState.objects, "get_or_create", side_effect=fake_get_or_create
        ), self.assertRaises(AddressChainState.DoesNotExist):
            AddressChainState.acquire_for_update(
                address=self.address,
                chain=self.chain,
            )


class WalletGenerationTests(TestCase):
    """钱包生成与地址派生已在主系统内部闭环（chains/keys.py）。"""

    def test_generate_creates_distinct_encrypted_mnemonics(self):
        # 每个钱包持有独立助记词；密文非空、互不相同，且能解密回 24 词助记词。
        wallet_a = Wallet.generate()
        wallet_b = Wallet.generate()

        self.assertTrue(wallet_a.encrypted_mnemonic)
        self.assertTrue(wallet_b.encrypted_mnemonic)
        self.assertNotEqual(
            wallet_a.encrypted_mnemonic, wallet_b.encrypted_mnemonic
        )
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

        with patch(
            "chains.service.Chain.objects.select_for_update",
            wraps=Chain.objects.select_for_update,
        ) as lock_mock:
            result = TransferService.create_observed_transfer(observed=self.payload)

        self.assertTrue(result.created)
        self.assertFalse(result.conflict)
        self.assertIsNotNone(result.transfer)
        lock_mock.assert_called_once()
        enqueue_mock.assert_called_once()

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
            status=TxTaskStatus.PENDING_CONFIRM,
        )

    def test_mark_finalized_success_transitions_correctly(self):
        updated = TxTask.mark_finalized_success(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.CONFIRMED)

    def test_reset_to_pending_chain_transitions_correctly(self):
        updated = TxTask.reset_to_pending_chain(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.PENDING_CHAIN)

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
        self.assertEqual(self.task.status, TxTaskStatus.CONFIRMED)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_reset_to_pending_chain_can_resolve_old_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "ef" * 32)

        updated = TxTask.reset_to_pending_chain(
            chain=self.chain,
            tx_hash=old_hash,
        )

        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.PENDING_CHAIN)
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
            expected_status=TxTaskStatus.PENDING_CHAIN,
        )

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.PENDING_CONFIRM)

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
        self.assertEqual(self.task.status, TxTaskStatus.CONFIRMED)

    def test_mark_pending_confirm_skips_finalized_tasks(self):
        # 先将任务标记为已确认终局
        TxTask.mark_finalized_success(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        # mark_pending_confirm 不应回退已终局的任务
        updated = TxTask.mark_pending_confirm(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.CONFIRMED)

    def test_mark_pending_confirm_with_empty_hash_is_noop(self):
        updated = TxTask.mark_pending_confirm(chain=self.chain, tx_hash="")
        self.assertFalse(updated)

    def test_mark_pending_confirm_can_resolve_old_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "f0" * 32)

        updated = TxTask.mark_pending_confirm(
            chain=self.chain,
            tx_hash=old_hash,
        )

        self.assertTrue(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.PENDING_CONFIRM)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_reset_to_pending_chain_skips_non_pending_confirm_tasks(self):
        TxTask.objects.filter(pk=self.task.pk).update(
            status=TxTaskStatus.QUEUED,
        )
        updated = TxTask.reset_to_pending_chain(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        self.assertFalse(updated)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, TxTaskStatus.QUEUED)


class VaultSlotReceivedFlagTests(TestCase):
    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="VaultSlot Received Flag Coin",
            symbol="VRF",
            coingecko_id="vaultslot-received-flag",
        )
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
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


class BlockNumberUpdatedCompensationTests(TestCase):
    """验证 block_number_updated 在满批时自调度补偿。"""

    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="Ether BN",
            symbol="ETH-BN",
            coingecko_id="ether-bn",
        )
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum, latest_block_number=200
        )
        self.wallet = Wallet.objects.create()
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000e1"
            ),
        )

    @patch("chains.tasks.block_number_updated.apply_async")
    @patch("chains.tasks.confirm_transfer.delay")
    def test_reschedules_when_quick_batch_is_full(
        self, confirm_delay_mock, reschedule_mock
    ):
        from chains.tasks import block_number_updated

        # 创建 17 个 QUICK 模式的 confirming 转账（超过 BATCH_SIZE=16）
        for i in range(17):
            Transfer.objects.create(
                chain=self.chain,
                block=190,
                block_hash="0x" + "aa" * 32,
                hash="0x" + f"{i:064x}",
                crypto=self.crypto,
                from_address=self.addr.address,
                to_address=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000e2"
                ),
                value="1",
                amount="1",
                timestamp=1700000000 + i,
                datetime=timezone.now(),
                status=TransferStatus.CONFIRMING,
                confirm_mode=ConfirmMode.QUICK,
                type=TransferType.Deposit,
                processed_at=timezone.now(),
            )

        block_number_updated.run(self.chain.pk)

        # 应派发 16 个确认任务
        self.assertEqual(confirm_delay_mock.call_count, 16)
        # 应自调度一次补偿
        reschedule_mock.assert_called_once_with(args=(self.chain.pk,), countdown=2)

    @patch("chains.tasks.block_number_updated.apply_async")
    @patch("chains.tasks.confirm_transfer.delay")
    def test_no_reschedule_when_batch_not_full(
        self, confirm_delay_mock, reschedule_mock
    ):
        from chains.tasks import block_number_updated

        # 只创建 3 个转账，不满批
        for i in range(3):
            Transfer.objects.create(
                chain=self.chain,
                block=190,
                block_hash="0x" + "aa" * 32,
                hash="0x" + f"{i+100:064x}",
                crypto=self.crypto,
                from_address=self.addr.address,
                to_address=Web3.to_checksum_address(
                    "0x00000000000000000000000000000000000000e2"
                ),
                value="1",
                amount="1",
                timestamp=1700000000 + i,
                datetime=timezone.now(),
                status=TransferStatus.CONFIRMING,
                confirm_mode=ConfirmMode.QUICK,
                type=TransferType.Deposit,
                processed_at=timezone.now(),
            )

        block_number_updated.run(self.chain.pk)

        self.assertEqual(confirm_delay_mock.call_count, 3)
        reschedule_mock.assert_not_called()


class ProcessTransferAutoretryTests(SimpleTestCase):
    """process_transfer 死锁自动重试回归测试。

    PostgreSQL 死锁的设计前提是被牺牲方应重试；StressRun 高并发场景里，
    `try_match_invoice` / `confirm_invoice` 等行锁链路会偶发 deadlock。
    若 process_transfer 不配置 OperationalError 重试，单次死锁就会让
    Transfer 永久卡在未处理状态，对应的账单也无法被匹配。
    """

    def test_process_transfer_autoretries_on_database_deadlock(self):
        from django.db import OperationalError

        self.assertIn(
            OperationalError,
            process_transfer.autoretry_for,
            "process_transfer 必须把 OperationalError 配进 autoretry_for "
            "以覆盖 PG deadlock detected",
        )
        self.assertGreaterEqual(
            process_transfer.max_retries, 1,
            "max_retries 必须 >= 1 才能真正触发重试",
        )
        self.assertTrue(
            getattr(process_transfer, "retry_backoff", False),
            "retry_backoff 必须启用，避免死锁后密集重试加剧锁冲突",
        )
