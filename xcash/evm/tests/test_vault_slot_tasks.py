import threading
import time
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

import pytest
from django.db import IntegrityError
from django.db import connections
from django.db import transaction as db_transaction
from django.test import TestCase
from django.utils import timezone
from eth_utils import keccak
from web3 import Web3

from chains.constants import EVM_UNKNOWN_SOURCE_ADDRESS
from chains.constants import ChainCode
from chains.models import Address
from chains.models import AddressUsage
from chains.models import Chain
from chains.models import ChainType
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from chains.models import VaultSlotUsage
from chains.models import Wallet
from chains.vault_slots import create_collect_tx_task_for_slot
from core.models import SystemSettings
from core.models import SystemWallet
from currencies.models import Crypto
from currencies.models import CryptoOnChain
from currencies.models import Fiat
from deposits.models import Deposit
from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.intents import DEFAULT_VAULT_SLOT_COLLECT_GAS
from evm.intents import DEFAULT_VAULT_SLOT_DEPLOY_GAS
from evm.intents import build_vault_slot_collect_intent
from evm.intents import build_vault_slot_deploy_intent
from evm.internal_tx.vault_slot_deploy import XCASH_VAULT_SLOT_DEPLOYED_TOPIC0
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain
from invoices.models import Invoice
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


def test_build_vault_slot_collect_intent_encodes_direct_slot_call():
    slot_address = "0x" + "c" * 40
    token_address = "0x" + "d" * 40

    intent = build_vault_slot_collect_intent(
        sender=_fake_address(),
        chain=_fake_chain(),
        slot_address=slot_address,
        token_address=token_address,
    )

    assert intent.tx_type == TxTaskType.VaultSlotCollect
    assert intent.to == Web3.to_checksum_address(slot_address)
    assert intent.value == 0
    assert intent.gas == DEFAULT_VAULT_SLOT_COLLECT_GAS
    assert intent.data.startswith(f"0x{_selector('collect(address)')}")
    assert Web3.to_checksum_address(token_address)[2:].lower() in intent.data


@pytest.mark.django_db(transaction=True)
def test_concurrent_schedule_deploy_reuses_single_task_for_same_slot():
    chain = make_evm_chain(
        code=ChainCode.Ethereum,
        rpc="http://vault-slot.local",
    )
    project = Project.objects.create(name="Concurrent VaultSlot")
    project.evm_vault = Web3.to_checksum_address(
        "0x0000000000000000000000000000000000000f01"
    )
    project.save(update_fields=["evm_vault"])
    customer = Customer.objects.create(project=project, uid="same-slot")
    wallet = Wallet.objects.create()
    system_wallet = Wallet.objects.create()
    SystemWallet.objects.create(wallet=system_wallet)
    fallback_sender = Address.objects.create(
        wallet=wallet,
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
        address_index=0,
        address=Web3.to_checksum_address("0x0000000000000000000000000000000000000d01"),
    )
    system_sender = Address.objects.create(
        wallet=system_wallet,
        chain_type=ChainType.EVM,
        usage=AddressUsage.HotWallet,
        bip44_account=Wallet.get_bip44_account(AddressUsage.HotWallet),
        address_index=0,
        address=Web3.to_checksum_address("0x0000000000000000000000000000000000000d02"),
    )
    slot = VaultSlot.objects.create(
        chain=chain,
        project=project,
        usage=VaultSlotUsage.DEPOSIT,
        customer=customer,
        address=Web3.to_checksum_address("0x0000000000000000000000000000000000000a01"),
        salt=bytes.fromhex("11" * 32),
    )

    def fake_get_address(wallet_self, *args, **kwargs):
        if wallet_self.pk == system_wallet.pk:
            return system_sender
        return fallback_sender

    def slow_not_deployed(*args, **kwargs):
        # 让其余线程在首个事务更新 deploy_tx_task 前排队到 SELECT FOR UPDATE 上，
        # 覆盖真实压测里同一 slot 多个 on_commit 调度同时触发的窗口。
        time.sleep(0.05)
        return False

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    errors = []
    results = []
    lock = threading.Lock()

    def schedule():
        connections.close_all()
        try:
            barrier.wait(timeout=10)
            task = VaultSlot.schedule_deploy(slot.pk)
            with lock:
                results.append(None if task is None else task.pk)
        except Exception as exc:  # pragma: no cover - 失败时由断言展示异常
            with lock:
                errors.append(exc)
        finally:
            connections.close_all()

    with (
        patch("evm.vault_slots.is_deployed_on_chain", side_effect=slow_not_deployed),
        patch.object(
            Wallet, "get_address", autospec=True, side_effect=fake_get_address
        ),
    ):
        threads = [threading.Thread(target=schedule) for _ in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

    assert errors == []
    assert len(results) == thread_count
    assert len(set(results)) == 1
    assert (
        EvmTxTask.objects.filter(base_task__tx_type=TxTaskType.VaultSlotDeploy).count()
        == 1
    )
    slot.refresh_from_db()
    assert slot.deploy_tx_task_id == results[0]


class VaultSlotAddressSchedulingTests(TestCase):
    def setUp(self):
        self.chain = make_evm_chain(
            code=ChainCode.Ethereum,
            rpc="http://vault-slot.local",
        )
        self.wallet = Wallet.objects.create()
        self.project = Project.objects.create(
            name="Deposit Slot Project",
        )
        self.project.evm_vault = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000f01"
        )
        self.project.save(update_fields=["evm_vault"])
        Fiat.objects.get_or_create(code="USD")
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
        CryptoOnChain.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=self.token_address,
            decimals=6,
        )
        deployed_patch = patch(
            "evm.vault_slots.is_deployed_on_chain", return_value=False
        )
        deployed_patch.start()
        self.addCleanup(deployed_patch.stop)

    def patch_address_derivation(self):
        # 地址派生已在 chains 内部闭环；这里直接桩掉 Wallet.get_address。
        # 部署与归集都走系统热钱包，故系统钱包返回预建的 system_sender；
        # 其余钱包返回兜底 Address，避免依赖真实派生结果。
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

    def test_first_ensure_deposit_address_delays_deploy_for_token(self):
        self.project.evm_vault = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000f01"
        )
        self.project.save(update_fields=["evm_vault"])
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
            )

        slot = VaultSlot.objects.get(chain=self.chain, customer=self.customer)
        self.assertEqual(address, slot.address)
        schedule.assert_not_called()

    def test_first_ensure_deposit_address_schedules_deploy_for_native_after_commit(self):
        self.project.evm_vault = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000f01"
        )
        self.project.save(update_fields=["evm_vault"])
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.chain.native_coin,
            )

        slot = VaultSlot.objects.get(chain=self.chain, customer=self.customer)
        self.assertEqual(address, slot.address)
        self.assertEqual(schedule.call_count, 1)

        intent = schedule.call_args.args[0]
        self.assertEqual(intent.tx_type, TxTaskType.VaultSlotDeploy)
        self.assertEqual(intent.sender, self.system_sender)
        self.assertEqual(intent.to, XCASH_VAULT_SLOT_FACTORY_ADDRESS)
        self.assertIn(self.project.evm_vault[2:].lower(), intent.data)

    def test_customer_vault_slot_records_project_usage_without_invoice_index(self):
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
            )

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
                b"xcash:evm-vault-slot:deposit:"
                + str(self.project.pk).encode()
                + b":"
                + self.customer.uid.encode()
            ),
        )
        self.assertEqual(
            invoice_salt,
            keccak(
                b"xcash:evm-vault-slot:invoice:"
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

    def test_collect_token_address_maps_chain_native_coin_to_zero_address(self):
        from evm.vault_slots import NATIVE_COLLECT_TOKEN_ADDRESS
        from evm.vault_slots import collect_token_address

        self.assertEqual(
            collect_token_address(crypto=self.chain.native_coin, chain=self.chain),
            NATIVE_COLLECT_TOKEN_ADDRESS,
        )
        self.assertEqual(
            collect_token_address(crypto=self.token, chain=self.chain),
            self.token_address,
        )

    def test_create_collect_tx_task_for_native_coin_uses_zero_address_token(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)

        with self.patch_address_derivation():
            task = create_collect_tx_task_for_slot(
                chain=self.chain,
                crypto=self.chain.native_coin,
                slot=slot,
            )

        self.assertEqual(task.tx_type, TxTaskType.VaultSlotCollect)
        self.assertEqual(
            task.evm_task.data,
            f"0x{_selector('collect(address)')}{'0' * 64}",
        )

    def test_schedule_deploy_skips_when_slot_already_deployed_on_chain(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch("evm.vault_slots.is_deployed_on_chain", return_value=True),
            patch.object(EvmTxTask, "schedule") as schedule,
        ):
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertIsNone(task)
        schedule.assert_not_called()
        slot.refresh_from_db()
        self.assertIsNone(slot.deploy_tx_task)
        self.assertTrue(slot.is_deployed)

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
        existing_task.status = TxTaskStatus.SUCCEEDED
        existing_task.save(update_fields=["status", "updated_at"])
        VaultSlot.objects.filter(pk=slot.pk).update(is_deployed=True)

        with address_patch, patch.object(EvmTxTask, "schedule") as schedule:
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertIsNone(task)
        schedule.assert_not_called()

    def test_schedule_deploy_recreates_after_failed_recorded_deploy_tx_task(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with address_patch:
            failed_task = VaultSlot.schedule_deploy(slot.pk)
        failed_task.status = TxTaskStatus.FAILED
        failed_task.save(update_fields=["status", "updated_at"])

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
        failed_task.status = TxTaskStatus.FAILED
        failed_task.save(update_fields=["status", "updated_at"])

        with (
            address_patch,
            patch("evm.vault_slots.is_deployed_on_chain", return_value=True),
            patch.object(EvmTxTask, "schedule") as schedule,
        ):
            task = VaultSlot.schedule_deploy(slot.pk)

        self.assertIsNone(task)
        slot.refresh_from_db()
        self.assertEqual(slot.deploy_tx_task, failed_task)
        self.assertTrue(slot.is_deployed)
        schedule.assert_not_called()

    @patch("evm.internal_tx.processor.notify_vault_slot_deploy_gas_fee")
    def test_confirmed_deploy_marks_vault_slot_deployed(
        self,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()
        tx_hash = "0x" + "aa" * 32
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        self.chain.refresh_from_db()

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)

        evm_task = task.evm_task
        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value.eth.get_transaction.return_value = {
                "hash": tx_hash,
                "from": self.system_sender.address,
            }
            EvmTaskPoller.process_succeeded_receipt(
                evm_task=evm_task,
                tx_hash=tx_hash,
                receipt={"status": 1, "blockNumber": 1},
            )

        task.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.SUCCEEDED)
        self.assertTrue(slot.is_deployed)
        notify_gas_fee.assert_called_once()
        self.assertEqual(notify_gas_fee.call_args.kwargs["tx_task"].pk, task.pk)

    @patch("evm.internal_tx.processor.notify_vault_slot_deploy_gas_fee")
    def test_confirmed_deposit_deploy_initial_native_balance_creates_confirmed_deposit(
        self,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        slot = self._create_vault_slot()
        self._ensure_native_crypto_mapping()
        address_patch = self.patch_address_derivation()
        tx_hash = "0x" + "ba" * 32
        timestamp = int(timezone.now().timestamp())
        initial_value = Decimal(15 * 10**17)
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        self.chain.refresh_from_db()

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)

        receipt = self._deploy_receipt(
            slot=slot,
            initial_native_balance=int(initial_value),
        )
        evm_task = task.evm_task

        with (
            patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock,
            patch("chains.service.TransferService.enqueue_processing"),
            patch("chains.vault_slot_balances.refresh_vault_slot_balance_for_transfer"),
            patch("deposits.service.WebhookService.create_event"),
            patch("deposits.service.screen_deposit_aml.delay"),
            patch("deposits.service.send_saas_callback"),
        ):
            w3_mock.return_value.eth.get_transaction.return_value = {
                "hash": tx_hash,
                "from": self.system_sender.address,
            }
            w3_mock.return_value.eth.get_block.return_value = {
                "timestamp": timestamp,
            }
            processed = EvmTaskPoller.process_succeeded_receipt(
                evm_task=evm_task,
                tx_hash=tx_hash,
                receipt=receipt,
            )

        self.assertTrue(processed)
        task.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.SUCCEEDED)
        self.assertTrue(slot.is_deployed)
        self.assertTrue(slot.has_received)

        transfer = Transfer.objects.get(hash=tx_hash)
        self.assertEqual(transfer.event_index, 1)
        self.assertEqual(transfer.from_address, EVM_UNKNOWN_SOURCE_ADDRESS)
        self.assertEqual(transfer.to_address, slot.address)
        self.assertEqual(transfer.crypto, self.chain.native_coin)
        self.assertEqual(transfer.value, initial_value)
        self.assertEqual(transfer.amount, Decimal("1.5"))
        self.assertEqual(transfer.type, TransferType.Deposit)
        self.assertEqual(transfer.status, TransferStatus.CONFIRMED)
        self.assertIsNotNone(transfer.processed_at)

        deposit = Deposit.objects.get(transfer=transfer)
        self.assertEqual(deposit.customer, self.customer)
        notify_gas_fee.assert_called_once()

    @patch("evm.internal_tx.processor.notify_vault_slot_deploy_gas_fee")
    def test_confirmed_invoice_deploy_initial_native_balance_confirms_invoice(
        self,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        native_crypto = self.chain.native_coin
        self._ensure_native_crypto_mapping()
        slot = VaultSlot.objects.create(
            project=self.project,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=0,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a12"
            ),
            salt=b"\x12" * 32,
        )
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="invoice-initial-native",
            title="Invoice initial native",
            currency_id="USD",
            amount=Decimal("1"),
            methods={native_crypto.symbol: [self.chain.code]},
            crypto=native_crypto,
            chain=self.chain,
            pay_address=slot.address,
            pay_amount=Decimal("1"),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        address_patch = self.patch_address_derivation()
        tx_hash = "0x" + "bb" * 32
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        self.chain.refresh_from_db()

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)
        task.refresh_from_db()

        receipt = self._deploy_receipt(
            slot=slot,
            initial_native_balance=10**18,
        )
        evm_task = task.evm_task

        with (
            patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock,
            patch("chains.service.TransferService.enqueue_processing"),
            patch("chains.vault_slot_balances.refresh_vault_slot_balance_for_transfer"),
            patch(
                "chains.models.VaultSlot.schedule_collect_for_invoice"
            ) as schedule_collect,
            patch("invoices.service.WebhookService.create_event"),
            patch("invoices.service.screen_invoice_aml.delay"),
            patch("invoices.service.send_saas_callback"),
        ):
            w3_mock.return_value.eth.get_transaction.return_value = {
                "hash": tx_hash,
                "from": self.system_sender.address,
            }
            processed = EvmTaskPoller.process_succeeded_receipt(
                evm_task=evm_task,
                tx_hash=tx_hash,
                receipt=receipt,
            )

        self.assertTrue(processed)
        task.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.SUCCEEDED)
        self.assertTrue(slot.is_deployed)
        self.assertTrue(slot.has_received)

        transfer = Transfer.objects.get(hash=tx_hash)
        self.assertEqual(transfer.event_index, 1)
        self.assertEqual(transfer.from_address, EVM_UNKNOWN_SOURCE_ADDRESS)
        self.assertEqual(transfer.to_address, slot.address)
        self.assertEqual(transfer.crypto, native_crypto)
        self.assertEqual(transfer.value, Decimal(10**18))
        self.assertEqual(transfer.amount, Decimal("1"))
        self.assertEqual(transfer.datetime, task.created_at)
        self.assertEqual(transfer.timestamp, int(task.created_at.timestamp()))
        self.assertEqual(transfer.type, TransferType.Invoice)
        self.assertEqual(transfer.status, TransferStatus.CONFIRMED)
        self.assertIsNotNone(transfer.processed_at)

        invoice.refresh_from_db()
        self.assertEqual(invoice.transfer, transfer)
        self.assertEqual(invoice.status, InvoiceStatus.COMPLETED)
        schedule_collect.assert_called_once_with(invoice.pk)
        notify_gas_fee.assert_called_once()

    @patch("evm.internal_tx.processor.notify_vault_slot_collect_gas_fee")
    @patch("evm.internal_tx.processor.refresh_vault_slot_balance_for_collect_task")
    def test_confirmed_collect_finalizes_task_without_transfer(
        self,
        refresh_balance,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        slot = self._create_vault_slot()
        tx_hash = "0x" + "ad" * 32
        value = 1_230_000
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        self.chain.refresh_from_db()
        self._mark_vault_slot_deployed(slot)

        with self.patch_address_derivation():
            task = create_collect_tx_task_for_slot(
                chain=self.chain,
                crypto=self.token,
                slot=slot,
            )
        schedule = VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.token,
            due_at=timezone.now(),
            tx_task=task,
        )
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)

        def address_topic(address: str) -> str:
            return "0x" + "0" * 24 + Web3.to_checksum_address(address)[2:].lower()

        receipt = {
            "status": 1,
            "blockNumber": 1,
            "logs": [
                {
                    "address": slot.address,
                    "topics": [
                        Web3.keccak(text="XcashCollected(address,uint256)"),
                        address_topic(self.token_address),
                    ],
                    "data": hex(value),
                    "logIndex": 5,
                },
                {
                    "address": self.token_address,
                    "topics": [
                        Web3.keccak(text="Transfer(address,address,uint256)"),
                        address_topic(slot.address),
                        address_topic(self.project.evm_vault),
                    ],
                    "data": hex(value),
                    "logIndex": 6,
                },
            ],
        }
        transfer_count = Transfer.objects.count()
        evm_task = task.evm_task

        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value.eth.get_transaction.return_value = {
                "hash": tx_hash,
                "from": self.system_sender.address,
            }
            processed = EvmTaskPoller.process_succeeded_receipt(
                evm_task=evm_task,
                tx_hash=tx_hash,
                receipt=receipt,
            )

        self.assertTrue(processed)
        task.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.SUCCEEDED)
        self.assertEqual(Transfer.objects.count(), transfer_count)
        refresh_balance.assert_called_once()
        self.assertEqual(refresh_balance.call_args.args[0].pk, task.pk)
        notify_gas_fee.assert_called_once()
        self.assertEqual(notify_gas_fee.call_args.kwargs["tx_task"].pk, task.pk)
        schedule.refresh_from_db()
        self.assertEqual(schedule.tx_task_id, task.pk)

    @patch("evm.internal_tx.processor.notify_vault_slot_collect_gas_fee")
    @patch("evm.internal_tx.processor.refresh_vault_slot_balance_for_collect_task")
    def test_successful_empty_collect_finalizes_task_without_balance_refresh(
        self,
        refresh_balance,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        slot = self._create_vault_slot()
        tx_hash = "0x" + "ae" * 32
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        self.chain.refresh_from_db()
        self._mark_vault_slot_deployed(slot)

        with self.patch_address_derivation():
            task = create_collect_tx_task_for_slot(
                chain=self.chain,
                crypto=self.token,
                slot=slot,
            )
        VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.token,
            due_at=timezone.now(),
            tx_task=task,
        )
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)

        evm_task = task.evm_task
        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value.eth.get_transaction.return_value = {
                "hash": tx_hash,
                "from": self.system_sender.address,
            }
            processed = EvmTaskPoller.process_succeeded_receipt(
                evm_task=evm_task,
                tx_hash=tx_hash,
                receipt={"status": 1, "blockNumber": 1, "logs": []},
            )

        self.assertTrue(processed)
        task.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.SUCCEEDED)
        refresh_balance.assert_not_called()
        notify_gas_fee.assert_called_once()
        self.assertEqual(notify_gas_fee.call_args.kwargs["tx_task"].pk, task.pk)

    @patch("evm.internal_tx.processor.notify_vault_slot_collect_gas_fee")
    @patch("evm.internal_tx.processor.refresh_vault_slot_balance_for_collect_task")
    def test_successful_empty_native_collect_finalizes_task_without_balance_refresh(
        self,
        refresh_balance,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        slot = self._create_vault_slot()
        tx_hash = "0x" + "af" * 32
        Chain.objects.filter(pk=self.chain.pk).update(latest_block_number=100)
        self.chain.refresh_from_db()
        self._mark_vault_slot_deployed(slot)

        with self.patch_address_derivation():
            task = create_collect_tx_task_for_slot(
                chain=self.chain,
                crypto=self.chain.native_coin,
                slot=slot,
            )
        VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.chain.native_coin,
            due_at=timezone.now(),
            tx_task=task,
        )
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)

        evm_task = task.evm_task
        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value.eth.get_transaction.return_value = {
                "hash": tx_hash,
                "from": self.system_sender.address,
            }
            processed = EvmTaskPoller.process_succeeded_receipt(
                evm_task=evm_task,
                tx_hash=tx_hash,
                receipt={"status": 1, "blockNumber": 1, "logs": []},
            )

        self.assertTrue(processed)
        task.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.SUCCEEDED)
        refresh_balance.assert_not_called()
        notify_gas_fee.assert_called_once()
        self.assertEqual(notify_gas_fee.call_args.kwargs["tx_task"].pk, task.pk)

    @patch("evm.internal_tx.processor.notify_vault_slot_deploy_gas_fee")
    def test_failed_deploy_marks_deployed_when_slot_has_external_code(
        self,
        notify_gas_fee,
    ):
        from evm.poller import EvmTaskPoller

        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()
        tx_hash = "0x" + "ac" * 32

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)
        task.append_tx_hash(tx_hash)
        TxTask.objects.filter(pk=task.pk).update(status=TxTaskStatus.SUBMITTED)

        with patch("evm.vault_slots.is_deployed_on_chain", return_value=True):
            EvmTaskPoller.finalize_failed_task(evm_task=task.evm_task)

        task.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(task.status, TxTaskStatus.FAILED)
        self.assertTrue(slot.is_deployed)
        notify_gas_fee.assert_not_called()

    @patch("evm.saas_gas_billing.send_saas_callback")
    def test_confirmed_deploy_notifies_saas_gas_fee(self, send_callback_mock):
        from evm.saas_gas_billing import notify_vault_slot_deploy_gas_fee

        slot = self._create_vault_slot()
        native_crypto = self.chain.native_coin
        native_crypto.prices = {"USD": "2000"}
        native_crypto.save(update_fields=["prices"])
        CryptoOnChain.objects.update_or_create(
            crypto=native_crypto,
            chain=self.chain,
            defaults={"address": "", "decimals": 18},
        )
        tx_hash = "0x" + "ab" * 32
        address_patch = self.patch_address_derivation()
        w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(
                    return_value={
                        "gasUsed": 21000,
                        "effectiveGasPrice": 1_000_000_000,
                    }
                ),
                get_transaction=Mock(return_value={"gasPrice": 1_000_000_000}),
            )
        )

        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)
        task.tx_hash = tx_hash
        task.save(update_fields=["tx_hash", "updated_at"])

        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value = w3
            notify_vault_slot_deploy_gas_fee(tx_task=task)

        send_callback_mock.assert_called_once()
        callback = send_callback_mock.call_args.args[0]
        self.assertEqual(callback.event, "gas_fee.vault_slot_deploy.confirmed")
        self.assertEqual(callback.appid, self.project.appid)
        self.assertEqual(callback.uid, self.customer.uid)
        self.assertEqual(callback.currency, "USDT")
        self.assertIsNone(callback.worth)
        tx_detail = callback.tx_detail
        self.assertEqual(tx_detail["gas_cost"], "0.042")
        self.assertEqual(tx_detail["tx_hash"], tx_hash)
        self.assertEqual(tx_detail["chain"], "Ethereum")
        self.assertEqual(tx_detail["gas_used"], 21000)
        self.assertEqual(tx_detail["gas_price"], 1_000_000_000)
        self.assertEqual(tx_detail["native_price"], "2000")

    @patch("evm.saas_gas_billing.retry_vault_slot_deploy_gas_fee.delay")
    @patch("evm.saas_gas_billing._build_tx_detail", side_effect=RuntimeError("rpc down"))
    def test_deploy_gas_fee_build_failure_schedules_retry(
        self,
        _build_tx_detail_mock,
        retry_delay,
    ):
        from evm.saas_gas_billing import notify_vault_slot_deploy_gas_fee

        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()
        with address_patch:
            task = VaultSlot.schedule_deploy(slot.pk)
        task.tx_hash = "0x" + "ef" * 32
        task.save(update_fields=["tx_hash", "updated_at"])

        with self.captureOnCommitCallbacks(execute=True):
            notify_vault_slot_deploy_gas_fee(tx_task=task)

        retry_delay.assert_called_once_with(task.pk)

    def test_ensure_deposit_address_rejects_project_without_vault(self):
        Project.objects.filter(pk=self.project.pk).update(evm_vault=None)
        self.project.refresh_from_db()
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.assertRaisesRegex(RuntimeError, "智能合约收款归集地址未配置"),
        ):
            VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
            )

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
                crypto=self.token,
            )
            second_address = VaultSlot.ensure_deposit_address(
                chain=second_chain,
                customer=self.customer,
                crypto=self.token,
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

    def test_existing_slot_without_deploy_task_recovers_schedule_for_native(self):
        slot = self._create_vault_slot()
        address_patch = self.patch_address_derivation()

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.chain.native_coin,
            )

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
            address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
            )

        self.assertEqual(address, slot.address)
        self.assertEqual(
            EvmTxTask.objects.filter(base_task=existing_task).count(),
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
                crypto=self.token,
            )

        with (
            address_patch,
            patch.object(EvmTxTask, "schedule") as schedule,
            self.captureOnCommitCallbacks(execute=True),
        ):
            second_address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
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
            address = VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
            )

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
            VaultSlot.ensure_deposit_address(
                chain=self.chain,
                customer=self.customer,
                crypto=self.token,
            )

        self.assertIs(raised.exception, original_error)

    def test_schedule_collect_for_deposit_uses_vault_sender_and_slot_target(self):
        SystemSettings.objects.create(evm_vault_slot_collect_delay_minutes=30)
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

    def test_ensure_pending_integrity_conflict_keeps_outer_transaction_usable(self):
        slot = self._create_vault_slot()
        existing = VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.token,
            due_at=timezone.now(),
        )

        with db_transaction.atomic():
            with patch.object(
                VaultSlotCollectSchedule.objects,
                "filter",
                return_value=SimpleNamespace(first=lambda: None),
            ):
                schedule = VaultSlotCollectSchedule.ensure_pending(
                    chain=self.chain,
                    vault_slot=slot,
                    crypto=self.token,
                )

            Crypto.objects.create(
                name="Post Integrity Sentinel",
                symbol="PIS",
                coingecko_id="post-integrity-sentinel",
            )

        self.assertEqual(schedule.pk, existing.pk)
        self.assertTrue(Crypto.objects.filter(symbol="PIS").exists())

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
            currency_id="USD",
            amount="10.00000000",
            methods={self.token.symbol: [self.chain.code]},
            crypto=self.token,
            chain=self.chain,
            pay_amount="10.00000000",
            pay_address=slot.address,
            status=InvoiceStatus.COMPLETED,
            expires_at=timezone.now(),
        )

        with self.patch_address_derivation():
            schedule = VaultSlot.schedule_collect_for_invoice(invoice.pk)

        self.assertEqual(schedule.chain, self.chain)
        self.assertEqual(schedule.vault_slot, slot)
        self.assertEqual(schedule.crypto, self.token)
        self.assertIsNone(schedule.tx_task)

    def test_schedule_collect_for_invoice_uses_contract_slot_and_native_coin(self):
        slot = VaultSlot.objects.create(
            project=self.project,
            chain=self.chain,
            usage=VaultSlotUsage.INVOICE,
            invoice_index=1,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a22"
            ),
            salt=b"\x22" * 32,
        )
        invoice = Invoice.objects.create(
            project=self.project,
            out_no="invoice-slot-native-collect",
            title="Invoice slot native collect",
            currency_id="USD",
            amount="10.00000000",
            methods={self.chain.native_coin.symbol: [self.chain.code]},
            crypto=self.chain.native_coin,
            chain=self.chain,
            pay_amount="10.00000000",
            pay_address=slot.address,
            status=InvoiceStatus.COMPLETED,
            expires_at=timezone.now(),
        )

        with self.patch_address_derivation():
            schedule = VaultSlot.schedule_collect_for_invoice(invoice.pk)

        self.assertEqual(schedule.chain, self.chain)
        self.assertEqual(schedule.vault_slot, slot)
        self.assertEqual(schedule.crypto, self.chain.native_coin)
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
        self._mark_vault_slot_deployed(slot)
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 1)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.tx_task)
        # 归集与部署一样统一用系统热钱包作为 sender（仅付 gas，资金去向由合约写死的 vault 决定）。
        self.assertEqual(schedule.tx_task.sender, self.system_sender)
        self.assertEqual(schedule.tx_task.chain, self.chain)
        self.assertEqual(schedule.tx_task.evm_task.to, slot.address)
        self.assertEqual(schedule.tx_task.tx_type, TxTaskType.VaultSlotCollect)
        self.assertTrue(
            schedule.tx_task.evm_task.data.startswith(
                f"0x{_selector('collect(address)')}"
            )
        )
        self.assertIn(self.token_address[2:].lower(), schedule.tx_task.evm_task.data)

    def test_due_collect_schedule_defers_and_schedules_deploy_for_undeployed_slot(self):
        # 部署是归集的前置状态:未部署槽位到期时不建归集任务,
        # 而是转入部署流并把归集计划退避到部署确认之后。
        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with address_patch:
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 0)
        schedule.refresh_from_db()
        self.assertIsNone(schedule.tx_task)
        self.assertGreater(schedule.due_at, timezone.now())
        slot.refresh_from_db()
        self.assertIsNotNone(slot.deploy_tx_task)
        self.assertEqual(slot.deploy_tx_task.tx_type, TxTaskType.VaultSlotDeploy)
        self.assertEqual(
            slot.deploy_tx_task.evm_task.to,
            Web3.to_checksum_address(XCASH_VAULT_SLOT_FACTORY_ADDRESS),
        )
        self.assertTrue(
            slot.deploy_tx_task.evm_task.data.startswith(
                f"0x{_selector('deployVaultSlot(address,bytes32)')}"
            )
        )

    def test_deploy_confirmation_expedites_deferred_collect_schedule(self):
        # 部署确认翻转 is_deployed 时,把被退避的 pending 归集计划拨回当前时间,
        # 让下一轮 execute_due 立即归集,而不是干等最长 10 分钟退避。
        from chains.vault_slots import mark_deployed_by_task

        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() + timedelta(minutes=10)
        schedule.save(update_fields=["due_at", "updated_at"])

        with address_patch:
            deploy_task = VaultSlot.schedule_deploy(slot.pk)
        self.assertIsNotNone(deploy_task)

        marked = mark_deployed_by_task(deploy_task)

        self.assertTrue(marked)
        slot.refresh_from_db()
        self.assertTrue(slot.is_deployed)
        schedule.refresh_from_db()
        self.assertLessEqual(schedule.due_at, timezone.now())

    def test_deploy_confirmation_schedules_native_collect_when_balance_remains(self):
        from chains.vault_slots import mark_deployed

        slot = self._create_vault_slot()

        with (
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=Decimal("1")),
            ) as refresh_balance,
            self.captureOnCommitCallbacks(execute=True),
        ):
            marked = mark_deployed(slot)

        self.assertTrue(marked)
        refresh_balance.assert_called_once()
        schedule = VaultSlotCollectSchedule.objects.get(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.chain.native_coin,
            tx_task__isnull=True,
        )
        self.assertLessEqual(schedule.due_at, timezone.now())

    def test_due_native_collect_schedule_creates_zero_address_collect_task(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        schedule = VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.chain.native_coin,
            due_at=timezone.now() - timedelta(seconds=1),
        )

        with (
            self.patch_address_derivation(),
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=Decimal("1")),
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 1)
        schedule.refresh_from_db()
        self.assertIsNotNone(schedule.tx_task)
        self.assertEqual(
            schedule.tx_task.evm_task.data,
            f"0x{_selector('collect(address)')}{'0' * 64}",
        )

    def test_due_collect_schedule_deletes_pending_schedule_when_balance_is_zero(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=0),
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 0)
        self.assertFalse(VaultSlotCollectSchedule.objects.filter(pk=schedule.pk).exists())
        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.VaultSlotCollect
            ).exists()
        )

    def test_due_collect_schedule_keeps_pending_when_balance_refresh_fails(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        deposit = self._create_deposit(slot=slot)
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=None,
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 0)
        schedule.refresh_from_db()
        self.assertIsNone(schedule.tx_task)
        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.VaultSlotCollect
            ).exists()
        )

    def test_schedule_collect_for_deposit_creates_new_schedule_after_task_bound(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        first_deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        second_deposit = self._create_deposit(slot=slot, tx_hash_suffix="2")
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(first_deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ),
        ):
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

    def test_two_due_collect_schedules_same_slot_get_independent_tasks(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        first_deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        second_deposit = self._create_deposit(slot=slot, tx_hash_suffix="2")
        address_patch = self.patch_address_derivation()

        with address_patch:
            first = VaultSlot.schedule_collect_for_deposit(first_deposit.pk)
        first.due_at = timezone.now() - timedelta(seconds=1)
        first.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ),
        ):
            VaultSlotCollectSchedule.execute_due()
            first.refresh_from_db()
            self.assertIsNotNone(first.tx_task_id)
            self.assertEqual(first.tx_task.status, TxTaskStatus.QUEUED)

            second = VaultSlot.schedule_collect_for_deposit(second_deposit.pk)
        second.due_at = timezone.now() - timedelta(seconds=1)
        second.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 1)
        second.refresh_from_db()
        self.assertIsNotNone(second.tx_task_id)
        self.assertNotEqual(first.tx_task_id, second.tx_task_id)

    def test_due_collect_schedule_error_does_not_abort_batch(self):
        first_slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(first_slot)
        second_customer = Customer.objects.create(
            project=self.project,
            uid="vault-slot-customer-2",
        )
        second_slot = VaultSlot.objects.create(
            customer=second_customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a22"
            ),
            salt=b"\x22" * 32,
            is_deployed=True,
        )
        first_deposit = self._create_deposit(slot=first_slot, tx_hash_suffix="1")
        second_deposit = self._create_deposit(
            slot=second_slot,
            customer=second_customer,
            tx_hash_suffix="2",
        )
        address_patch = self.patch_address_derivation()

        with address_patch:
            first = VaultSlot.schedule_collect_for_deposit(first_deposit.pk)
            second = VaultSlot.schedule_collect_for_deposit(second_deposit.pk)
        for schedule in (first, second):
            schedule.due_at = timezone.now() - timedelta(seconds=1)
            schedule.save(update_fields=["due_at", "updated_at"])

        original_create_tx_task = VaultSlotCollectSchedule.create_tx_task

        def fail_first_schedule(schedule):
            if schedule.pk == first.pk:
                raise RuntimeError("token disabled")
            return original_create_tx_task(schedule)

        with (
            address_patch,
            patch.object(
                VaultSlotCollectSchedule,
                "create_tx_task",
                autospec=True,
                side_effect=fail_first_schedule,
            ),
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 1)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertIsNone(first.tx_task_id)
        self.assertIsNotNone(second.tx_task_id)
        # 失败计划必须退避，否则恒占 execute_due 批次头部。
        self.assertGreater(first.due_at, timezone.now())

    def test_due_collect_schedule_failure_backoff_frees_followup_batches(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch.object(
                VaultSlotCollectSchedule,
                "create_tx_task",
                autospec=True,
                side_effect=RuntimeError("token disabled"),
            ) as create_tx_task_mock,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=SimpleNamespace(value=1),
            ),
        ):
            self.assertEqual(VaultSlotCollectSchedule.execute_due(), 0)
            self.assertEqual(create_tx_task_mock.call_count, 1)
            # 退避期内的后续批次不应再捞到该计划，把名额让给其它计划。
            self.assertEqual(VaultSlotCollectSchedule.execute_due(), 0)
            self.assertEqual(create_tx_task_mock.call_count, 1)

        schedule.refresh_from_db()
        self.assertIsNone(schedule.tx_task_id)
        self.assertGreater(schedule.due_at, timezone.now())

    def test_due_collect_schedule_defers_when_balance_refresh_fails(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slot_balances.refresh_vault_slot_balance_safely",
                return_value=None,
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 0)
        schedule.refresh_from_db()
        self.assertIsNone(schedule.tx_task_id)
        self.assertGreater(schedule.due_at, timezone.now())

    def test_due_collect_schedule_defers_when_collect_not_creatable(self):
        slot = self._create_vault_slot()
        self._mark_vault_slot_deployed(slot)
        deposit = self._create_deposit(slot=slot, tx_hash_suffix="1")
        address_patch = self.patch_address_derivation()

        with address_patch:
            schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)
        schedule.due_at = timezone.now() - timedelta(seconds=1)
        schedule.save(update_fields=["due_at", "updated_at"])

        with (
            address_patch,
            patch(
                "chains.vault_slots.can_create_collect_tx_task",
                return_value=False,
            ),
        ):
            created_count = VaultSlotCollectSchedule.execute_due()

        self.assertEqual(created_count, 0)
        schedule.refresh_from_db()
        self.assertIsNone(schedule.tx_task_id)
        self.assertGreater(schedule.due_at, timezone.now())

    def test_schedule_collect_for_deposit_schedules_native_deposit(self):
        slot = self._create_vault_slot()
        deposit = self._create_deposit(slot=slot, crypto=self.chain.native_coin)

        schedule = VaultSlot.schedule_collect_for_deposit(deposit.pk)

        self.assertEqual(schedule.chain, self.chain)
        self.assertEqual(schedule.vault_slot, slot)
        self.assertEqual(schedule.crypto, self.chain.native_coin)
        self.assertIsNone(schedule.tx_task)
        self.assertFalse(
            EvmTxTask.objects.filter(
                base_task__tx_type=TxTaskType.VaultSlotCollect
            ).exists()
        )

    def test_collect_matcher_decodes_slot_collect_call(self):
        from evm.contracts_codec import predict_xcash_vault_slot_address
        from evm.internal_tx.vault_slot_collect import vault_slot_collect_matcher

        slot = self._create_vault_slot()
        slot.address = predict_xcash_vault_slot_address(
            vault=self.project.evm_vault,
            salt=bytes(slot.salt),
        )
        slot.save(update_fields=["address"])
        intent = build_vault_slot_collect_intent(
            sender=self.system_sender,
            chain=self.chain,
            slot_address=slot.address,
            token_address=self.token_address,
        )
        evm_task = EvmTxTask.schedule(intent)
        value = 1_230_000

        def address_topic(address: str) -> str:
            return "0x" + "0" * 24 + Web3.to_checksum_address(address)[2:].lower()

        receipt = {
            "logs": [
                {
                    "address": slot.address,
                    "topics": [
                        Web3.keccak(text="XcashCollected(address,uint256)"),
                        address_topic(self.token_address),
                    ],
                    "data": hex(value),
                    "logIndex": 5,
                },
                {
                    "address": self.token_address,
                    "topics": [
                        Web3.keccak(text="Transfer(address,address,uint256)"),
                        address_topic(slot.address),
                        address_topic(self.project.evm_vault),
                    ],
                    "data": hex(value),
                    "logIndex": 6,
                },
            ],
        }

        fact = vault_slot_collect_matcher(
            chain=self.chain,
            tx_task=evm_task.base_task,
            receipt=receipt,
        )

        self.assertIsNotNone(fact)
        self.assertEqual(fact.from_address, slot.address)
        self.assertEqual(fact.to_address, self.project.evm_vault)
        self.assertEqual(fact.crypto, self.token)
        self.assertEqual(fact.value, Decimal(value))
        self.assertEqual(fact.amount, Decimal("1.23"))

    @patch("evm.saas_gas_billing.send_saas_callback")
    def test_confirmed_collect_task_notifies_saas_gas_fee(self, send_callback_mock):
        from evm.saas_gas_billing import notify_vault_slot_collect_gas_fee

        slot = self._create_vault_slot()
        native_crypto = self.chain.native_coin
        native_crypto.prices = {"USD": "2000"}
        native_crypto.save(update_fields=["prices"])
        CryptoOnChain.objects.update_or_create(
            crypto=native_crypto,
            chain=self.chain,
            defaults={"address": "", "decimals": 18},
        )
        tx_hash = "0x" + "cd" * 32
        self._mark_vault_slot_deployed(slot)
        with self.patch_address_derivation():
            task = create_collect_tx_task_for_slot(
                chain=self.chain,
                crypto=self.token,
                slot=slot,
            )
        VaultSlotCollectSchedule.objects.create(
            chain=self.chain,
            vault_slot=slot,
            crypto=self.token,
            due_at=timezone.now(),
            tx_task=task,
        )
        task.tx_hash = tx_hash
        task.save(update_fields=["tx_hash", "updated_at"])
        w3 = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(
                    return_value={
                        "gasUsed": 50000,
                        "effectiveGasPrice": 2_000_000_000,
                    }
                ),
                get_transaction=Mock(return_value={"gasPrice": 2_000_000_000}),
            )
        )

        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value = w3
            notify_vault_slot_collect_gas_fee(tx_task=task)

        send_callback_mock.assert_called_once()
        callback = send_callback_mock.call_args.args[0]
        self.assertEqual(callback.event, "gas_fee.vault_slot_collect.confirmed")
        self.assertEqual(callback.appid, self.project.appid)
        self.assertEqual(callback.uid, self.customer.uid)
        self.assertEqual(callback.currency, "USDT")
        self.assertIsNone(callback.worth)
        tx_detail = callback.tx_detail
        self.assertEqual(tx_detail["gas_cost"], "0.2")
        self.assertEqual(tx_detail["tx_hash"], tx_hash)
        self.assertEqual(tx_detail["chain"], "Ethereum")
        self.assertEqual(tx_detail["gas_used"], 50000)
        self.assertEqual(tx_detail["gas_price"], 2_000_000_000)
        self.assertEqual(tx_detail["native_price"], "2000")

    def _create_vault_slot(self) -> VaultSlot:
        if self.project.evm_vault is None:
            self.project.evm_vault = Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000f01"
            )
            self.project.save(update_fields=["evm_vault"])
        return VaultSlot.objects.create(
            customer=self.customer,
            usage=VaultSlotUsage.DEPOSIT,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a11"
            ),
            salt=b"\x11" * 32,
        )

    def _ensure_native_crypto_mapping(self) -> None:
        CryptoOnChain.objects.update_or_create(
            crypto=self.chain.native_coin,
            chain=self.chain,
            defaults={
                "address": "",
                "decimals": 18,
                "active": True,
            },
        )

    def _deploy_receipt(
        self,
        *,
        slot: VaultSlot,
        initial_native_balance: int,
    ) -> dict:
        return {
            "status": 1,
            "blockNumber": 1,
            "blockHash": "0x" + "cd" * 32,
            "logs": [
                {
                    "address": Web3.to_checksum_address(
                        "0x0000000000000000000000000000000000000bad"
                    ),
                    "topics": [],
                    "data": "0x",
                    "logIndex": 10,
                },
                {
                    "address": self.chain.vault_slot_contract_addresses().factory,
                    "topics": [
                        XCASH_VAULT_SLOT_DEPLOYED_TOPIC0,
                        self._address_topic(slot.address),
                        self._address_topic(self.project.evm_vault),
                        "0x" + bytes(slot.salt).hex(),
                    ],
                    "data": "0x" + initial_native_balance.to_bytes(32, "big").hex(),
                    "logIndex": 11,
                },
            ],
        }

    @staticmethod
    def _address_topic(address: str) -> str:
        return "0x" + "0" * 24 + Web3.to_checksum_address(address)[2:].lower()

    @staticmethod
    def _mark_vault_slot_deployed(slot: VaultSlot) -> None:
        VaultSlot.objects.filter(pk=slot.pk).update(is_deployed=True)
        slot.is_deployed = True

    def _create_deposit(
        self,
        *,
        slot: VaultSlot,
        customer: Customer | None = None,
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
            customer=customer or self.customer,
            transfer=transfer,
        )
