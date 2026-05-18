from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.test import TransactionTestCase
from django.utils import timezone
from web3 import Web3

from chains.models import AddressUsage
from chains.models import BroadcastTask
from chains.models import BroadcastTaskFailureReason
from chains.models import BroadcastTaskResult
from chains.models import BroadcastTaskStage
from chains.models import OnchainActionType
from chains.models import OnchainTransfer
from deposits.models import Deposit
from deposits.models import DepositAddress
from deposits.models import DepositCollection
from deposits.models import DepositStatus
from deposits.models import GasRecharge
from evm.choices import TxKind
from evm.contracts_codec import collector_init_code_hash
from evm.intents import Eip3009Authorization
from evm.internal_tx import handlers as handlers_mod
from evm.internal_tx import matchers as matchers_mod
from evm.internal_tx.facts import MatchedTransferFact
from evm.internal_tx.processor import process_internal_transaction
from evm.models import ContractDeployCollectionStatus
from evm.models import EvmBroadcastTask
from evm.models import X402FacilitationStatus
from evm.services.create2 import ContractDeployCollectionService
from evm.services.x402 import X402FacilitationService
from evm.tests._fixtures import make_broadcast_task
from evm.tests._fixtures import make_erc20_token
from evm.tests._fixtures import make_evm_chain
from evm.tests._fixtures import make_evm_system_address
from evm.tests._fixtures import make_tx_hash
from projects.models import Project
from users.models import Customer
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


def _base_task_without_asset_fields(*, chain, address, action_type, tx_hash_suffix):
    return BroadcastTask.objects.create(
        chain=chain,
        address=address,
        action_type=action_type,
        tx_hash=make_tx_hash(tx_hash_suffix),
        stage=BroadcastTaskStage.PENDING_CHAIN,
        result=BroadcastTaskResult.UNKNOWN,
    )


def _native_evm_task(*, base_task, address, chain, to, value_raw, nonce=0):
    return EvmBroadcastTask.objects.create(
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


class DirectInternalLifecycleWithoutBroadcastAssetFieldsTests(TestCase):
    def test_native_withdrawal_matches_from_withdrawal_and_evm_task(self):
        chain = make_evm_chain(code="eth-noasset-wd", chain_id=43010)
        vault = make_evm_system_address(suffix="ad01", usage=AddressUsage.Vault)
        recipient = Web3.to_checksum_address("0x" + "91" * 20)
        value_raw = 1_250_000_000_000_000_000
        base_task = _base_task_without_asset_fields(
            chain=chain,
            address=vault,
            action_type=OnchainActionType.Withdrawal,
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
            broadcast_task=base_task,
            status=WithdrawalStatus.PENDING,
        )

        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            ts.return_value = (1_700_000_000, timezone.now())
            process_internal_transaction(
                chain=chain,
                tx={"hash": base_task.tx_hash, "from": vault.address},
                receipt={"status": 1, "logs": [], "blockNumber": 10},
            )

        transfer = OnchainTransfer.objects.get(hash=base_task.tx_hash)
        transfer.process()
        withdrawal = Withdrawal.objects.get(broadcast_task=base_task)
        assert withdrawal.transfer_id == transfer.pk
        assert transfer.crypto_id == chain.native_coin_id
        assert transfer.to_address == recipient
        assert transfer.value == Decimal(value_raw)

    def test_gas_recharge_matches_from_gas_recharge_and_evm_task(self):
        chain = make_evm_chain(code="eth-noasset-gas", chain_id=43011)
        vault = make_evm_system_address(suffix="ad02", usage=AddressUsage.Vault)
        wallet = vault.wallet
        deposit_addr = make_evm_system_address(
            wallet=wallet,
            suffix="ad12",
            usage=AddressUsage.Deposit,
        )
        project = Project.objects.create(name="NoAssetGas", wallet=wallet)
        customer = Customer.objects.create(project=project, uid="noasset-gas")
        deposit_address = DepositAddress.objects.create(
            customer=customer,
            chain_type=chain.type,
            address=deposit_addr,
        )
        value_raw = 300_000_000_000_000
        base_task = _base_task_without_asset_fields(
            chain=chain,
            address=vault,
            action_type=OnchainActionType.GasRecharge,
            tx_hash_suffix="0a01",
        )
        _native_evm_task(
            base_task=base_task,
            address=vault,
            chain=chain,
            to=deposit_addr.address,
            value_raw=value_raw,
        )
        GasRecharge.objects.create(
            deposit_address=deposit_address,
            broadcast_task=base_task,
        )

        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            ts.return_value = (1_700_000_000, timezone.now())
            process_internal_transaction(
                chain=chain,
                tx={"hash": base_task.tx_hash, "from": vault.address},
                receipt={"status": 1, "logs": [], "blockNumber": 11},
            )

        transfer = OnchainTransfer.objects.get(hash=base_task.tx_hash)
        transfer.process()
        recharge = GasRecharge.objects.get(broadcast_task=base_task)
        assert recharge.transfer_id == transfer.pk
        assert transfer.type == OnchainActionType.GasRecharge
        assert transfer.to_address == deposit_addr.address
        assert transfer.value == Decimal(value_raw)

    def test_erc20_collection_matches_from_deposits_and_evm_task(self):
        chain = make_evm_chain(code="eth-noasset-col", chain_id=43012)
        crypto = make_erc20_token(chain=chain, address_suffix="cafe", decimals=6)
        vault = make_evm_system_address(suffix="ad03", usage=AddressUsage.Vault)
        wallet = vault.wallet
        deposit_addr = make_evm_system_address(
            wallet=wallet,
            suffix="ad13",
            usage=AddressUsage.Deposit,
        )
        project = Project.objects.create(name="NoAssetCollection", wallet=wallet)
        customer = Customer.objects.create(project=project, uid="noasset-collection")
        DepositAddress.objects.create(
            customer=customer,
            chain_type=chain.type,
            address=deposit_addr,
        )
        recipient = Web3.to_checksum_address("0x" + "92" * 20)
        value_raw = 2_500_000
        base_task = _base_task_without_asset_fields(
            chain=chain,
            address=deposit_addr,
            action_type=OnchainActionType.DepositCollection,
            tx_hash_suffix="c001",
        )
        encoded_args = (
            "0x"
            "a9059cbb"
            f"{recipient.lower().replace('0x', '').rjust(64, '0')}"
            f"{hex(value_raw)[2:].rjust(64, '0')}"
        )
        EvmBroadcastTask.objects.create(
            base_task=base_task,
            address=deposit_addr,
            chain=chain,
            nonce=0,
            to=crypto.address(chain),
            value=0,
            data=encoded_args,
            gas=65_000,
            tx_kind=TxKind.CONTRACT_CALL,
        )
        collection = DepositCollection.objects.create(
            collection_hash=None,
            broadcast_task=base_task,
        )
        deposit_transfer = OnchainTransfer.objects.create(
            chain=chain,
            block=1,
            hash=make_tx_hash("de01"),
            event_id="erc20:1",
            crypto=crypto,
            from_address=Web3.to_checksum_address("0x" + "93" * 20),
            to_address=deposit_addr.address,
            value=value_raw,
            amount=Decimal("2.5"),
            timestamp=1_700_000_000,
            datetime=timezone.now(),
        )
        Deposit.objects.create(
            customer=customer,
            transfer=deposit_transfer,
            status=DepositStatus.COMPLETED,
            collection=collection,
        )

        receipt = {
            "status": 1,
            "logs": [
                _erc20_transfer_log(
                    token=crypto.address(chain),
                    from_addr=deposit_addr.address,
                    to_addr=recipient,
                    value_raw=value_raw,
                    log_index=3,
                )
            ],
            "blockNumber": 12,
        }
        with patch("evm.internal_tx.processor._lookup_block_timestamp") as ts:
            ts.return_value = (1_700_000_000, timezone.now())
            process_internal_transaction(
                chain=chain,
                tx={"hash": base_task.tx_hash, "from": deposit_addr.address},
                receipt=receipt,
            )

        transfer = OnchainTransfer.objects.get(hash=base_task.tx_hash, event_id="erc20:3")
        transfer.process()
        collection.refresh_from_db()
        assert collection.transfer_id == transfer.pk
        assert collection.collection_hash == base_task.tx_hash
        assert transfer.type == OnchainActionType.DepositCollection


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
        assert transfer.type == OnchainActionType.X402Facilitate

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
            expected_collect_value_raw=value_raw,
            gas=200_000,
        )
        collection = result.collection
        assert collection.collector_init_code_hash == collector_init_code_hash(
            to=vault_address,
            token=crypto.address(chain),
        )
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
        assert transfer.type == OnchainActionType.ContractDeployCollect


class ProcessorFailureAtomicityTests(TransactionTestCase):
    def test_failed_finalize_rolls_back_broadcast_task_when_handler_raises(self):
        chain = make_evm_chain(code="eth-atomic", chain_id=43001)
        address = make_evm_system_address(suffix="a7")
        task = make_broadcast_task(
            chain=chain,
            address=address,
            action_type=OnchainActionType.Withdrawal,
            tx_hash_suffix="fa11",
            stage=BroadcastTaskStage.PENDING_CHAIN,
        )
        original_handler = handlers_mod.HANDLERS[OnchainActionType.Withdrawal]
        handler = MagicMock()
        handler.finalize_failed.side_effect = RuntimeError("business failure")
        handlers_mod.HANDLERS[OnchainActionType.Withdrawal] = handler
        try:
            with self.assertRaisesRegex(RuntimeError, "business failure"):
                process_internal_transaction(
                    chain=chain,
                    tx={"hash": task.tx_hash, "from": address.address},
                    receipt={"status": 0, "logs": [], "blockNumber": 1},
                )
        finally:
            handlers_mod.HANDLERS[OnchainActionType.Withdrawal] = original_handler

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
            action_type=OnchainActionType.Withdrawal,
            tx_hash_suffix="55",
            stage=BroadcastTaskStage.PENDING_CHAIN,
        )
        fact = MatchedTransferFact(
            event_id="native:tx",
            from_address=address.address,
            to_address="0x00000000000000000000000000000000000000ff",
            crypto=chain.native_coin,
            value=Decimal("1000000000000000000"),
            amount=Decimal("1"),
        )
        original_matcher = matchers_mod.MATCHERS[OnchainActionType.Withdrawal]
        matchers_mod.MATCHERS[OnchainActionType.Withdrawal] = (
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
            matchers_mod.MATCHERS[OnchainActionType.Withdrawal] = original_matcher
