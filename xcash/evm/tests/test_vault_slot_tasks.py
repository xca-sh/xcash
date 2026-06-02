from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.test import TestCase
from django.utils import timezone
from eth_utils import keccak
from web3 import Web3

from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from core.models import SystemSettings
from core.models import SystemWallet
from currencies.models import ChainToken
from currencies.models import Crypto
from deposits.models import Deposit
from evm.choices import TxKind
from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.intents import DEFAULT_VAULT_SLOT_COLLECT_GAS
from evm.intents import DEFAULT_VAULT_SLOT_DEPLOY_GAS
from evm.intents import build_vault_slot_collect_intent
from evm.intents import build_vault_slot_deploy_intent
from evm.models import EvmTxTask
from evm.models import VaultSlot
from evm.models import VaultSlotCollectSchedule
from evm.models import VaultSlotUsage
from evm.tests._fixtures import make_evm_chain
from invoices.models import Invoice
from invoices.models import InvoiceBillingMode
from invoices.models import InvoiceStatus
from projects.models import Customer
from projects.models import Project


def _fake_address():
    return object()


def _fake_chain():
    return object()


def _selector(signature: str) -> str:
    return Web3.keccak(text=signature)[:4].hex()


def test_build_vault_slot_deploy_intent_encodes_factory_call():
    factory_address = "0x" + "a" * 40
    vault_address = "0x" + "b" * 40
    salt = bytes.fromhex("11" * 32)

    intent = build_vault_slot_deploy_intent(
        sender=_fake_address(),
        chain=_fake_chain(),
        factory_address=factory_address,
        vault_address=vault_address,
        salt=salt,
    )

    assert intent.tx_type == TxTaskType.VaultSlotDeploy
    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(factory_address)
    assert intent.value == 0
    assert intent.gas == DEFAULT_VAULT_SLOT_DEPLOY_GAS
    assert intent.data.startswith(f"0x{_selector('deployVaultSlot(address,bytes32)')}")
    assert Web3.to_checksum_address(vault_address)[2:].lower() in intent.data
    assert salt.hex() in intent.data


def test_build_vault_slot_deploy_intent_rejects_non_32_byte_salt():
    with pytest.raises(ValueError, match="salt must be 32 bytes"):
        build_vault_slot_deploy_intent(
            sender=_fake_address(),
            chain=_fake_chain(),
            factory_address="0x" + "a" * 40,
            vault_address="0x" + "b" * 40,
            salt=b"short",
        )


def test_build_vault_slot_collect_intent_encodes_slot_call():
    vault_slot_address = "0x" + "c" * 40
    token_address = "0x" + "d" * 40

    intent = build_vault_slot_collect_intent(
        sender=_fake_address(),
        chain=_fake_chain(),
        vault_slot_address=vault_slot_address,
        token_address=token_address,
    )

    assert intent.tx_type == TxTaskType.VaultSlotCollect
    assert intent.tx_kind == TxKind.CONTRACT_CALL
    assert intent.to == Web3.to_checksum_address(vault_slot_address)
    assert intent.value == 0
    assert intent.gas == DEFAULT_VAULT_SLOT_COLLECT_GAS
    assert intent.data.startswith(f"0x{_selector('collect(address)')}")
    assert Web3.to_checksum_address(token_address)[2:].lower() in intent.data


class VaultSlotAddressSchedulingTests(TestCase):
    def setUp(self):
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://vault-slot.local",
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
            uid="vault-slot-customer",
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
            coingecko_id="vault-slot-token",
        )
        self.token_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000e20"
        )
        ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=self.token_address,
            decimals=6,
        )
        deployed_patch = patch.object(
            VaultSlot,
            "_is_deployed_on_chain",
            return_value=False,
        )
        deployed_patch.start()
        self.addCleanup(deployed_patch.stop)

    def patch_address_derivation(self):
        # 地址派生已在 chains 内部闭环；这里直接桩掉 Wallet.get_address，
        # 让系统钱包与项目钱包各自返回测试预建的 Address，避免依赖真实派生结果。
        def fake_get_address(wallet_self, *args, **kwargs):
            if wallet_self.pk == self.system_wallet.pk:
                return self.system_sender
            return self.vault

        return patch.object(
            Wallet,
            "get_address",
            autospec=True,
            side_effect=fake_get_address,
        )

    def test_first_ensure_deposit_address_schedules_deploy_after_commit(self):
        self.project.vault = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000f01"
        )
        self.project.save(update_fields=["vault"])
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        slot = VaultSlot.objects.get(chain=self.chain, customer=self.customer)
        self.assertEqual(address, slot.address)
        self.assertEqual(schedule.call_count, 1)

        intent = schedule.call_args.args[0]
        self.assertEqual(intent.tx_type, TxTaskType.VaultSlotDeploy)
        self.assertEqual(intent.sender, self.system_sender)
        self.assertEqual(intent.to, XCASH_VAULT_SLOT_FACTORY_ADDRESS)
        self.assertIn(self.project.vault[2:].lower(), intent.data)

    def test_customer_vault_slot_records_project_usage_without_invoice_index(self):
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        slot = VaultSlot.objects.get(chain=self.chain, customer=self.customer)
        self.assertEqual(slot.project, self.project)
        self.assertEqual(slot.usage, VaultSlotUsage.DEPOSIT)
        self.assertIsNone(slot.invoice_index)

    def test_build_salt_dispatches_by_usage(self):
        deposit_salt = VaultSlot.build_salt(
            usage=VaultSlotUsage.DEPOSIT,
            customer=self.customer,
        )
        invoice_salt = VaultSlot.build_salt(
            usage=VaultSlotUsage.INVOICE,
            project_id=self.project.pk,
            invoice_index=3,
        )

        self.assertEqual(
            deposit_salt,
            keccak(
                b"xcash:vault-slot:deposit:"
                + str(self.project.pk).encode()
                + b":"
                + self.customer.uid.encode()
            ),
        )
        self.assertEqual(
            invoice_salt,
            keccak(
                b"xcash:vault-slot:invoice:"
                + str(self.project.pk).encode()
                + b":"
                + b"3"
            ),
        )

    def test_schedule_deploy_records_deploy_tx_task(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)

        slot.refresh_from_db()
        self.assertEqual(slot.deploy_tx_task, task)

    def test_schedule_deploy_uses_system_wallet_sender(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertEqual(task.sender, self.system_sender)

    def test_schedule_deploy_skips_when_slot_already_deployed_on_chain(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(VaultSlot, "_is_deployed_on_chain", return_value=True),
            patch.object(EvmTxTask, "schedule") as schedule,
        ):
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertIsNone(task)
        schedule.assert_not_called()
        slot.refresh_from_db()
        self.assertIsNone(slot.deploy_tx_task)

    def test_schedule_deploy_returns_recorded_unfinalized_deploy_tx_task(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            existing_task = VaultSlot.schedule_deploy(slot.pk)

        with address_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertEqual(task.pk, existing_task.pk)
        schedule.assert_not_called()

    def test_schedule_deploy_skips_successful_recorded_deploy_tx_task(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            existing_task = VaultSlot.schedule_deploy(slot.pk)
        existing_task.base_task.status = TxTaskStatus.CONFIRMED
        existing_task.base_task.save(update_fields=["status", "updated_at"])

        with address_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertEqual(task.pk, existing_task.pk)
        schedule.assert_not_called()

    def test_schedule_deploy_recreates_after_failed_recorded_deploy_tx_task(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            failed_task = VaultSlot.schedule_deploy(slot.pk)
        failed_task.base_task.status = TxTaskStatus.FAILED
        failed_task.base_task.save(update_fields=["status", "updated_at"])

        with address_patch:
            new_task = VaultSlot.schedule_deploy(slot.pk)

        slot.refresh_from_db()
        self.assertNotEqual(new_task.pk, failed_task.pk)
        self.assertEqual(slot.deploy_tx_task, new_task)

    def test_schedule_deploy_does_not_recreate_failed_task_when_slot_has_code(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            failed_task = VaultSlot.schedule_deploy(slot.pk)
        failed_task.base_task.status = TxTaskStatus.FAILED
        failed_task.base_task.save(update_fields=["status", "updated_at"])

        with (
            address_patch,
            patch.object(VaultSlot, "_is_deployed_on_chain", return_value=True),
            patch.object(EvmTxTask, "schedule") as schedule,
        ):
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertIsNone(task)
        slot.refresh_from_db()
        self.assertEqual(slot.deploy_tx_task, failed_task)
        schedule.assert_not_called()

    def test_ensure_deposit_address_rejects_project_without_vault(self):
        Project.objects.filter(pk=self.project.pk).update(vault=None)
        self.project.refresh_from_db()
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.assertRaisesRegex(RuntimeError, "VaultSlot Vault 地址未配置"),
        ):
            VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        schedule.assert_not_called()
        self.assertFalse(
            VaultSlot.objects.filter(chain=self.chain, customer=self.customer).exists()
        )

    def test_same_customer_can_reuse_vault_slot_address_across_evm_chains(self):
        second_chain = make_evm_chain(
            code=ChainCode.BSC,
            rpc="http://vault-slot-2.local",
        )
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            first_address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
            )
            second_address = VaultSlot.ensure_deposit_address(
                chain=second_chain,
                customer=self.customer,
            )

        self.assertEqual(second_address, first_address)
        self.assertEqual(
            VaultSlot.objects.filter(address=first_address).count(),
            2,
        )
        self.assertEqual(
            set(
                VaultSlot.objects.filter(address=first_address).values_list(
                    "chain_id",
                    flat=True,
                )
            ),
            {self.chain.pk, second_chain.pk},
        )

    def test_existing_slot_without_deploy_task_recovers_schedule(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        self.assertEqual(address, slot.address)
        self.assertEqual(schedule.call_count, 1)
        intent = schedule.call_args.args[0]
        self.assertEqual(intent.tx_type, TxTaskType.VaultSlotDeploy)
        self.assertEqual(intent.sender, self.system_sender)
        self.assertEqual(intent.to, XCASH_VAULT_SLOT_FACTORY_ADDRESS)

    def test_existing_slot_with_same_deploy_task_does_not_duplicate_schedule(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            existing_task = VaultSlot.schedule_deploy(slot.pk)

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        self.assertEqual(address, slot.address)
        self.assertEqual(
            EvmTxTask.objects.filter(pk=existing_task.pk).count(),
            1,
        )
        schedule.assert_not_called()

    def test_second_ensure_deposit_address_returns_existing_address_without_scheduling(
        self,
    ):
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            self.captureOnCommitCallbacks(execute=True),
        ):
            first_address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
            )

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            second_address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
            )

        self.assertEqual(second_address, first_address)
        schedule.assert_not_called()

    def test_integrity_error_lookup_path_does_not_schedule_duplicate_deploy(self):
        slot = VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            salt=b"\x11" * 32,
        )
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(
                VaultSlot.objects,
                "filter",
                return_value=SimpleNamespace(first=lambda: None),
            ),
            patch.object(
                VaultSlot.objects,
                "get_or_create",
                side_effect=IntegrityError("duplicate"),
            ),
            patch.object(VaultSlot.objects, "get", return_value=slot),
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        self.assertEqual(address, slot.address)
        schedule.assert_not_called()

    def test_integrity_error_lookup_failure_reraises_original_integrity_error(self):
        address_patch = self.patch_address_derivation()
        original_error = IntegrityError("duplicate")

        with (
            address_patch,
            patch.object(
                VaultSlot.objects,
                "filter",
                return_value=SimpleNamespace(first=lambda: None),
            ),
            patch.object(
                VaultSlot.objects,
                "get_or_create",
                side_effect=original_error,
            ),
            patch.object(
                VaultSlot.objects,
                "get",
                side_effect=VaultSlot.DoesNotExist,
            ),
            self.assertRaises(IntegrityError) as raised,
        ):
            VaultSlot.ensure_deposit_address(chain=self.chain, customer=self.customer)

        self.assertIs(raised.exception, original_error)

    def test_schedule_collect_for_deposit_uses_vault_sender_and_slot_target(self):
        SystemSettings.objects.create(vault_slot_collect_delay_minutes=30)
        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()
        before = timezone.now()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertEqual(schedule.chain, self.chain)
        self.assertEqual(schedule.vault_slot, slot)
        self.assertEqual(schedule.crypto, self.token)
        self.assertIsNone(schedule.tx_task)
        self.assertGreaterEqual(schedule.due_at, before + timedelta(minutes=30))
        self.assertLessEqual(schedule.due_at, timezone.now() + timedelta(minutes=30))
        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.VaultSlotCollect
            ).exists()
        )

    def test_schedule_collect_for_invoice_uses_contract_slot_and_token(self):
        slot = VaultSlot.objects.create(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a21"
            ),
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

        with self.patch_address_derivation():
            schedule = VaultSlot.schedule_collect_for_invoice(invoice.pk)

        self.assertEqual(schedule.chain, self.chain)
        self.assertEqual(schedule.vault_slot, slot)
        self.assertEqual(schedule.crypto, self.token)
        self.assertIsNone(schedule.tx_task)

    def test_schedule_collect_for_deposit_is_idempotent_for_pending_schedule(self):
        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            existing = VaultSlot.schedule_collect_for_deposit(deposit.pk)

        with address_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = VaultSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertEqual(task.pk, existing.pk)
        schedule.assert_not_called()

    def test_schedule_collect_for_deposit_reuses_pending_schedule_across_deposits(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        first_deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        second_deposit = self._create_deposit(slot=slot, tx_hash_suffix="2")
        with address_patch:
            existing = VaultSlot.schedule_collect_for_deposit(first_deposit.pk)

        with address_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = VaultSlot.schedule_collect_for_deposit(second_deposit.pk)

        self.assertEqual(task.pk, existing.pk)
        schedule.assert_not_called()
        self.assertEqual(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=slot,
                crypto=self.token,
                tx_task__isnull=True,
            ).count(),
            1,
        )

    def test_due_collect_schedule_creates_tx_task_and_binds_it(self):
        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with address_patch:
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 1)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.tx_task)
        self.assertEqual(schedule.tx_task.sender, self.vault)
        self.assertEqual(schedule.tx_task.chain, self.chain)
        self.assertEqual(schedule.tx_task.to, slot.address)
        self.assertEqual(schedule.tx_task.base_task.tx_type, TxTaskType.VaultSlotCollect)
        self.assertTrue(schedule.tx_task.data.startswith(f"0x{_selector('collect(address)')}"))
        self.assertIn(self.token_address[2:].lower(), schedule.tx_task.data)

    def test_schedule_collect_for_deposit_creates_new_schedule_after_task_bound(self):
        slot = self._create_vault_slot()
        first_deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        second_deposit = self._create_deposit(slot=slot, tx_hash_suffix="2")
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(first_deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with address_patch:
            VaultSlotCollectSchedule.execute_due()
            new_schedule = VaultSlot.schedule_collect_for_deposit(second_deposit.pk)

        self.assertNotEqual(new_schedule.pk, schedule.pk)
        self.assertIsNone(new_schedule.tx_task)
        self.assertEqual(
            VaultSlotCollectSchedule.objects.filter(
                chain=self.chain,
                vault_slot=slot,
                crypto=self.token,
            ).count(),
            2,
        )

    def test_schedule_collect_for_deposit_skips_native_deposit(self):
        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot, crypto=self.chain.native_coin)

        task = VaultSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertIsNone(task)
        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.VaultSlotCollect
            ).exists()
        )
        self.assertFalse(VaultSlotCollectSchedule.objects.exists())

    def _create_vault_slot(self) -> VaultSlot:
        if self.project.vault is None:
            self.project.vault = Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000f01"
            )
            self.project.save(update_fields=["vault"])
        return VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            salt=b"\x11" * 32,
        )

    def _create_deposit(
        self,
        *,
        slot: VaultSlot,
        crypto: Crypto | None = None,
        tx_hash_suffix: str = "1",
    ) -> Deposit:
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=1,
            block_hash="0x" + "aa" * 32,
            hash="0x" + tx_hash_suffix * 64,
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
        )
