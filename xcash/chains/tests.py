import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import Mock
from unittest.mock import patch

import httpx
import pytest
from django.core import checks
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings
from django.utils import timezone
from web3 import Web3
from web3.exceptions import ExtraDataLengthError

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
from chains.models import TxTaskStage
from chains.models import TxTaskType
from chains.models import Wallet
from chains.service import ObservedTransferPayload
from chains.service import TransferService
from chains.signer import RemoteSignerBackend
from chains.signer import SignerAdminSummary
from chains.signer import SignerServiceError
from chains.signer import build_signer_signature_payload
from chains.signer import get_signer_backend
from chains.tasks import block_number_updated
from chains.tasks import process_transfer
from chains.transfer_matching import addresses_equal
from chains.transfer_matching import raw_amount
from chains.transfer_matching import transfer_matches
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.choices import TxKind
from evm.constants import DEFAULT_BASE_TRANSFER_GAS
from evm.constants import DEFAULT_ERC20_TRANSFER_GAS


class TransferMatchingTests(TestCase):
    def test_addresses_equal_normalizes_evm_addresses(self):
        chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        checksum = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000abc"
        )

        self.assertTrue(addresses_equal(checksum.lower(), checksum, chain=chain))

    def test_transfer_matches_uses_raw_value_and_chain_specific_address_rules(self):
        native = Crypto.objects.create(
            name="Transfer Match Coin",
            symbol="TMC",
            coingecko_id="transfer-match-coin",
            decimals=6,
        )
        chain = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="",
            active=True,
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
            type=TransferType.Withdrawal,
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
        # 业务 amount 的小数位超过 crypto.decimals 时（如压测用 8 位精度生成 USDT 提币），
        # 链上 raw value 必然被向下截断；raw_amount 必须与 broadcast 端 `int(...)` 对齐，
        # 否则 transfer_matches 的严格 == 比对会永久失败，已上链的提币无法被认领。
        crypto = Crypto.objects.create(
            name="Raw Amount Trunc",
            symbol="RAT",
            coingecko_id="raw-amount-trunc",
            decimals=6,
        )
        chain = Chain.objects.create(
            code=ChainCode.Polygon,
            rpc="",
            active=True,
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
            type=TransferType.Withdrawal,
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


class ChainPoaRetryTests(TestCase):
    def setUp(self):
        self.chain = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="",
            active=True,
        )
        Chain.objects.filter(pk=self.chain.pk).update(rpc="http://bsc.local")
        self.chain.rpc = "http://bsc.local"

    @patch("chains.models.Web3")
    def test_get_block_with_poa_retry_marks_chain_and_rebuilds_cached_w3(
        self, web3_mock
    ):
        failing_w3 = Mock()
        failing_w3.eth.get_block.side_effect = ExtraDataLengthError(
            "poa extraData too long"
        )
        retry_w3 = Mock()
        retry_w3.eth.get_block.return_value = {"timestamp": 1_776_734_136}
        web3_mock.return_value = retry_w3
        self.chain.__dict__["w3"] = failing_w3

        block = self.chain.get_block_with_poa_retry(93_739_122)

        self.assertEqual(block["timestamp"], 1_776_734_136)
        self.chain.refresh_from_db()
        self.assertTrue(self.chain.is_poa)
        self.assertIs(self.chain.__dict__["w3"], retry_w3)
        retry_w3.middleware_onion.inject.assert_called_once()


class TxTaskValidationTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address="0x0000000000000000000000000000000000000001",
        )

    def test_failed_result_must_be_finalized(self):
        # 失败是终局结果，必须落在已结束阶段。
        task = TxTask(
            chain=self.chain,
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "1" * 64,
            stage=TxTaskStage.PENDING_CHAIN,
            success=False,
        )

        with self.assertRaises(ValidationError):
            task.full_clean()


class WalletBip44AccountMapTests(TestCase):
    def test_vault_maps_to_bip44_account_0(self):
        self.assertEqual(Wallet.get_bip44_account(AddressUsage.HotWallet), 0)

    def test_unknown_usage_raises_value_error(self):
        with self.assertRaises(ValueError):
            Wallet.get_bip44_account("nonexistent")


class TxHashModelTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
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
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "a1" * 32,
            stage=TxTaskStage.QUEUED,
            success=None,
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
        other_chain = Chain.objects.create(
            code=ChainCode.BSC,
            rpc="",
            active=True,
        )

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
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
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
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "e1" * 32,
            stage=TxTaskStage.QUEUED,
            success=None,
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

    @patch("chains.signer.get_signer_backend")
    def test_get_address_rejects_corrupted_existing_identity(
        self, get_signer_backend_mock
    ):
        # 历史脏数据若把同一 HD 身份写成错误地址，运行时必须立即报错而不是继续使用。
        signer_backend = Mock(spec=RemoteSignerBackend)
        signer_backend.derive_address.return_value = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000aa"
        )
        get_signer_backend_mock.return_value = signer_backend
        wallet = Wallet.objects.create()
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

    @patch("chains.signer.get_signer_backend")
    def test_get_address_preserves_non_identity_integrity_error(
        self, get_signer_backend_mock
    ):
        # 若冲突的是别的唯一约束（如地址被其他地址记录占用），不能误判成 tuple 并发创建成功。
        expected_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000ab"
        )
        signer_backend = Mock(spec=RemoteSignerBackend)
        signer_backend.derive_address.return_value = expected_address
        get_signer_backend_mock.return_value = signer_backend
        wallet = Wallet.objects.create()
        occupied_wallet = Wallet.objects.create()
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

    @patch("chains.signer.get_signer_backend")
    def test_get_address_falls_back_to_locked_read_after_integrity_error(
        self, get_signer_backend_mock
    ):
        # 模拟并发事务已落库但 get_or_create 撞 unique 约束失败的场景：
        # IntegrityError 后必须用 select_for_update 加锁回查，等对方事务提交后命中记录。
        expected_address = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000cc"
        )
        signer_backend = Mock(spec=RemoteSignerBackend)
        signer_backend.derive_address.return_value = expected_address
        get_signer_backend_mock.return_value = signer_backend
        wallet = Wallet.objects.create()
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

    @patch("chains.signer.get_signer_backend")
    def test_get_address_reraises_integrity_error_when_fallback_misses(
        self, get_signer_backend_mock
    ):
        # 回查 DoesNotExist 时必须把原 IntegrityError 抛出，避免吞掉真错误。
        signer_backend = Mock(spec=RemoteSignerBackend)
        signer_backend.derive_address.return_value = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000dd"
        )
        get_signer_backend_mock.return_value = signer_backend
        wallet = Wallet.objects.create()

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
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
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


class TransferConfirmDispatchTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.crypto = Crypto.objects.create(
            name="Ethereum Confirm Dispatch",
            symbol="ETHCD",
            coingecko_id="ethereum-confirm-dispatch",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
            latest_block_number=100,
        )
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000c1"
            ),
        )

    def _create_withdrawal_transfer_fixture(self, *, tx_hash: str):
        from projects.models import Project
        from withdrawals.models import Withdrawal
        from withdrawals.models import WithdrawalStatus

        project = Project.objects.create(
            name=f"project-{tx_hash[-6:]}",
            wallet=self.wallet,
            webhook="https://example.com/webhook",
        )
        tx_task = TxTask.objects.create(
            chain=self.chain,
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CONFIRM,
            success=None,
        )
        withdrawal = Withdrawal.objects.create(
            project=project,
            chain=self.chain,
            crypto=self.crypto,
            amount="1",
            worth="1",
            out_no=f"out-{tx_hash[-6:]}",
            to=Web3.to_checksum_address("0x00000000000000000000000000000000000000c3"),
            tx_task=tx_task,
            status=WithdrawalStatus.CONFIRMING,
        )
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "aa" * 32,
            hash=tx_hash,
            crypto=self.crypto,
            from_address=self.addr.address,
            to_address=withdrawal.to,
            value="1",
            amount="1",
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
            type=TransferType.Withdrawal,
        )
        withdrawal.transfer = transfer
        withdrawal.save(update_fields=["transfer", "updated_at"])
        return transfer, withdrawal, tx_task

    @patch("chains.tasks.confirm_transfer.delay")
    def test_block_number_updated_dispatches_quick_transfer_without_waiting_depth(
        self,
        confirm_transfer_delay_mock,
    ):
        # QUICK 模式只要已进入 confirming 且完成业务归类，就应立即进入确认任务，不等区块深度。
        from chains.tasks import block_number_updated

        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "aa" * 32,
            hash="0x" + "7" * 64,
            crypto=self.crypto,
            from_address=self.addr.address,
            to_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000c2"
            ),
            value="1",
            amount="1",
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
            confirm_mode=ConfirmMode.QUICK,
            type=TransferType.Withdrawal,
            processed_at=timezone.now(),
        )

        block_number_updated.run(self.chain.pk)

        confirm_transfer_delay_mock.assert_called_once_with(transfer.pk)

    @patch("chains.tasks.confirm_transfer.delay")
    def test_reorg_observed_transfer_replaces_old_transfer_before_full_confirm_dispatch(
        self,
        confirm_transfer_delay_mock,
    ):
        # reorg 后同一 tx_hash 可能被重新打包到新块；旧 Transfer 必须被删除，
        # 当前观测重新落库，避免确认调度沿用旧 block 提前放行。
        Chain.objects.filter(pk=self.chain.pk).update(
            latest_block_number=105,
        )
        self.chain.refresh_from_db()
        tx_hash = "0x" + "8" * 64
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=90,
            block_hash="0x" + "aa" * 32,
            hash=tx_hash,
            crypto=self.crypto,
            from_address=self.addr.address,
            to_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000c4"
            ),
            value=Decimal("1"),
            amount=Decimal("1"),
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMING,
            confirm_mode=ConfirmMode.FULL,
            processed_at=timezone.now(),
        )
        Transfer.objects.filter(pk=transfer.pk).update(
            created_at=timezone.now() - timedelta(seconds=20)
        )
        observed_at = transfer.datetime + timedelta(seconds=15)

        result = TransferService.create_observed_transfer(
            observed=ObservedTransferPayload(
                chain=self.chain,
                block=100,
                block_hash="0x" + "aa" * 32,
                tx_hash=tx_hash,
                from_address=transfer.from_address,
                to_address=transfer.to_address,
                crypto=self.crypto,
                value=Decimal("1"),
                amount=Decimal("1"),
                timestamp=2,
                occurred_at=observed_at,
                source="test-reorg",
            )
        )

        self.assertTrue(result.created)
        self.assertFalse(result.conflict)
        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())
        self.assertEqual(result.transfer.block, 100)
        self.assertEqual(result.transfer.timestamp, 2)
        self.assertEqual(result.transfer.datetime, observed_at)

        block_number_updated.run(self.chain.pk)

        confirm_transfer_delay_mock.assert_not_called()

    @patch("chains.tasks.confirm_transfer.delay")
    def test_reorg_observed_transfer_replaces_old_transfer_even_when_block_is_lower(
        self,
        confirm_transfer_delay_mock,
    ):
        # create_observed_transfer 只负责把同 tx_hash 的当前观测作为事实重建；
        # 业务确认安全由 confirm receipt 路径兜底。
        Chain.objects.filter(pk=self.chain.pk).update(
            latest_block_number=105,
        )
        self.chain.refresh_from_db()
        tx_hash = "0x" + "9" * 64
        observed_at = timezone.now()
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            block_hash="0x" + "aa" * 32,
            hash=tx_hash,
            crypto=self.crypto,
            from_address=self.addr.address,
            to_address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000c5"
            ),
            value=Decimal("1"),
            amount=Decimal("1"),
            timestamp=2,
            datetime=observed_at,
            status=TransferStatus.CONFIRMING,
            confirm_mode=ConfirmMode.FULL,
            processed_at=timezone.now(),
        )
        Transfer.objects.filter(pk=transfer.pk).update(
            created_at=timezone.now() - timedelta(seconds=20)
        )

        result = TransferService.create_observed_transfer(
            observed=ObservedTransferPayload(
                chain=self.chain,
                block=90,
                block_hash="0x" + "aa" * 32,
                tx_hash=tx_hash,
                from_address=transfer.from_address,
                to_address=transfer.to_address,
                crypto=self.crypto,
                value=Decimal("1"),
                amount=Decimal("1"),
                timestamp=1,
                occurred_at=observed_at - timedelta(seconds=15),
                source="test-reorg-stale",
            )
        )

        self.assertTrue(result.created)
        self.assertFalse(result.conflict)
        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())
        self.assertEqual(result.transfer.block, 90)
        self.assertEqual(result.transfer.timestamp, 1)
        self.assertEqual(result.transfer.datetime, observed_at - timedelta(seconds=15))

        block_number_updated.run(self.chain.pk)

        confirm_transfer_delay_mock.assert_not_called()

    @patch("common.decorators.cache.delete", return_value=True)
    @patch("common.decorators.cache.add", return_value=True)
    @patch("withdrawals.service.WithdrawalService.notify_status_changed")
    @patch("chains.tasks.AdapterFactory.get_adapter")
    def test_confirm_transfer_raises_when_failed_result_appears_on_existing_transfer(
        self,
        get_adapter_mock,
        _notify_mock,
        _cache_add_mock,
        _cache_delete_mock,
    ):
        from chains.adapters import TxCheckStatus
        from chains.tasks import confirm_transfer
        from withdrawals.models import WithdrawalStatus

        transfer, withdrawal, tx_task = self._create_withdrawal_transfer_fixture(
            tx_hash="0x" + "f" * 64
        )
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckStatus.FAILED
        get_adapter_mock.return_value = adapter

        with self.assertRaisesMessage(
            RuntimeError, "失败交易不应存在 Transfer 记录"
        ):
            confirm_transfer.run(transfer.pk)

        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.CONFIRMING)
        self.assertEqual(withdrawal.transfer_id, transfer.pk)
        tx_task.refresh_from_db()
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CONFIRM)
        self.assertIsNone(tx_task.success)

    @patch("common.decorators.cache.delete", return_value=True)
    @patch("common.decorators.cache.add", return_value=True)
    @patch("chains.tasks.AdapterFactory.get_adapter")
    def test_confirm_transfer_handles_dropped_result_by_reverting_pending_chain(
        self, get_adapter_mock, _cache_add_mock, _cache_delete_mock
    ):
        from chains.adapters import TxCheckStatus
        from chains.tasks import confirm_transfer
        from withdrawals.models import WithdrawalStatus

        transfer, withdrawal, tx_task = self._create_withdrawal_transfer_fixture(
            tx_hash="0x" + "e" * 64
        )
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckStatus.DROPPED
        get_adapter_mock.return_value = adapter

        confirm_transfer.run(transfer.pk)

        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.PENDING)
        self.assertIsNone(withdrawal.transfer)
        tx_task.refresh_from_db()
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(tx_task.success)

    @patch("common.decorators.cache.delete", return_value=True)
    @patch("common.decorators.cache.add", return_value=True)
    @patch("chains.tasks.AdapterFactory.get_adapter")
    def test_confirm_transfer_refreshes_block_when_receipt_moves_higher(
        self, get_adapter_mock, _cache_add_mock, _cache_delete_mock
    ):
        from chains.adapters import TxCheckResult
        from chains.adapters import TxCheckStatus
        from chains.tasks import confirm_transfer
        from withdrawals.models import WithdrawalStatus

        transfer, withdrawal, tx_task = self._create_withdrawal_transfer_fixture(
            tx_hash="0x" + "b" * 64
        )
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.CONFIRMED,
            block_number=120,
            block_hash="0x" + "12" * 32,
        )
        get_adapter_mock.return_value = adapter

        confirm_transfer.run(transfer.pk)

        transfer.refresh_from_db()
        withdrawal.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(transfer.block, 120)
        self.assertEqual(transfer.block_hash, "0x" + "12" * 32)
        self.assertEqual(transfer.status, TransferStatus.CONFIRMING)
        self.assertEqual(withdrawal.status, WithdrawalStatus.CONFIRMING)
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CONFIRM)

    @patch("common.decorators.cache.delete", return_value=True)
    @patch("common.decorators.cache.add", return_value=True)
    @patch("chains.tasks.AdapterFactory.get_adapter")
    def test_confirm_transfer_refreshes_block_hash_when_receipt_hash_changes(
        self, get_adapter_mock, _cache_add_mock, _cache_delete_mock
    ):
        from chains.adapters import TxCheckResult
        from chains.adapters import TxCheckStatus
        from chains.tasks import confirm_transfer
        from withdrawals.models import WithdrawalStatus

        transfer, withdrawal, tx_task = self._create_withdrawal_transfer_fixture(
            tx_hash="0x" + "c" * 64
        )
        Transfer.objects.filter(pk=transfer.pk).update(
            block_hash="0x" + "11" * 32
        )
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckResult(
            status=TxCheckStatus.CONFIRMED,
            block_number=transfer.block,
            block_hash="0x" + "22" * 32,
        )
        get_adapter_mock.return_value = adapter

        confirm_transfer.run(transfer.pk)

        transfer.refresh_from_db()
        withdrawal.refresh_from_db()
        tx_task.refresh_from_db()
        self.assertEqual(transfer.block, 100)
        self.assertEqual(transfer.block_hash, "0x" + "22" * 32)
        self.assertEqual(transfer.status, TransferStatus.CONFIRMING)
        self.assertEqual(withdrawal.status, WithdrawalStatus.CONFIRMING)
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CONFIRM)

    @patch("common.decorators.cache.delete", return_value=True)
    @patch("common.decorators.cache.add", return_value=True)
    @patch("chains.tasks.AdapterFactory.get_adapter")
    def test_confirm_transfer_retries_confirming_result_before_drop(
        self, get_adapter_mock, _cache_add_mock, _cache_delete_mock
    ):
        from chains.adapters import TxCheckStatus
        from chains.tasks import confirm_transfer
        from withdrawals.models import WithdrawalStatus

        transfer, withdrawal, tx_task = self._create_withdrawal_transfer_fixture(
            tx_hash="0x" + "d" * 64
        )
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckStatus.CONFIRMING
        get_adapter_mock.return_value = adapter

        with patch.object(
            confirm_transfer,
            "retry",
            side_effect=RuntimeError("retry scheduled"),
        ) as retry_mock, self.assertRaisesMessage(RuntimeError, "retry scheduled"):
            confirm_transfer.run(transfer.pk)

        retry_mock.assert_called_once()
        self.assertTrue(Transfer.objects.filter(pk=transfer.pk).exists())
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.CONFIRMING)
        self.assertEqual(withdrawal.transfer_id, transfer.pk)
        tx_task.refresh_from_db()
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CONFIRM)

    @patch("common.decorators.cache.delete", return_value=True)
    @patch("common.decorators.cache.add", return_value=True)
    @patch("chains.tasks.AdapterFactory.get_adapter")
    def test_confirm_transfer_drops_confirming_result_after_retry_limit(
        self, get_adapter_mock, _cache_add_mock, _cache_delete_mock
    ):
        from chains.adapters import TxCheckStatus
        from chains.tasks import confirm_transfer
        from withdrawals.models import WithdrawalStatus

        transfer, withdrawal, tx_task = self._create_withdrawal_transfer_fixture(
            tx_hash="0x" + "c" * 64
        )
        adapter = Mock()
        adapter.tx_result.return_value = TxCheckStatus.CONFIRMING
        get_adapter_mock.return_value = adapter

        old_retries = confirm_transfer.request.retries
        confirm_transfer.request.retries = confirm_transfer.max_retries
        try:
            confirm_transfer.run(transfer.pk)
        finally:
            confirm_transfer.request.retries = old_retries

        self.assertFalse(Transfer.objects.filter(pk=transfer.pk).exists())
        withdrawal.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.PENDING)
        self.assertIsNone(withdrawal.transfer)
        tx_task.refresh_from_db()
        self.assertEqual(tx_task.stage, TxTaskStage.PENDING_CHAIN)


class SignerBackendTests(TestCase):
    @override_settings(
        SIGNER_BACKEND="remote",
        SIGNER_BASE_URL="http://signer.internal",
        SIGNER_TIMEOUT=3.5,
        SIGNER_SHARED_SECRET="secret",
    )
    @patch("chains.signer.httpx.post")
    def test_remote_signer_backend_posts_wallet_chain_and_bip44_params(
        self, httpx_post_mock
    ):
        # 远端 signer 接受 wallet_id / chain_type / bip44_account / address_index。
        addr = Mock(
            wallet_id=12, chain_type=ChainType.EVM, bip44_account=1, address_index=0
        )
        chain = Mock()
        response = Mock()
        response.json.return_value = {
            "tx_hash": "0x" + "11" * 32,
            "raw_transaction": "0xdeadbeef",
        }
        httpx_post_mock.return_value = response

        payload = get_signer_backend().sign_evm_transaction(
            address=addr,
            chain=chain,
            tx_dict={"nonce": 7, "data": "0x"},
        )

        self.assertEqual(payload.tx_hash, "0x" + "11" * 32)
        self.assertEqual(payload.raw_transaction, "0xdeadbeef")
        _, kwargs = httpx_post_mock.call_args
        body = kwargs["content"].decode("utf-8")
        self.assertIn('"wallet_id":12', body)
        self.assertIn(f'"chain_type":"{ChainType.EVM}"', body)
        self.assertIn('"bip44_account":1', body)
        self.assertIn('"address_index":0', body)

    @override_settings(
        SIGNER_BACKEND="remote",
        SIGNER_BASE_URL="http://signer.internal",
        SIGNER_TIMEOUT=3.5,
        SIGNER_SHARED_SECRET="secret",
    )
    @patch("chains.signer.httpx.post")
    def test_remote_signer_backend_create_wallet_returns_wallet_id(
        self, httpx_post_mock
    ):
        response = Mock()
        response.json.return_value = {
            "wallet_id": 99,
            "created": True,
        }
        httpx_post_mock.return_value = response

        wallet_id = get_signer_backend().create_wallet(wallet_id=99)

        self.assertEqual(wallet_id, 99)
        _, kwargs = httpx_post_mock.call_args
        self.assertEqual(
            json.loads(kwargs["content"].decode("utf-8")),
            {
                "wallet_id": 99,
                "request_id": json.loads(kwargs["content"].decode("utf-8"))[
                    "request_id"
                ],
            },
        )
        request_payload = json.loads(kwargs["content"].decode("utf-8"))
        self.assertEqual(
            kwargs["headers"]["X-Signer-Signature"],
            hmac.new(
                b"secret",
                build_signer_signature_payload(
                    method="POST",
                    path="/v1/wallets/create",
                    request_id=request_payload["request_id"],
                    request_body=kwargs["content"],
                ),
                hashlib.sha256,
            ).hexdigest(),
        )

    @override_settings(
        SIGNER_BACKEND="remote",
        SIGNER_BASE_URL="http://signer.internal",
        SIGNER_TIMEOUT=3.5,
        SIGNER_SHARED_SECRET="secret",
    )
    @patch("chains.signer.httpx.post")
    def test_remote_signer_backend_create_wallet_rejects_mismatched_wallet_id(
        self, httpx_post_mock
    ):
        response = Mock()
        response.json.return_value = {
            "wallet_id": 100,
            "created": True,
        }
        httpx_post_mock.return_value = response

        with self.assertRaisesMessage(SignerServiceError, "wallet_id 不匹配"):
            get_signer_backend().create_wallet(wallet_id=99)

    @override_settings(
        SIGNER_BACKEND="remote",
        SIGNER_BASE_URL="http://signer.internal",
        SIGNER_TIMEOUT=3.5,
        SIGNER_SHARED_SECRET="secret",
    )
    @patch("chains.signer.httpx.post")
    def test_remote_signer_backend_includes_signer_error_detail(
        self, httpx_post_mock
    ):
        response = Mock()
        response.status_code = 400
        response.json.return_value = {
            "code": "1000",
            "message": "参数错误",
            "detail": "wallet_id 无效",
        }
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=httpx.Request("POST", "http://signer.internal/v1/sign/evm"),
            response=response,
        )
        httpx_post_mock.return_value = response
        addr = Mock(
            wallet_id=95, chain_type=ChainType.EVM, bip44_account=1, address_index=0
        )

        with self.assertRaisesMessage(
            SignerServiceError,
            "远端 signer 请求失败: /v1/sign/evm (HTTP 400, code=1000, detail=wallet_id 无效)",
        ):
            get_signer_backend().sign_evm_transaction(
                address=addr,
                chain=Mock(),
                tx_dict={"nonce": 0, "data": "0x"},
            )

    @override_settings(
        SIGNER_BACKEND="remote",
        SIGNER_BASE_URL="http://signer.internal",
        SIGNER_TIMEOUT=3.5,
        SIGNER_SHARED_SECRET="secret",
    )
    @patch("chains.signer.httpx.post")
    def test_remote_signer_backend_derive_address_posts_bip44_params(
        self, httpx_post_mock
    ):
        wallet = Mock(pk=12)
        response = Mock()
        response.json.return_value = {
            "address": "0x00000000000000000000000000000000000000f1",
        }
        httpx_post_mock.return_value = response

        address = get_signer_backend().derive_address(
            wallet=wallet,
            chain_type=ChainType.EVM,
            bip44_account=1,
            address_index=0,
        )

        self.assertEqual(address, "0x00000000000000000000000000000000000000f1")
        _, kwargs = httpx_post_mock.call_args
        body = kwargs["content"].decode("utf-8")
        self.assertIn('"wallet_id":12', body)
        self.assertIn(f'"chain_type":"{ChainType.EVM}"', body)
        self.assertIn('"bip44_account":1', body)
        self.assertIn('"address_index":0', body)

    @override_settings(
        SIGNER_BACKEND="remote",
        SIGNER_BASE_URL="http://signer.internal",
        SIGNER_TIMEOUT=3.5,
        SIGNER_SHARED_SECRET="secret",
    )
    @patch("chains.signer.httpx.get")
    def test_remote_signer_backend_fetches_admin_summary(self, httpx_get_mock):
        # 主应用后台只通过内部只读 API 拉取 signer 摘要，不直接读取 signer 数据库。
        response = Mock()
        response.json.return_value = {
            "health": {
                "database": True,
                "cache": True,
                "auth_configured": True,
                "healthy": True,
            },
            "wallets": {"total": 3, "active": 2, "frozen": 1},
            "requests_last_hour": {
                "total": 10,
                "succeeded": 8,
                "failed": 1,
                "rate_limited": 1,
            },
            "recent_anomalies": [
                {
                    "request_id": "req-1",
                    "endpoint": "/v1/sign/evm",
                    "wallet_id": 12,
                    "chain_type": ChainType.EVM,
                    "bip44_account": 0,
                    "address_index": 0,
                    "status": "failed",
                    "error_code": "1005",
                    "detail": "wallet 已冻结",
                    "created_at": "2026-03-14T10:00:00+00:00",
                }
            ],
        }
        httpx_get_mock.return_value = response

        summary = get_signer_backend().fetch_admin_summary()

        self.assertIsInstance(summary, SignerAdminSummary)
        self.assertEqual(summary.wallets["frozen"], 1)
        self.assertEqual(summary.requests_last_hour["failed"], 1)
        self.assertEqual(summary.recent_anomalies[0]["wallet_id"], 12)
        _, kwargs = httpx_get_mock.call_args
        self.assertEqual(
            kwargs["headers"]["X-Signer-Signature"],
            hmac.new(
                b"secret",
                build_signer_signature_payload(
                    method="GET",
                    path="/internal/admin-summary",
                    request_id=kwargs["headers"]["X-Signer-Request-Id"],
                    request_body=b"",
                ),
                hashlib.sha256,
            ).hexdigest(),
        )


@override_settings(
    SIGNER_BACKEND="remote",
    SIGNER_BASE_URL="http://signer.internal",
    SIGNER_TIMEOUT=3.5,
    SIGNER_SHARED_SECRET="secret",
)
class WalletRemoteGenerationTests(TestCase):
    @patch("chains.signer.httpx.post")
    def test_generate_remote_wallet_uses_signer_create_and_derive_address(
        self, httpx_post_mock
    ):
        # remote 模式下新钱包不再本地生成助记词，但后续 get_address 仍应能通过 signer 派生地址。
        def side_effect(url, **kwargs):
            response = Mock()
            body = json.loads(kwargs["content"].decode("utf-8"))
            if url.endswith("/v1/wallets/create"):
                self.assertIsInstance(body["wallet_id"], int)
                response.json.return_value = {
                    "wallet_id": body["wallet_id"],
                    "created": True,
                }
                return response
            if url.endswith("/v1/wallets/derive-address"):
                self.assertEqual(body["wallet_id"], created_wallet_id)
                response.json.return_value = {
                    "address": Web3.to_checksum_address(
                        "0x000000000000000000000000000000000000abcd"
                    ),
                }
                return response
            raise AssertionError(f"unexpected url: {url}")

        created_wallet_id = None

        def capturing_side_effect(url, **kwargs):
            nonlocal created_wallet_id
            response = side_effect(url, **kwargs)
            body = json.loads(kwargs["content"].decode("utf-8"))
            if url.endswith("/v1/wallets/create"):
                created_wallet_id = body["wallet_id"]
            return response

        httpx_post_mock.side_effect = capturing_side_effect

        wallet = Wallet.generate()
        addr = wallet.get_address(
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
        )

        self.assertEqual(wallet.pk, created_wallet_id)
        self.assertEqual(
            addr.address,
            Web3.to_checksum_address("0x000000000000000000000000000000000000abcd"),
        )

    @patch("chains.signer.get_signer_backend")
    def test_generate_remote_wallet_raises_readable_error_when_signer_unavailable(
        self,
        get_signer_backend_mock,
    ):
        signer_backend = Mock(spec=RemoteSignerBackend)
        signer_backend.create_wallet.side_effect = SignerServiceError("signer down")
        get_signer_backend_mock.return_value = signer_backend

        with self.assertRaisesMessage(
            RuntimeError, "signer 服务不可用，无法创建新钱包"
        ):
            Wallet.generate()

    @patch("chains.signer.get_signer_backend")
    def test_get_address_raises_readable_error_when_signer_unavailable(
        self,
        get_signer_backend_mock,
    ):
        signer_backend = Mock(spec=RemoteSignerBackend)
        signer_backend.derive_address.side_effect = SignerServiceError("signer down")
        get_signer_backend_mock.return_value = signer_backend
        wallet = Wallet.objects.create()

        with self.assertRaisesMessage(RuntimeError, "signer 服务不可用，无法为钱包"):
            wallet.get_address(
                chain_type=ChainType.EVM,
                usage=AddressUsage.HotWallet,
            )


@override_settings(
    SIGNER_BACKEND="remote",
    SIGNER_BASE_URL="",
    SIGNER_SHARED_SECRET="",
)
class SignerSystemCheckTests(TestCase):
    def test_remote_signer_requires_base_url_and_shared_secret(self):
        errors = checks.run_checks()
        error_ids = {error.id for error in errors}

        self.assertIn("chains.E002", error_ids)
        self.assertIn("chains.E003", error_ids)


class UpdateLatestBlockTaskConfigTests(TestCase):
    @patch("chains.tasks.block_number_updated.delay")
    def test_update_the_latest_block_keeps_tron_height_without_rpc_polling(
        self,
        block_number_updated_delay_mock,
    ):
        from chains.tasks import update_the_latest_block

        chain = Chain.objects.create(
            code=ChainCode.Tron,
            rpc="http://tron.invalid",
            active=True,
            latest_block_number=456,
        )

        update_the_latest_block.run(chain.pk)

        chain.refresh_from_db()
        self.assertEqual(chain.latest_block_number, 456)
        block_number_updated_delay_mock.assert_not_called()


class TransferServiceCreateObservedTests(TestCase):
    """覆盖 TransferService.create_observed_transfer 的幂等与冲突场景。"""

    def setUp(self):
        from chains.service import ObservedTransferPayload

        self.crypto = Crypto.objects.create(
            name="Ether OT",
            symbol="ETH-OT",
            coingecko_id="ether-ot",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
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
            occurred_at=timezone.now(),
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
            datetime=self.payload.occurred_at,
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
            occurred_at=self.payload.occurred_at + timedelta(seconds=1),
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
            datetime=self.payload.occurred_at,
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
            occurred_at=self.payload.occurred_at + timedelta(seconds=1),
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
            self.payload.occurred_at + timedelta(seconds=1),
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
            datetime=self.payload.occurred_at,
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
            occurred_at=self.payload.occurred_at,
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
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
        )
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
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash="0x" + "dd" * 32,
            stage=TxTaskStage.PENDING_CONFIRM,
            success=None,
        )

    def test_mark_finalized_success_transitions_correctly(self):
        updated = TxTask.mark_finalized_success(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertEqual(updated, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.FINALIZED)
        self.assertIs(self.task.success, True)

    def test_reset_to_pending_chain_transitions_correctly(self):
        updated = TxTask.reset_to_pending_chain(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertEqual(updated, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(self.task.success)

    def test_mark_finalized_success_can_resolve_old_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "ee" * 32)

        updated = TxTask.mark_finalized_success(
            chain=self.chain,
            tx_hash=old_hash,
        )

        self.assertEqual(updated, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.FINALIZED)
        self.assertIs(self.task.success, True)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_reset_to_pending_chain_can_resolve_old_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "ef" * 32)

        updated = TxTask.reset_to_pending_chain(
            chain=self.chain,
            tx_hash=old_hash,
        )

        self.assertEqual(updated, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(self.task.success)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_mark_finalized_failed_transitions_correctly(self):
        updated = TxTask.mark_finalized_failed(
            task_id=self.task.pk,
        )
        self.assertEqual(updated, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.FINALIZED)
        self.assertIs(self.task.success, False)

    def test_mark_finalized_failed_honors_expected_stage(self):
        updated = TxTask.mark_finalized_failed(
            task_id=self.task.pk,
            expected_stage=TxTaskStage.PENDING_CHAIN,
        )

        self.assertEqual(updated, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.PENDING_CONFIRM)
        self.assertIsNone(self.task.success)

    def test_mark_finalized_success_does_not_override_failed_final_state(self):
        TxTask.mark_finalized_failed(
            task_id=self.task.pk,
        )

        updated = TxTask.mark_finalized_success(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        self.assertEqual(updated, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.FINALIZED)
        self.assertIs(self.task.success, False)

    def test_mark_finalized_failed_does_not_override_success_final_state(self):
        TxTask.mark_finalized_success(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        updated = TxTask.mark_finalized_failed(
            task_id=self.task.pk,
        )

        self.assertEqual(updated, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.FINALIZED)
        self.assertIs(self.task.success, True)

    def test_mark_pending_confirm_skips_finalized_tasks(self):
        # 先将任务标记为已完结
        TxTask.mark_finalized_success(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        # mark_pending_confirm 不应回退已完结的任务
        updated = TxTask.mark_pending_confirm(
            chain=self.chain, tx_hash=self.task.tx_hash
        )
        self.assertEqual(updated, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.FINALIZED)

    def test_mark_pending_confirm_with_empty_hash_is_noop(self):
        updated = TxTask.mark_pending_confirm(chain=self.chain, tx_hash="")
        self.assertEqual(updated, 0)

    def test_mark_pending_confirm_can_resolve_old_hash(self):
        old_hash = self.task.tx_hash
        self.task.append_tx_hash(old_hash)
        self.task.append_tx_hash("0x" + "f0" * 32)

        updated = TxTask.mark_pending_confirm(
            chain=self.chain,
            tx_hash=old_hash,
        )

        self.assertEqual(updated, 1)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.PENDING_CONFIRM)
        self.assertIsNone(self.task.success)
        self.assertEqual(self.task.tx_hash, old_hash)

    def test_reset_to_pending_chain_skips_non_pending_confirm_tasks(self):
        TxTask.objects.filter(pk=self.task.pk).update(
            stage=TxTaskStage.QUEUED,
            success=None,
        )
        updated = TxTask.reset_to_pending_chain(
            chain=self.chain,
            tx_hash=self.task.tx_hash,
        )

        self.assertEqual(updated, 0)
        self.task.refresh_from_db()
        self.assertEqual(self.task.stage, TxTaskStage.QUEUED)
        self.assertIsNone(self.task.success)


class BlockNumberUpdatedCompensationTests(TestCase):
    """验证 block_number_updated 在满批时自调度补偿。"""

    def setUp(self):
        self.crypto = Crypto.objects.create(
            name="Ether BN",
            symbol="ETH-BN",
            coingecko_id="ether-bn",
        )
        self.chain = Chain.objects.create(
            code=ChainCode.Ethereum,
            rpc="",
            active=True,
            latest_block_number=200,
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


@pytest.mark.django_db
def test_address_send_crypto_schedules_native_transfer_intent():
    chain = Chain.objects.create(
        code=ChainCode.Ethereum,
    )
    native = chain.native_coin
    address = Address.objects.create(
        wallet=Wallet.objects.create(),
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
        address_index=0,
        address=Web3.to_checksum_address(
            "0x0000000000000000000000000000000000121201"
        ),
    )
    tx_hash = "0x" + "12" * 32

    with patch("evm.models.EvmTxTask.schedule") as schedule_mock:
        schedule_mock.return_value = Mock(base_task=Mock(tx_hash=tx_hash))

        result = address.send_crypto(
            crypto=native,
            chain=chain,
            to="0x0000000000000000000000000000000000121202",
            amount=Decimal("1.5"),
            tx_type=TxTaskType.Withdrawal,
        )

    assert result == tx_hash
    schedule_mock.assert_called_once()
    (intent,) = schedule_mock.call_args.args
    assert intent.address == address
    assert intent.chain == chain
    assert intent.tx_kind == TxKind.NATIVE_TRANSFER
    assert intent.tx_type == TxTaskType.Withdrawal
    assert intent.value == 1_500_000_000_000_000_000
    assert intent.gas == DEFAULT_BASE_TRANSFER_GAS


@pytest.mark.django_db
def test_address_send_crypto_schedules_erc20_transfer_intent():
    token = Crypto.objects.create(
        name="Task12 BSC Wrapped",
        symbol="BSC",
        coingecko_id="task12-bsc-wrapped",
        decimals=6,
    )
    chain = Chain.objects.create(
        code=ChainCode.Ethereum,
    )
    ChainToken.objects.create(
        chain=chain,
        crypto=token,
        address=Web3.to_checksum_address(
            "0x00000000000000000000000000000000001212c0"
        ),
        decimals=6,
    )
    assert token.is_native
    assert token != chain.native_coin
    address = Address.objects.create(
        wallet=Wallet.objects.create(),
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
        address_index=0,
        address=Web3.to_checksum_address(
            "0x0000000000000000000000000000000000121203"
        ),
    )
    recipient = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000121204"
    )
    tx_hash = "0x" + "13" * 32

    with patch("evm.models.EvmTxTask.schedule") as schedule_mock:
        schedule_mock.return_value = Mock(base_task=Mock(tx_hash=tx_hash))

        result = address.send_crypto(
            crypto=token,
            chain=chain,
            to=recipient,
            amount=Decimal("2.25"),
            tx_type=TxTaskType.Withdrawal,
        )

    assert result == tx_hash
    schedule_mock.assert_called_once()
    (intent,) = schedule_mock.call_args.args
    assert intent.address == address
    assert intent.chain == chain
    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(
        "0x00000000000000000000000000000000001212c0"
    )
    assert intent.tx_type == TxTaskType.Withdrawal
    assert intent.gas == DEFAULT_ERC20_TRANSFER_GAS


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
