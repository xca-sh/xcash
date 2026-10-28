from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.test import TestCase
from web3 import Web3

from chains.constants import ChainCode
from chains.constants import ChainType
from chains.models import Address
from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStatus
from chains.models import TxTaskType
from chains.models import Wallet
from evm.admin import EvmTxTaskAdmin
from evm.models import EvmTxTask
from evm.tests._fixtures import make_evm_chain


class EvmTxTaskAdminTests(SimpleTestCase):
    def test_tx_task_admin_excludes_signed_payload(self):
        model_admin = EvmTxTaskAdmin(EvmTxTask, AdminSite())

        self.assertIn("signed_payload", model_admin.get_exclude(Mock(), obj=None))


class EvmTxTaskAdminActionTests(TestCase):
    def setUp(self):
        self.admin = EvmTxTaskAdmin(EvmTxTask, AdminSite())
        self.admin.message_user = Mock()
        self.chain = make_evm_chain(code=ChainCode.Ethereum)
        self.wallet = Wallet.objects.create()
        self.sender = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x0000000000000000000000000000000000000a01"
            ),
        )

    def create_task(self, *, status: str, nonce: int) -> EvmTxTask:
        base_task = TxTask.objects.create(
            chain=self.chain,
            sender=self.sender,
            tx_type=TxTaskType.VaultSlotCollect,
            status=status,
            tx_hash="0x" + f"{nonce + 1:064x}",
        )
        return EvmTxTask.objects.create(
            base_task=base_task,
            sender=self.sender,
            chain=self.chain,
            nonce=nonce,
            to=Web3.to_checksum_address("0x0000000000000000000000000000000000000b01"),
            value=0,
            gas=21_000,
            data="0xdeadbeef",
            gas_price=1,
        )

    def make_request(self):
        user = get_user_model().objects.create_superuser(
            username="ops-admin",
            password="pw",
        )
        return Mock(user=user)

    def test_mark_queued_failed_action_only_updates_queued_tasks(self):
        queued = self.create_task(status=TxTaskStatus.QUEUED, nonce=0)
        submitted = self.create_task(status=TxTaskStatus.SUBMITTED, nonce=1)
        request = self.make_request()

        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            # 链上 nonce 已越过 nonce=0，表示该 nonce 已被消费，护栏放行。
            w3_mock.return_value.eth.get_transaction_count.return_value = 1
            self.admin.mark_queued_failed_after_nonce_handled(
                request=request,
                queryset=EvmTxTask.objects.filter(pk__in=[queued.pk, submitted.pk]),
            )

        queued.base_task.refresh_from_db()
        submitted.base_task.refresh_from_db()
        self.assertEqual(queued.base_task.status, TxTaskStatus.FAILED)
        self.assertEqual(submitted.base_task.status, TxTaskStatus.SUBMITTED)
        self.admin.message_user.assert_called_once()
        # 标记失败必须留下可追溯的 admin 审计日志。
        self.assertEqual(LogEntry.objects.filter(object_id=str(queued.pk)).count(), 1)

    def test_mark_queued_failed_blocks_when_chain_nonce_not_consumed(self):
        # 链上 nonce 尚未越过该任务：标记失败会造成永久 nonce 缺口，必须拦截。
        queued = self.create_task(status=TxTaskStatus.QUEUED, nonce=0)
        request = self.make_request()

        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value.eth.get_transaction_count.return_value = 0
            self.admin.mark_queued_failed_after_nonce_handled(
                request=request,
                queryset=EvmTxTask.objects.filter(pk=queued.pk),
            )

        queued.base_task.refresh_from_db()
        self.assertEqual(queued.base_task.status, TxTaskStatus.QUEUED)
        self.assertEqual(LogEntry.objects.count(), 0)

    def test_mark_queued_failed_blocks_when_chain_nonce_query_fails(self):
        # 链上 nonce 查询失败时宁可拦截也不放行，避免误造 nonce 缺口。
        queued = self.create_task(status=TxTaskStatus.QUEUED, nonce=0)
        request = self.make_request()

        with patch.object(type(self.chain), "w3", new_callable=PropertyMock) as w3_mock:
            w3_mock.return_value.eth.get_transaction_count.side_effect = RuntimeError(
                "rpc down"
            )
            self.admin.mark_queued_failed_after_nonce_handled(
                request=request,
                queryset=EvmTxTask.objects.filter(pk=queued.pk),
            )

        queued.base_task.refresh_from_db()
        self.assertEqual(queued.base_task.status, TxTaskStatus.QUEUED)

    def test_mark_queued_failed_permission_is_superuser_only(self):
        superuser = Mock(is_active=True, is_superuser=True)
        auditor = Mock(is_active=True, is_superuser=False)
        self.assertTrue(
            self.admin.has_mark_queued_failed_permission(Mock(user=superuser))
        )
        self.assertFalse(
            self.admin.has_mark_queued_failed_permission(Mock(user=auditor))
        )
