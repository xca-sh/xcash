from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from eth_utils import keccak
from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import TxTaskStage
from chains.models import TxTaskType
from chains.models import Wallet
from core.models import SystemWallet
from currencies.models import ChainToken
from currencies.models import Crypto
from deposits.models import Deposit
from deposits.models import DepositStatus
from evm.choices import TxKind
from evm.constants import XCASH_DEPOSIT_FACTORY_ADDRESS
from evm.intents import DEFAULT_DEPOSIT_SLOT_COLLECT_GAS
from evm.intents import DEFAULT_DEPOSIT_SLOT_DEPLOY_GAS
from evm.intents import build_deposit_slot_collect_intent
from evm.intents import build_deposit_slot_deploy_intent
from evm.models import DepositSlot
from evm.models import DepositSlotUsage
from evm.models import EvmTxTask
from invoices.models import Invoice
from invoices.models import InvoiceBillingMode
from invoices.models import InvoiceStatus
from projects.models import Project
from users.models import Customer


def _fake_address():
    return object()


def _fake_chain():
    return object()


def _selector(signature: str) -> str:
    return Web3.keccak(text=signature)[:4].hex()


def test_build_deposit_slot_deploy_intent_encodes_factory_call():
    factory_address = "0x" + "a" * 40
    vault_address = "0x" + "b" * 40
    salt = bytes.fromhex("11" * 32)

    intent = build_deposit_slot_deploy_intent(
        address=_fake_address(),
        chain=_fake_chain(),
        factory_address=factory_address,
        vault_address=vault_address,
        salt=salt,
    )

    assert intent.tx_type == TxTaskType.DepositSlotDeploy
    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(factory_address)
    assert intent.value == 0
    assert intent.gas == DEFAULT_DEPOSIT_SLOT_DEPLOY_GAS
    assert intent.data.startswith(f"0x{_selector('deployDepositSlot(address,bytes32)')}")
    assert Web3.to_checksum_address(vault_address)[2:].lower() in intent.data
    assert salt.hex() in intent.data


def test_build_deposit_slot_deploy_intent_rejects_non_32_byte_salt():
    with pytest.raises(ValueError, match="salt must be 32 bytes"):
        build_deposit_slot_deploy_intent(
            address=_fake_address(),
            chain=_fake_chain(),
            factory_address="0x" + "a" * 40,
            vault_address="0x" + "b" * 40,
            salt=b"short",
        )


def test_build_deposit_slot_collect_intent_encodes_slot_call():
    deposit_slot_address = "0x" + "c" * 40
    token_address = "0x" + "d" * 40

    intent = build_deposit_slot_collect_intent(
        address=_fake_address(),
        chain=_fake_chain(),
        deposit_slot_address=deposit_slot_address,
        token_address=token_address,
    )

    assert intent.tx_type == TxTaskType.DepositSlotCollect
    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(deposit_slot_address)
    assert intent.value == 0
    assert intent.gas == DEFAULT_DEPOSIT_SLOT_COLLECT_GAS
    assert intent.data.startswith(f"0x{_selector('collect(address)')}")
    assert Web3.to_checksum_address(token_address)[2:].lower() in intent.data


class DepositSlotAddressSchedulingTests(TestCase):
    def setUp(self):
        self.native = Crypto.objects.create(
            name="Deposit Slot Native",
            symbol="DSN",
            coingecko_id="deposit-slot-native",
        )
        self.chain = Chain.objects.create(
            code="deposit-slot-chain",
            name="Deposit Slot Chain",
            type=ChainType.EVM,
            chain_id=991_100,
            rpc="http://deposit-slot.local",
            native_coin=self.native,
            active=True,
        )
        self.wallet = Wallet.objects.create()
        self.project = Project.objects.create(
            name="Deposit Slot Project",
            wallet=self.wallet,
        )
        self.project.vault = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000f01"
        )
        self.project.save(update_fields=["vault"])
        self.customer = Customer.objects.create(
            project=self.project,
            uid="deposit-slot-customer",
        )
        self.vault = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000d01"
            ),
        )
        self.system_wallet = Wallet.objects.create()
        self.system_wallet_marker = SystemWallet.objects.create(
            wallet=self.system_wallet
        )
        self.system_sender = Address.objects.create(
            wallet=self.system_wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000d02"
            ),
        )
        self.token = Crypto.objects.create(
            name="Deposit Slot Token",
            symbol="DST",
            coingecko_id="deposit-slot-token",
        )
        self.token_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000e20"
        )
        ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=self.token_address,
        )
        deployed_patch = patch.object(
            DepositSlot,
            "_is_deployed_on_chain",
            return_value=False,
        )
        deployed_patch.start()
        self.addCleanup(deployed_patch.stop)

    def _patch_signer(self):
        # factory / template 地址已通过 evm.constants 模块常量注入，无需再 mock 部署配置。
        return patch(
            "chains.signer.get_signer_backend",
            return_value=SimpleNamespace(
                derive_address=lambda **kwargs: (
                    self.system_sender.address
                    if kwargs["wallet"].pk == self.system_wallet.pk
                    else self.vault.address
                )
            ),
        )

    def test_first_get_address_schedules_deploy_after_commit(self):
        self.project.vault = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000f01"
        )
        self.project.save(update_fields=["vault"])
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        slot = DepositSlot.objects.get(chain=self.chain, customer=self.customer)
        self.assertEqual(address, slot.address)
        self.assertEqual(schedule.call_count, 1)

        intent = schedule.call_args.args[0]
        self.assertEqual(intent.tx_type, TxTaskType.DepositSlotDeploy)
        self.assertEqual(intent.address, self.system_sender)
        self.assertEqual(intent.to, XCASH_DEPOSIT_FACTORY_ADDRESS)
        self.assertIn(self.project.vault[2:].lower(), intent.data)

    def test_customer_deposit_slot_records_project_usage_without_invoice_index(self):
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        slot = DepositSlot.objects.get(chain=self.chain, customer=self.customer)
        self.assertEqual(slot.project, self.project)
        self.assertEqual(slot.usage, DepositSlotUsage.DEPOSIT)
        self.assertIsNone(slot.invoice_index)

    def test_build_salt_dispatches_by_usage(self):
        deposit_salt = DepositSlot.build_salt(
            usage=DepositSlotUsage.DEPOSIT,
            customer=self.customer,
        )
        invoice_salt = DepositSlot.build_salt(
            usage=DepositSlotUsage.INVOICE,
            project_id=self.project.pk,
            invoice_index=3,
        )

        self.assertEqual(
            deposit_salt,
            keccak(
                b"xcash:deposit-slot:deposit:"
                + str(self.project.pk).encode()
                + b":"
                + self.customer.uid.encode()
            ),
        )
        self.assertEqual(
            invoice_salt,
            keccak(
                b"xcash:deposit-slot:invoice:"
                + str(self.project.pk).encode()
                + b":"
                + b"3"
            ),
        )

    def test_schedule_deploy_records_deploy_tx_task(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with signer_patch:
            task = DepositSlot.schedule_deploy(slot.pk)

        slot.refresh_from_db()
        self.assertEqual(slot.deploy_tx_task, task)

    def test_schedule_deploy_uses_system_wallet_sender(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with signer_patch:
            task = DepositSlot.schedule_deploy(slot.pk)

        self.assertEqual(task.address, self.system_sender)

    def test_schedule_deploy_skips_when_slot_already_deployed_on_chain(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(DepositSlot, "_is_deployed_on_chain", return_value=True),
            patch.object(EvmTxTask, "schedule") as schedule,
        ):
            task = DepositSlot.schedule_deploy(slot.pk)

        self.assertIsNone(task)
        schedule.assert_not_called()
        slot.refresh_from_db()
        self.assertIsNone(slot.deploy_tx_task)

    def test_schedule_deploy_returns_recorded_unfinalized_deploy_tx_task(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with signer_patch:
            existing_task = DepositSlot.schedule_deploy(slot.pk)

        with signer_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = DepositSlot.schedule_deploy(slot.pk)

        self.assertEqual(task.pk, existing_task.pk)
        schedule.assert_not_called()

    def test_schedule_deploy_skips_successful_recorded_deploy_tx_task(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with signer_patch:
            existing_task = DepositSlot.schedule_deploy(slot.pk)
        existing_task.base_task.stage = TxTaskStage.FINALIZED
        existing_task.base_task.success = True
        existing_task.base_task.save(update_fields=["stage", "success", "updated_at"])

        with signer_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = DepositSlot.schedule_deploy(slot.pk)

        self.assertEqual(task.pk, existing_task.pk)
        schedule.assert_not_called()

    def test_schedule_deploy_recreates_after_failed_recorded_deploy_tx_task(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with signer_patch:
            failed_task = DepositSlot.schedule_deploy(slot.pk)
        failed_task.base_task.stage = TxTaskStage.FINALIZED
        failed_task.base_task.success = False
        failed_task.base_task.save(update_fields=["stage", "success", "updated_at"])

        with signer_patch:
            new_task = DepositSlot.schedule_deploy(slot.pk)

        slot.refresh_from_db()
        self.assertNotEqual(new_task.pk, failed_task.pk)
        self.assertEqual(slot.deploy_tx_task, new_task)

    def test_get_address_rejects_project_without_vault(self):
        Project.objects.filter(pk=self.project.pk).update(vault=None)
        self.project.refresh_from_db()
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.assertRaisesRegex(RuntimeError, "DepositSlot Vault 地址未配置"),
        ):
            DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        schedule.assert_not_called()
        self.assertFalse(
            DepositSlot.objects.filter(chain=self.chain, customer=self.customer).exists()
        )

    def test_same_customer_can_reuse_deposit_slot_address_across_evm_chains(self):
        second_chain = Chain.objects.create(
            code="deposit-slot-chain-2",
            name="Deposit Slot Chain 2",
            type=ChainType.EVM,
            chain_id=991_101,
            rpc="http://deposit-slot-2.local",
            native_coin=self.native,
            active=True,
        )
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            first_address = DepositSlot.get_deposit_address(
                chain=self.chain,
                customer=self.customer,
            )
            second_address = DepositSlot.get_deposit_address(
                chain=second_chain,
                customer=self.customer,
            )

        self.assertEqual(second_address, first_address)
        self.assertEqual(
            DepositSlot.objects.filter(address=first_address).count(),
            2,
        )
        self.assertEqual(
            set(
                DepositSlot.objects.filter(address=first_address).values_list(
                    "chain_id",
                    flat=True,
                )
            ),
            {self.chain.pk, second_chain.pk},
        )

    def test_existing_slot_without_deploy_task_recovers_schedule(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        self.assertEqual(address, slot.address)
        self.assertEqual(schedule.call_count, 1)
        intent = schedule.call_args.args[0]
        self.assertEqual(intent.tx_type, TxTaskType.DepositSlotDeploy)
        self.assertEqual(intent.address, self.system_sender)
        self.assertEqual(intent.to, XCASH_DEPOSIT_FACTORY_ADDRESS)

    def test_existing_slot_with_same_deploy_task_does_not_duplicate_schedule(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        with signer_patch:
            existing_task = DepositSlot.schedule_deploy(slot.pk)

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        self.assertEqual(address, slot.address)
        self.assertEqual(
            EvmTxTask.objects.filter(pk=existing_task.pk).count(),
            1,
        )
        schedule.assert_not_called()

    def test_schedule_deploy_rejects_current_vault_address_mismatch(self):
        slot = self._create_deposit_slot(
            vault_address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000bad"
            )
        )
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.assertRaisesRegex(RuntimeError, "Vault 地址不一致"),
        ):
            DepositSlot.schedule_deploy(slot.pk)

        schedule.assert_not_called()

    def test_second_get_address_returns_existing_address_without_scheduling(self):
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            self.captureOnCommitCallbacks(execute=True),
        ):
            first_address = DepositSlot.get_deposit_address(
                chain=self.chain,
                customer=self.customer,
            )

        with (
            signer_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            second_address = DepositSlot.get_deposit_address(
                chain=self.chain,
                customer=self.customer,
            )

        self.assertEqual(second_address, first_address)
        schedule.assert_not_called()

    def test_integrity_error_lookup_path_does_not_schedule_duplicate_deploy(self):
        slot = DepositSlot.objects.create(
            customer=self.customer,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            vault_address=self.vault.address,
            salt=b"\x11" * 32,
        )
        signer_patch = self._patch_signer()

        with (
            signer_patch,
            patch.object(
                DepositSlot.objects,
                "filter",
                return_value=SimpleNamespace(first=lambda: None),
            ),
            patch.object(
                DepositSlot.objects,
                "get_or_create",
                side_effect=IntegrityError("duplicate"),
            ),
            patch.object(DepositSlot.objects, "get", return_value=slot),
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        self.assertEqual(address, slot.address)
        schedule.assert_not_called()

    def test_integrity_error_lookup_failure_reraises_original_integrity_error(self):
        signer_patch = self._patch_signer()
        original_error = IntegrityError("duplicate")

        with (
            signer_patch,
            patch.object(
                DepositSlot.objects,
                "filter",
                return_value=SimpleNamespace(first=lambda: None),
            ),
            patch.object(
                DepositSlot.objects,
                "get_or_create",
                side_effect=original_error,
            ),
            patch.object(
                DepositSlot.objects,
                "get",
                side_effect=DepositSlot.DoesNotExist,
            ),
            self.assertRaises(IntegrityError) as raised,
        ):
            DepositSlot.get_deposit_address(chain=self.chain, customer=self.customer)

        self.assertIs(raised.exception, original_error)

    def test_schedule_collect_for_deposit_uses_vault_sender_and_slot_target(self):
        slot = self._create_deposit_slot()
        deposit = self._create_deposit(slot=slot)
        signer_patch = self._patch_signer()

        with signer_patch:
            task = DepositSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertEqual(task.address, self.vault)
        self.assertEqual(task.chain, self.chain)
        self.assertEqual(task.to, slot.address)
        self.assertEqual(task.base_task.tx_type, TxTaskType.DepositSlotCollect)
        self.assertTrue(task.data.startswith(f"0x{_selector('collect(address)')}"))
        self.assertIn(self.token_address[2:].lower(), task.data)

    def test_schedule_collect_for_invoice_uses_contract_slot_and_token(self):
        slot = DepositSlot.objects.create(
            project=self.project,
            chain=self.chain,
            usage=DepositSlotUsage.INVOICE,
            invoice_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a21"
            ),
            vault_address=self.project.vault,
            salt=b"\x21" * 32,
        )
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="invoice-slot-collect",
            title="Invoice slot collect",
            currency=self.token.symbol,
            amount="10.00000000",
            methods={self.token.symbol: [self.chain.code]},
            crypto=self.token,
            chain=self.chain,
            pay_amount="10.00000000",
            pay_address=slot.address,
            billing_mode=InvoiceBillingMode.CONTRACT,
            status=InvoiceStatus.COMPLETED,
            expires_at=timezone.now(),
        )

        with self._patch_signer():
            task = DepositSlot.schedule_collect_for_invoice(invoice.pk)

        self.assertEqual(task.address, self.vault)
        self.assertEqual(task.chain, self.chain)
        self.assertEqual(task.to, slot.address)
        self.assertEqual(task.base_task.tx_type, TxTaskType.DepositSlotCollect)
        self.assertTrue(task.data.startswith(f"0x{_selector('collect(address)')}"))
        self.assertIn(self.token_address[2:].lower(), task.data)

    def test_schedule_collect_for_deposit_is_idempotent_for_unfinalized_task(self):
        slot = self._create_deposit_slot()
        deposit = self._create_deposit(slot=slot)
        signer_patch = self._patch_signer()

        with signer_patch:
            existing = DepositSlot.schedule_collect_for_deposit(deposit.pk)

        with signer_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = DepositSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertEqual(task.pk, existing.pk)
        schedule.assert_not_called()

    def test_schedule_collect_for_deposit_reuses_unknown_unfinalized_tasks(self):
        slot = self._create_deposit_slot()
        signer_patch = self._patch_signer()

        for index, stage in enumerate(
            (
                TxTaskStage.QUEUED,
                TxTaskStage.PENDING_CHAIN,
                TxTaskStage.PENDING_CONFIRM,
            ),
            start=1,
        ):
            deposit = self._create_deposit(slot=slot, event_id=f"erc20:{index}")
            with signer_patch:
                existing = DepositSlot.schedule_collect_for_deposit(deposit.pk)
            existing.base_task.stage = stage
            existing.base_task.success = None
            existing.base_task.save(update_fields=["stage", "success", "updated_at"])

            with signer_patch, patch.object(EvmTxTask, "schedule") as schedule:
                task = DepositSlot.schedule_collect_for_deposit(deposit.pk)

            self.assertEqual(task.pk, existing.pk)
            schedule.assert_not_called()
            existing.base_task.stage = TxTaskStage.FINALIZED
            existing.base_task.success = False
            existing.base_task.save(update_fields=["stage", "success", "updated_at"])

    def test_schedule_collect_for_deposit_recreates_after_finalized_failed_task(self):
        slot = self._create_deposit_slot()
        deposit = self._create_deposit(slot=slot)
        signer_patch = self._patch_signer()

        with signer_patch:
            failed_task = DepositSlot.schedule_collect_for_deposit(deposit.pk)
        failed_task.base_task.stage = TxTaskStage.FINALIZED
        failed_task.base_task.success = False
        failed_task.base_task.save(update_fields=["stage", "success", "updated_at"])

        with signer_patch:
            new_task = DepositSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertNotEqual(new_task.pk, failed_task.pk)
        self.assertEqual(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.DepositSlotCollect
            ).count(),
            2,
        )

    def test_schedule_collect_for_deposit_rejects_vault_mismatch_without_task(self):
        slot = self._create_deposit_slot(
            vault_address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000bad"
            )
        )
        deposit = self._create_deposit(slot=slot)
        signer_patch = self._patch_signer()

        with signer_patch, self.assertRaisesRegex(RuntimeError, "Vault 地址不一致"):
            DepositSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.DepositSlotCollect
            ).exists()
        )

    def test_schedule_collect_for_deposit_skips_native_deposit(self):
        slot = self._create_deposit_slot()
        deposit = self._create_deposit(slot=slot, crypto=self.native, event_id="native:1")

        task = DepositSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertIsNone(task)
        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.DepositSlotCollect
            ).exists()
        )

    def _create_deposit_slot(self, *, vault_address: str | None = None) -> DepositSlot:
        if self.project.vault is None:
            self.project.vault = Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000f01"
            )
            self.project.save(update_fields=["vault"])
        return DepositSlot.objects.create(
            customer=self.customer,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            vault_address=vault_address or self.project.vault,
            salt=b"\x11" * 32,
        )

    def _create_deposit(
        self,
        *,
        slot: DepositSlot,
        crypto: Crypto | None = None,
        event_id: str = "erc20:1",
    ) -> Deposit:
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=1,
            hash="0x" + event_id[-1] * 64,
            event_id=event_id,
            crypto=crypto or self.token,
            from_address="0x0000000000000000000000000000000000000002",
            to_address=slot.address,
            value="1",
            amount=1,
            timestamp=1,
            datetime=timezone.now(),
            status=TransferStatus.CONFIRMED,
            type=TransferType.Deposit,
        )
        return Deposit.objects.create(
            customer=self.customer,
            transfer=transfer,
            status=DepositStatus.COMPLETED,
        )
