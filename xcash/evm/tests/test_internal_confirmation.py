from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from web3 import Web3

from chains.models import Address
from chains.models import AddressUsage
from chains.models import TxTask
from chains.models import TxTaskStage
from chains.models import Chain
from chains.models import ChainType
from chains.models import TxTaskType
from chains.models import Transfer
from chains.models import TxHash
from chains.models import Wallet
from common.consts import ERC20_TRANSFER_GAS
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.choices import TxKind
from evm.models import EvmTxTask


class EvmInternalTaskConfirmationTests(TestCase):
    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.native = Crypto.objects.create(
            name="Ethereum Internal Confirm",
            symbol="ETHIC",
            coingecko_id="ethereum-internal-confirm",
        )
        self.token = Crypto.objects.create(
            name="USD Coin Internal Confirm",
            symbol="USDCIC",
            coingecko_id="usd-coin-internal-confirm",
            decimals=6,
        )
        self.chain = Chain.objects.create(
            code="eth-internal-confirm",
            name="Ethereum Internal Confirm",
            type=ChainType.EVM,
            chain_id=20002,
            rpc="http://localhost:8545",
            native_coin=self.native,
            active=True,
        )
        ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000c1"
            ),
            decimals=6,
        )
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=1,
            address_index=0,
            address=Web3.to_checksum_address(
                "0x00000000000000000000000000000000000000c2"
            ),
        )

    def _create_withdrawal_with_pending_evm_task(
        self,
        *,
        tx_hash: str,
    ):
        from projects.models import Project
        from withdrawals.models import Withdrawal
        from withdrawals.models import WithdrawalStatus

        project = Project.objects.create(
            name=f"project-{tx_hash[-6:]}",
            wallet=self.wallet,
            webhook="https://example.com/webhook",
        )
        recipient = Web3.to_checksum_address(
            "0x00000000000000000000000000000000000000c3"
        )
        value_raw = 12_340_000
        encoded_args = (
            recipient.lower().replace("0x", "").rjust(64, "0")
            + hex(value_raw)[2:].rjust(64, "0")
        )
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        # 协调器通过 TxHash 历史记录查链上 receipt，必须有至少一条记录。
        TxHash.objects.create(
            tx_task=base_task,
            chain=self.chain,
            hash=tx_hash,
            version=0,
        )
        evm_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=self.addr,
            chain=self.chain,
            nonce=0,
            to=Web3.to_checksum_address("0x00000000000000000000000000000000000000c1"),
            value=0,
            data=f"0xa9059cbb{encoded_args}",
            gas=ERC20_TRANSFER_GAS,
            tx_kind=TxKind.CONTRACT_CALL,
            gas_price=1,
            signed_payload="0x01",
        )
        withdrawal = Withdrawal.objects.create(
            project=project,
            chain=self.chain,
            crypto=self.token,
            amount=Decimal("12.34"),
            worth=Decimal("12.34"),
            out_no=f"out-{tx_hash[-6:]}",
            to=recipient,
            tx_task=base_task,
            status=WithdrawalStatus.PENDING,
            hash=tx_hash,
        )
        return withdrawal, base_task, evm_task

    def _make_overdue(self, evm_task):
        """将 evm_task 的 last_attempt_at 设置为超过阈值。"""

        from evm.constants import EVM_PENDING_REBROADCAST_TIMEOUT

        evm_task.last_attempt_at = timezone.now() - timedelta(
            seconds=EVM_PENDING_REBROADCAST_TIMEOUT + 60
        )
        evm_task.save(update_fields=["last_attempt_at"])

    @patch("withdrawals.service.WebhookService.create_event")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_fails_internal_withdrawal_when_receipt_status_zero(
        self,
        chain_w3_mock,
        webhook_mock,
    ):
        from evm.poller import EvmTaskPoller

        withdrawal, base_task, evm_task = self._create_withdrawal_with_pending_evm_task(
            tx_hash="0x" + "7" * 64
        )
        self._make_overdue(evm_task)
        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"status": 0}),
            )
        )

        with self.captureOnCommitCallbacks(execute=True):
            EvmTaskPoller.poll_chain(chain=self.chain)

        withdrawal.refresh_from_db()
        base_task.refresh_from_db()
        evm_task.refresh_from_db()
        self.assertEqual(withdrawal.status, "failed")
        self.assertEqual(base_task.stage, TxTaskStage.FINALIZED)
        self.assertIs(base_task.success, False)
        self.assertEqual(Transfer.objects.count(), 0)
        # 当前契约：FAILED 不发 webhook（与 withdrawals.tests 一致）。
        webhook_mock.assert_not_called()

    @patch("withdrawals.service.WebhookService.create_event")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_skips_when_within_timeout(
        self,
        chain_w3_mock,
        webhook_mock,
    ):
        """未超时的 PENDING_CHAIN 任务不做任何处理，等待 scanner 自然闭环。"""
        from evm.poller import EvmTaskPoller
        from withdrawals.models import WithdrawalStatus

        withdrawal, base_task, evm_task = self._create_withdrawal_with_pending_evm_task(
            tx_hash="0x" + "8" * 64
        )
        # last_attempt_at=None 或在阈值内，都视为未超时
        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"status": 1}),
            )
        )
        EvmTaskPoller.poll_chain(chain=self.chain)

        withdrawal.refresh_from_db()
        base_task.refresh_from_db()
        evm_task.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.PENDING)
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertIsNone(base_task.success)
        webhook_mock.assert_not_called()

    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_calls_observe_when_receipt_found_and_overdue(
        self,
        chain_w3_mock,
    ):
        """超时后查到 receipt status=1，协调器调用 _observe_confirmed_transaction 喂回扫描器管线。"""
        from evm.poller import EvmTaskPoller

        tx_hash = "0x" + "b" * 64
        receipt = {"status": 1, "blockNumber": 100}
        _, base_task, evm_task = self._create_withdrawal_with_pending_evm_task(
            tx_hash=tx_hash
        )
        self._make_overdue(evm_task)
        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value=receipt),
            )
        )

        with patch.object(
            EvmTaskPoller,
            "_observe_confirmed_transaction",
        ) as observe_mock:
            EvmTaskPoller.poll_chain(chain=self.chain)
            observe_mock.assert_called_once()
            call_kwargs = observe_mock.call_args.kwargs
            self.assertEqual(call_kwargs["tx_hash"], tx_hash)
            self.assertEqual(call_kwargs["receipt"], dict(receipt))

    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_rebroadcasts_when_all_hashes_not_found_and_overdue(
        self,
        chain_w3_mock,
    ):
        """超时后所有历史 hash 均无 receipt，触发重新广播。"""
        from web3.exceptions import TransactionNotFound

        from evm.poller import EvmTaskPoller

        _, base_task, evm_task = self._create_withdrawal_with_pending_evm_task(
            tx_hash="0x" + "9" * 64
        )
        self._make_overdue(evm_task)
        old_attempt_at = evm_task.last_attempt_at

        send_raw_mock = Mock(return_value="0x" + "f" * 64)
        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(
                    side_effect=TransactionNotFound("missing")
                ),
                gas_price=1,
                # 主动阈值 pre-flight 需要 get_balance，余额充足即可通过
                get_balance=Mock(return_value=10**18),
                send_raw_transaction=send_raw_mock,
            )
        )

        EvmTaskPoller.poll_chain(chain=self.chain)

        evm_task.refresh_from_db()
        base_task.refresh_from_db()
        self.assertGreater(evm_task.last_attempt_at, old_attempt_at)
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        send_raw_mock.assert_called_once()

    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_finds_receipt_via_historical_hash(
        self,
        chain_w3_mock,
    ):
        """当前 tx_hash 无 receipt 但历史 hash 有 receipt 时，通过历史 hash 喂回扫描器管线。"""
        from web3.exceptions import TransactionNotFound

        from evm.poller import EvmTaskPoller

        current_hash = "0x" + "c" * 64
        old_hash = "0x" + "d" * 64
        _, base_task, evm_task = self._create_withdrawal_with_pending_evm_task(
            tx_hash=current_hash
        )
        # 模拟 gas 提升重签产生的历史 hash
        TxHash.objects.create(
            tx_task=base_task,
            chain=self.chain,
            hash=old_hash,
            version=1,
        )
        self._make_overdue(evm_task)

        old_receipt = {"status": 1, "blockNumber": 200}

        def receipt_side_effect(tx_hash):
            if tx_hash == old_hash:
                return old_receipt
            raise TransactionNotFound(tx_hash)

        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(side_effect=receipt_side_effect),
            )
        )

        with patch.object(
            EvmTaskPoller,
            "_observe_confirmed_transaction",
        ) as observe_mock:
            EvmTaskPoller.poll_chain(chain=self.chain)
            observe_mock.assert_called_once()
            call_kwargs = observe_mock.call_args.kwargs
            self.assertEqual(call_kwargs["tx_hash"], old_hash)
            self.assertEqual(call_kwargs["receipt"], dict(old_receipt))

    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_continues_when_rebroadcast_raises(
        self,
        chain_w3_mock,
    ):
        """重新广播时 broadcast() 抛异常不会中断 poll 循环。"""
        from web3.exceptions import TransactionNotFound

        from evm.poller import EvmTaskPoller

        _, base_task, evm_task = self._create_withdrawal_with_pending_evm_task(
            tx_hash="0x" + "e" * 64
        )
        self._make_overdue(evm_task)

        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(
                    side_effect=TransactionNotFound("missing")
                ),
                gas_price=1,
                send_raw_transaction=Mock(
                    side_effect=ConnectionError("node unreachable")
                ),
            )
        )

        # 不应抛异常
        EvmTaskPoller.poll_chain(chain=self.chain)

        evm_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
