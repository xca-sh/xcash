from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from web3 import Web3

from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from chains.models import Transfer
from chains.models import TransferType
from evm.choices import TxKind
from evm.internal_tx import routing
from evm.internal_tx.routing import INTERNAL_TX_HANDLERS
from evm.internal_tx.routing import INTERNAL_TX_MATCHERS
from evm.internal_tx.routing import MatchedTransferFact
from evm.intents import build_deposit_slot_collect_intent
from evm.models import DepositSlot
from evm.models import DepositSlotUsage
from evm.internal_tx.processor import process_internal_transaction
from evm.models import EvmTxTask
from evm.tests._fixtures import make_tx_task
from evm.tests._fixtures import make_erc20_token
from evm.tests._fixtures import make_evm_chain
from evm.tests._fixtures import make_evm_system_address
from evm.tests._fixtures import make_tx_hash
from projects.models import Project
from withdrawals.models import Withdrawal
from withdrawals.models import WithdrawalStatus


def _erc20_transfer_log(*, token, from_addr, to_addr, value_raw, log_index):
    return {
        "address": token,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + Web3.to_checksum_address(from_addr)[2:].lower().zfill(64),
            "0x" + Web3.to_checksum_address(to_addr)[2:].lower().zfill(64),
        ],
        "data": "0x" + hex(value_raw)[2:].zfill(64),
        "logIndex": log_index,
    }


def _xcash_collected_log(*, slot, token, value_raw, log_index):
    return {
        "address": slot,
        "topics": [
            Web3.keccak(text="XcashCollected(address,uint256)").hex(),
            "0x" + Web3.to_checksum_address(token)[2:].lower().zfill(64),
        ],
        "data": "0x" + hex(value_raw)[2:].zfill(64),
        "logIndex": log_index,
    }


def _base_task_without_asset_fields(*, chain, address, tx_type, tx_hash_suffix):
    return TxTask.objects.create(
        chain=chain,
        address=address,
        tx_type=tx_type,
        tx_hash=make_tx_hash(tx_hash_suffix),
        stage=TxTaskStage.PENDING_CHAIN,
        success=None,
    )


def _native_evm_task(*, base_task, address, chain, to, value_raw, nonce=0):
    return EvmTxTask.objects.create(
        base_task=base_task,
        address=address,
        chain=chain,
        nonce=nonce,
        to=Web3.to_checksum_address(to),
        value=value_raw,
        data="",
        gas=21_000,
        tx_kind=TxKind.NATIVE_TRANSFER,
    )


class InternalTxRegistryTests(TestCase):
    def test_internal_tx_registry_explicitly_declares_business_routes(self):
        self.assertIs(
            routing.INTERNAL_TX_HANDLERS,
            INTERNAL_TX_HANDLERS,
        )
        self.assertIs(
            routing.INTERNAL_TX_MATCHERS,
            INTERNAL_TX_MATCHERS,
        )
        self.assertEqual(
            set(INTERNAL_TX_HANDLERS),
            {TxTaskType.DepositSlotCollect, TxTaskType.Withdrawal},
        )
        self.assertEqual(
            set(INTERNAL_TX_MATCHERS),
            {TxTaskType.DepositSlotCollect, TxTaskType.Withdrawal},
        )


class DirectInternalLifecycleWithoutBroadcastAssetFieldsTests(TestCase):
    def test_deposit_slot_collect_erc20_success_creates_collect_transfer(self):
        chain = make_evm_chain(code="eth-slot-collect", chain_id=43016)
        address = make_evm_system_address(suffix="ad06", usage=AddressUsage.HotWallet)
        project = Project.objects.create(name="SlotCollectProject", wallet=address.wallet)
        slot_address = Web3.to_checksum_address("0x" + "88" * 20)
        vault_address = Web3.to_checksum_address("0x" + "99" * 20)
        DepositSlot.objects.create(
            project=project,
            chain=chain,
            usage=DepositSlotUsage.INVOICE,
            invoice_index=1,
            address=slot_address,
            vault_address=vault_address,
            salt=b"\x01" * 32,
        )
        token = make_erc20_token(chain=chain, address_suffix="c016", decimals=6)
        task = _base_task_without_asset_fields(
            chain=chain,
            address=address,
            tx_type=TxTaskType.DepositSlotCollect,
            tx_hash_suffix="7777",
        )
        intent = build_deposit_slot_collect_intent(
            address=address,
            chain=chain,
            deposit_slot_address=slot_address,
            token_address=token.address(chain),
        )
        EvmTxTask.objects.create(
            base_task=task,
            address=address,
            chain=chain,
            nonce=0,
            to=intent.to,
            value=intent.value,
            data=intent.data,
            gas=intent.gas,
            tx_kind=intent.tx_kind,
        )
        value_raw = 123_456_789

        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            occurred_at = timezone.now()
            ts.return_value = (1_700_000_001, occurred_at)
            result = process_internal_transaction(
                chain=chain,
                tx={
                    "hash": task.tx_hash,
                    "from": address.address,
                    "to": slot_address,
                    "value": 0,
                    "input": intent.data,
                },
                receipt={
                    "status": 1,
                    "logs": [
                        _xcash_collected_log(
                            slot=slot_address,
                            token=token.address(chain),
                            value_raw=value_raw,
                            log_index=2,
                        ),
                        _erc20_transfer_log(
                            token=token.address(chain),
                            from_addr=slot_address,
                            to_addr=vault_address,
                            value_raw=value_raw,
                            log_index=3,
                        ),
                    ],
                    "blockNumber": 10,
                    "blockHash": "0x" + "ab" * 32,
                },
            )

        task.refresh_from_db()
        self.assertIsNotNone(result)
        self.assertTrue(result.created)
        transfer = result.transfer
        self.assertIsNotNone(transfer)
        transfer.process()
        transfer.refresh_from_db()
        self.assertEqual(transfer.type, TransferType.Collect)
        self.assertEqual(transfer.from_address, slot_address)
        self.assertEqual(transfer.to_address, vault_address)
        self.assertEqual(transfer.crypto_id, token.pk)
        self.assertEqual(transfer.value, Decimal(value_raw))
        self.assertEqual(transfer.amount, Decimal("123.456789"))
        self.assertEqual(transfer.block_hash, "0x" + "ab" * 32)

        task.refresh_from_db()
        self.assertEqual(task.stage, TxTaskStage.PENDING_CONFIRM)
        self.assertIsNone(task.success)

        transfer.confirm()
        task.refresh_from_db()
        self.assertEqual(task.stage, TxTaskStage.FINALIZED)
        self.assertIs(task.success, True)

    def test_deposit_slot_deploy_success_finalizes_without_transfer(self):
        chain = make_evm_chain(code="eth-slot-deploy", chain_id=43017)
        address = make_evm_system_address(suffix="ad07", usage=AddressUsage.HotWallet)
        task = _base_task_without_asset_fields(
            chain=chain,
            address=address,
            tx_type=TxTaskType.DepositSlotDeploy,
            tx_hash_suffix="7878",
        )
        EvmTxTask.objects.create(
            base_task=task,
            address=address,
            chain=chain,
            nonce=0,
            to=Web3.to_checksum_address("0x" + "88" * 20),
            value=0,
            data="0x1234",
            gas=300_000,
            tx_kind=TxKind.CONTRACT_CALL,
        )

        result = process_internal_transaction(
            chain=chain,
            tx={
                "hash": task.tx_hash,
                "from": address.address,
                "to": Web3.to_checksum_address("0x" + "88" * 20),
                "input": "0x1234",
            },
            receipt={"status": 1, "logs": [], "blockNumber": 10},
        )

        task.refresh_from_db()
        self.assertIsNone(result)
        self.assertEqual(task.stage, TxTaskStage.FINALIZED)
        self.assertIs(task.success, True)
        self.assertFalse(Transfer.objects.filter(hash=task.tx_hash).exists())

    def test_native_withdrawal_matches_from_withdrawal_and_evm_task(self):
        chain = make_evm_chain(code="eth-noasset-wd", chain_id=43010)
        vault = make_evm_system_address(suffix="ad01", usage=AddressUsage.HotWallet)
        recipient = Web3.to_checksum_address("0x" + "91" * 20)
        value_raw = 1_250_000_000_000_000_000
        base_task = _base_task_without_asset_fields(
            chain=chain,
            address=vault,
            tx_type=TxTaskType.Withdrawal,
            tx_hash_suffix="0d01",
        )
        _native_evm_task(
            base_task=base_task,
            address=vault,
            chain=chain,
            to=recipient,
            value_raw=value_raw,
        )
        project = Project.objects.create(name="NoAssetWithdrawal", wallet=vault.wallet)
        Withdrawal.objects.create(
            project=project,
            crypto=chain.native_coin,
            amount=Decimal("1.25"),
            chain=chain,
            out_no="noasset-withdrawal",
            to=recipient,
            tx_task=base_task,
            status=WithdrawalStatus.PENDING,
        )

        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            ts.return_value = (1_700_000_000, timezone.now())
            process_internal_transaction(
                chain=chain,
                tx={
                    "hash": base_task.tx_hash,
                    "from": vault.address,
                    "to": recipient,
                    "value": value_raw,
                    "input": "0x",
                },
                receipt={"status": 1, "logs": [], "blockNumber": 10},
            )

        transfer = Transfer.objects.get(hash=base_task.tx_hash)
        transfer.process()
        withdrawal = Withdrawal.objects.get(tx_task=base_task)
        assert withdrawal.transfer_id == transfer.pk
        assert transfer.crypto_id == chain.native_coin_id
        assert transfer.to_address == recipient
        assert transfer.value == Decimal(value_raw)

    def test_native_internal_transfer_fails_when_real_tx_recipient_differs(self):
        chain = make_evm_chain(code="eth-native-real", chain_id=43013)
        address = make_evm_system_address(suffix="ad04")
        recipient = Web3.to_checksum_address("0x" + "72" * 20)
        wrong_recipient = Web3.to_checksum_address("0x" + "73" * 20)
        value_raw = 10_000
        task = make_tx_task(
            chain=chain,
            address=address,
            tx_type=TxTaskType.Withdrawal,
            tx_hash_suffix="7171",
            stage=TxTaskStage.PENDING_CHAIN,
        )
        _native_evm_task(
            base_task=task,
            address=address,
            chain=chain,
            to=recipient,
            value_raw=value_raw,
        )

        process_internal_transaction(
            chain=chain,
            tx={
                "hash": task.tx_hash,
                "from": address.address,
                "to": wrong_recipient,
                "value": value_raw,
                "input": "0x",
            },
            receipt={"status": 1, "logs": [], "blockNumber": 1},
        )

        task.refresh_from_db()
        assert task.stage == TxTaskStage.PENDING_CHAIN
        assert task.success is None
        assert not Transfer.objects.filter(hash=task.tx_hash).exists()

    def test_native_internal_transfer_fails_when_real_tx_value_differs(self):
        chain = make_evm_chain(code="eth-native-real-value", chain_id=43014)
        address = make_evm_system_address(suffix="ad05")
        recipient = Web3.to_checksum_address("0x" + "75" * 20)
        value_raw = 10_000
        task = make_tx_task(
            chain=chain,
            address=address,
            tx_type=TxTaskType.Withdrawal,
            tx_hash_suffix="7474",
            stage=TxTaskStage.PENDING_CHAIN,
        )
        _native_evm_task(
            base_task=task,
            address=address,
            chain=chain,
            to=recipient,
            value_raw=value_raw,
        )

        process_internal_transaction(
            chain=chain,
            tx={
                "hash": task.tx_hash,
                "from": address.address,
                "to": recipient,
                "value": value_raw - 1,
                "input": "0x",
            },
            receipt={"status": 1, "logs": [], "blockNumber": 1},
        )

        task.refresh_from_db()
        assert task.stage == TxTaskStage.PENDING_CHAIN
        assert task.success is None
        assert not Transfer.objects.filter(hash=task.tx_hash).exists()


class ProcessorFailureAtomicityTests(TestCase):
    def test_failed_finalize_rolls_back_tx_task_when_handler_raises(self):
        chain = make_evm_chain(code="eth-atomic", chain_id=43001)
        address = make_evm_system_address(suffix="a7")
        task = make_tx_task(
            chain=chain,
            address=address,
            tx_type=TxTaskType.Withdrawal,
            tx_hash_suffix="fa11",
            stage=TxTaskStage.PENDING_CHAIN,
        )
        original_handler = routing.INTERNAL_TX_HANDLERS[TxTaskType.Withdrawal]
        handler = MagicMock()
        handler.finalize_failed.side_effect = RuntimeError("business failure")
        routing.INTERNAL_TX_HANDLERS[TxTaskType.Withdrawal] = handler
        try:
            with self.assertRaisesRegex(RuntimeError, "business failure"):
                process_internal_transaction(
                    chain=chain,
                    tx={"hash": task.tx_hash, "from": address.address},
                    receipt={"status": 0, "logs": [], "blockNumber": 1},
                )
        finally:
            routing.INTERNAL_TX_HANDLERS[TxTaskType.Withdrawal] = original_handler

        task.refresh_from_db()
        assert task.stage == TxTaskStage.PENDING_CHAIN
        assert task.success is None

    def test_failed_finalize_skips_handler_when_task_already_finalized(self):
        chain = make_evm_chain(code="eth-finalize-once", chain_id=43015)
        address = make_evm_system_address(suffix="a9")
        task = make_tx_task(
            chain=chain,
            address=address,
            tx_type=TxTaskType.Withdrawal,
            tx_hash_suffix="7676",
            stage=TxTaskStage.PENDING_CHAIN,
        )
        TxTask.objects.filter(pk=task.pk).update(
            stage=TxTaskStage.FINALIZED,
            success=False,
        )
        original_handler = routing.INTERNAL_TX_HANDLERS[TxTaskType.Withdrawal]
        handler = MagicMock()
        routing.INTERNAL_TX_HANDLERS[TxTaskType.Withdrawal] = handler
        try:
            process_internal_transaction(
                chain=chain,
                tx={"hash": task.tx_hash, "from": address.address},
                receipt={"status": 0, "logs": [], "blockNumber": 1},
            )
        finally:
            routing.INTERNAL_TX_HANDLERS[TxTaskType.Withdrawal] = original_handler

        handler.finalize_failed.assert_not_called()


class ProcessorTimestampReuseTests(TestCase):
    def test_supplied_block_time_skips_block_lookup(self):
        chain = make_evm_chain(code="eth-ts", chain_id=43002)
        address = make_evm_system_address(suffix="a8")
        task = make_tx_task(
            chain=chain,
            address=address,
            tx_type=TxTaskType.Withdrawal,
            tx_hash_suffix="55",
            stage=TxTaskStage.PENDING_CHAIN,
        )
        fact = MatchedTransferFact(
            event_id="native:tx",
            from_address=address.address,
            to_address="0x00000000000000000000000000000000000000ff",
            crypto=chain.native_coin,
            value=Decimal("1000000000000000000"),
            amount=Decimal("1"),
        )
        original_matcher = routing.INTERNAL_TX_MATCHERS[TxTaskType.Withdrawal]
        routing.INTERNAL_TX_MATCHERS[TxTaskType.Withdrawal] = (
            lambda *, chain, tx_task, receipt, tx=None: fact
        )
        try:
            with patch("evm.internal_tx.processor._lookup_block_timestamp") as lookup:
                process_internal_transaction(
                    chain=chain,
                    tx={"hash": task.tx_hash, "from": address.address},
                    receipt={
                        "status": 1,
                        "logs": [],
                        "blockNumber": 1234,
                        "blockHash": make_tx_hash("bc"),
                    },
                    block_timestamp=1_700_000_000,
                    occurred_at=timezone.now(),
                )
            lookup.assert_not_called()
        finally:
            routing.INTERNAL_TX_MATCHERS[TxTaskType.Withdrawal] = original_matcher
