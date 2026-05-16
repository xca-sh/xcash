from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

from chains.models import (
    AddressUsage,
    BroadcastTaskFailureReason,
    BroadcastTaskResult,
    BroadcastTaskStage,
    OnchainTransfer,
    TransferType,
)
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from evm.intents import Eip3009Authorization
from evm.internal_tx import handlers as handlers_mod
from evm.internal_tx import matchers as matchers_mod
from evm.internal_tx.facts import MatchedTransferFact
from evm.internal_tx.processor import process_internal_transaction
from evm.models import ContractDeployCollectionStatus, X402FacilitationStatus
from evm.services.create2 import ContractDeployCollectionService
from evm.services.x402 import X402FacilitationService
from evm.tests._fixtures import (
    make_broadcast_task,
    make_erc20_token,
    make_evm_chain,
    make_evm_system_address,
    make_tx_hash,
)
from web3 import Web3


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


class X402InternalLifecycleTests(TestCase):
    def setUp(self):
        self.chain = make_evm_chain(code="eth-x4-life", chain_id=42161)
        self.crypto = make_erc20_token(chain=self.chain, address_suffix="ab", decimals=6)
        self.facilitator = make_evm_system_address(
            suffix="fc",
            usage=AddressUsage.Vault,
        )
        self.auth_from = Web3.to_checksum_address("0x" + "31" * 20)
        self.auth_to = Web3.to_checksum_address("0x" + "41" * 20)
        self.value_raw = 1_000_000
        result = X402FacilitationService.create_and_schedule(
            facilitator=self.facilitator,
            chain=self.chain,
            crypto=self.crypto,
            authorization=Eip3009Authorization(
                from_address=self.auth_from,
                to=self.auth_to,
                value=self.value_raw,
                valid_after=1_700_000_000,
                valid_before=1_700_000_900,
                nonce=b"\x01" * 32,
                v=27,
                r=b"\x02" * 32,
                s=b"\x03" * 32,
            ),
        )
        self.facilitation = result.facilitation
        self.base_task = self.facilitation.broadcast_task
        self.base_task.tx_hash = make_tx_hash("402")
        self.base_task.stage = BroadcastTaskStage.PENDING_CHAIN
        self.base_task.save(update_fields=["tx_hash", "stage", "updated_at"])

    def _receipt(self, *, status: int, with_matching_log: bool):
        logs = []
        if with_matching_log:
            logs.append(
                _erc20_transfer_log(
                    token=self.crypto.address(self.chain),
                    from_addr=self.auth_from,
                    to_addr=self.auth_to,
                    value_raw=self.value_raw,
                    log_index=5,
                )
            )
        return {
            "status": status,
            "logs": logs,
            "blockNumber": 1234,
            "blockHash": make_tx_hash("aa"),
        }

    def test_success_creates_transfer_and_binds_facilitation(self):
        tx = {"hash": self.base_task.tx_hash, "from": self.facilitator.address}
        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            ts.return_value = (1_700_000_000, timezone.now())
            process_internal_transaction(
                chain=self.chain,
                tx=tx,
                receipt=self._receipt(status=1, with_matching_log=True),
            )

        transfer = OnchainTransfer.objects.get(
            chain=self.chain,
            hash=self.base_task.tx_hash,
            event_id="erc20:5",
        )
        transfer.process()

        self.facilitation.refresh_from_db()
        assert self.facilitation.transfer_id == transfer.pk
        assert self.facilitation.status == X402FacilitationStatus.BROADCASTED
        assert transfer.type == TransferType.X402Facilitate

    def test_missing_expected_transfer_fails_closed(self):
        tx = {"hash": self.base_task.tx_hash, "from": self.facilitator.address}
        process_internal_transaction(
            chain=self.chain,
            tx=tx,
            receipt=self._receipt(status=1, with_matching_log=False),
        )

        self.base_task.refresh_from_db()
        self.facilitation.refresh_from_db()
        assert self.base_task.stage == BroadcastTaskStage.FINALIZED
        assert self.base_task.result == BroadcastTaskResult.FAILED
        assert (
            self.base_task.failure_reason
            == BroadcastTaskFailureReason.EXPECTED_TRANSFER_MISSING
        )
        assert self.facilitation.status == X402FacilitationStatus.FAILED
        assert not OnchainTransfer.objects.filter(hash=self.base_task.tx_hash).exists()


class Create2InternalLifecycleTests(TestCase):
    def test_success_creates_transfer_and_binds_collection(self):
        chain = make_evm_chain(code="eth-c2-life", chain_id=42903)
        chain.create2_factory_address = Web3.to_checksum_address("0x" + "11" * 20)
        chain.save(update_fields=["create2_factory_address"])
        crypto = make_erc20_token(chain=chain, address_suffix="cd", decimals=6)
        deployer = make_evm_system_address(suffix="d4", usage=AddressUsage.Vault)
        vault_address = Web3.to_checksum_address("0x" + "44" * 20)
        value_raw = 1_000_000
        result = ContractDeployCollectionService.create_and_schedule(
            deployer=deployer,
            chain=chain,
            crypto=crypto,
            salt=b"\x01" * 32,
            vault_address=vault_address,
            collector_init_code_hash=b"\x02" * 32,
            expected_collect_value_raw=value_raw,
            gas=200_000,
        )
        collection = result.collection
        base_task = collection.broadcast_task
        base_task.tx_hash = make_tx_hash("c2e")
        base_task.stage = BroadcastTaskStage.PENDING_CHAIN
        base_task.save(update_fields=["tx_hash", "stage", "updated_at"])

        receipt = {
            "status": 1,
            "logs": [
                _erc20_transfer_log(
                    token=crypto.address(chain),
                    from_addr=collection.collector_address,
                    to_addr=collection.vault_address,
                    value_raw=value_raw,
                    log_index=7,
                )
            ],
            "blockNumber": 1234,
            "blockHash": make_tx_hash("c2b"),
        }
        tx = {"hash": base_task.tx_hash, "from": deployer.address}
        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            ts.return_value = (1_700_000_000, timezone.now())
            process_internal_transaction(chain=chain, tx=tx, receipt=receipt)

        transfer = OnchainTransfer.objects.get(
            chain=chain,
            hash=base_task.tx_hash,
            event_id="erc20:7",
        )
        transfer.process()

        collection.refresh_from_db()
        assert collection.transfer_id == transfer.pk
        assert collection.status == ContractDeployCollectionStatus.BROADCASTED
        assert transfer.type == TransferType.ContractDeployCollect


class ProcessorFailureAtomicityTests(TransactionTestCase):
    def test_failed_finalize_rolls_back_broadcast_task_when_handler_raises(self):
        chain = make_evm_chain(code="eth-atomic", chain_id=43001)
        address = make_evm_system_address(suffix="a7")
        task = make_broadcast_task(
            chain=chain,
            address=address,
            transfer_type=TransferType.Withdrawal,
            tx_hash_suffix="fa11",
            stage=BroadcastTaskStage.PENDING_CHAIN,
        )
        original_handler = handlers_mod.HANDLERS[TransferType.Withdrawal]
        handler = MagicMock()
        handler.finalize_failed.side_effect = RuntimeError("business failure")
        handlers_mod.HANDLERS[TransferType.Withdrawal] = handler
        try:
            with self.assertRaisesRegex(RuntimeError, "business failure"):
                process_internal_transaction(
                    chain=chain,
                    tx={"hash": task.tx_hash, "from": address.address},
                    receipt={"status": 0, "logs": [], "blockNumber": 1},
                )
        finally:
            handlers_mod.HANDLERS[TransferType.Withdrawal] = original_handler

        task.refresh_from_db()
        assert task.stage == BroadcastTaskStage.PENDING_CHAIN
        assert task.result == BroadcastTaskResult.UNKNOWN
        assert task.failure_reason == ""


class ProcessorTimestampReuseTests(TestCase):
    def test_supplied_block_time_skips_block_lookup(self):
        chain = make_evm_chain(code="eth-ts", chain_id=43002)
        address = make_evm_system_address(suffix="a8")
        task = make_broadcast_task(
            chain=chain,
            address=address,
            transfer_type=TransferType.Withdrawal,
            tx_hash_suffix="55",
            stage=BroadcastTaskStage.PENDING_CHAIN,
        )
        fact = MatchedTransferFact(
            event_id="native:tx",
            from_address=address.address,
            to_address=task.recipient,
            crypto=chain.native_coin,
            value=Decimal("1000000000000000000"),
            amount=Decimal("1"),
        )
        original_matcher = matchers_mod.MATCHERS[TransferType.Withdrawal]
        matchers_mod.MATCHERS[TransferType.Withdrawal] = (
            lambda *, chain, broadcast_task, receipt: fact
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
            matchers_mod.MATCHERS[TransferType.Withdrawal] = original_matcher
