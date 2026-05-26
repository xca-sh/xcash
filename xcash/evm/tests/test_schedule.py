from unittest.mock import patch

from django.test import TestCase
from web3 import Web3

from chains.models import Address
from chains.models import AddressChainState
from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import TxTaskType
from chains.models import Chain
from chains.models import ChainType
from chains.models import Wallet
from currencies.models import Crypto
from evm.choices import TxKind
from evm.intents import EvmTxIntent
from evm.models import EvmTxTask


class EvmTxTaskScheduleTests(TestCase):
    def setUp(self):
        self.native = Crypto.objects.create(
            name="Schedule Ether",
            symbol="ETHSCH",
            decimals=18,
            coingecko_id="schedule-ether",
        )
        self.chain = Chain.objects.create(
            code="eth-schedule",
            name="Ethereum Schedule",
            type=ChainType.EVM,
            chain_id=999_901,
            rpc="http://localhost:8545",
            native_coin=self.native,
            active=True,
        )
        self.chain.base_transfer_gas = 21_000
        self.chain.erc20_transfer_gas = 65_000
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a01"
            ),
        )
        self.recipient = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000b02"
        )

    def _intent(self, **overrides):
        values = {
            "address": self.address,
            "chain": self.chain,
            "tx_kind": TxKind.NATIVE_TRANSFER,
            "to": self.recipient,
            "value": 1_230_000_000_000_000_000,
            "data": "",
            "gas": 21_000,
            "tx_type": TxTaskType.Withdrawal,
            "verify_fn": None,
        }
        values.update(overrides)
        return EvmTxIntent(**values)

    def test_schedule_persists_base_and_evm_fields_from_intent(self):
        intent = self._intent(
            to=Web3.to_checksum_address("0x0000000000000000000000000000000000000c03"),
            value=456,
            data="0x1234",
            gas=88_000,
            tx_kind=TxKind.CONTRACT_CALL,
        )

        task = EvmTxTask.schedule(intent)

        self.assertEqual(task.address, intent.address)
        self.assertEqual(task.chain, intent.chain)
        self.assertEqual(task.tx_kind, intent.tx_kind)
        self.assertEqual(task.to, intent.to)
        self.assertEqual(task.value, intent.value)
        self.assertEqual(task.data, intent.data)
        self.assertEqual(task.gas, intent.gas)
        self.assertEqual(task.nonce, 0)

        base_task = task.base_task
        self.assertEqual(base_task.chain, intent.chain)
        self.assertEqual(base_task.address, intent.address)
        self.assertEqual(base_task.tx_type, intent.tx_type)
        self.assertEqual(base_task.stage, TxTaskStage.QUEUED)
        self.assertIsNone(base_task.success)

        state = AddressChainState.objects.get(address=self.address, chain=self.chain)
        self.assertEqual(state.address, self.address)
        self.assertEqual(state.chain, self.chain)

    def test_schedule_runs_verify_fn_inside_lock_before_nonce_allocation(self):
        events = []
        original_acquire = AddressChainState.acquire_for_update

        def acquire_for_update(*, address, chain):
            events.append("lock")
            return original_acquire(address=address, chain=chain)

        def verify():
            events.append("verify")

        def next_nonce(address, chain):
            events.append("nonce")
            return 0

        intent = self._intent(verify_fn=verify)

        with (
            patch.object(
                AddressChainState,
                "acquire_for_update",
                side_effect=acquire_for_update,
            ),
            patch.object(EvmTxTask, "_next_nonce", side_effect=next_nonce),
        ):
            task = EvmTxTask.schedule(intent)

        self.assertEqual(events, ["lock", "verify", "nonce"])
        self.assertEqual(task.nonce, 0)
        state = AddressChainState.objects.get(address=self.address, chain=self.chain)
        self.assertEqual(state.address, self.address)
        self.assertEqual(state.chain, self.chain)

    def test_schedule_rolls_back_when_verify_fn_raises(self):
        def reject():
            raise RuntimeError("balance changed")

        with self.assertRaisesRegex(RuntimeError, "balance changed"):
            EvmTxTask.schedule(self._intent(verify_fn=reject))

        self.assertEqual(TxTask.objects.count(), 0)
        self.assertEqual(EvmTxTask.objects.count(), 0)
        self.assertEqual(AddressChainState.objects.count(), 0)
