"""
轮询器 _observe_confirmed_transaction 和 _parse_erc20_transfer_log 的单元测试。

覆盖：
- 原生币路径：get_block + get_transaction → 构建正确的 ObservedTransferPayload
- ERC-20 路径：从 receipt.logs 解析 Transfer 事件 → 构建正确的 ObservedTransferPayload
- _parse_erc20_transfer_log 独立测试：正常解析、空 logs、非 Transfer topic
- 集成测试：poll_chain → create_observed_transfer → process() 完整管线
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import Mock
from unittest.mock import PropertyMock
from unittest.mock import patch

import eth_abi
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
from chains.models import TransferType
from chains.models import Transfer
from chains.models import Wallet
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.choices import TxKind
from evm.poller import EvmTaskPoller
from evm.internal_tx._log_utils import matches_transfer_log
from evm.internal_tx._log_utils import normalize_log_index
from evm.models import EvmTxTask
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0

# ---------------------------------------------------------------------------
# 公共测试地址（已通过 Web3.to_checksum_address 转换，满足 EIP-55 checksum 要求）
# ---------------------------------------------------------------------------
_SENDER_HEX = Web3.to_checksum_address("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
_RECEIVER_HEX = Web3.to_checksum_address("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
_VAULT_HEX = Web3.to_checksum_address("0xcccccccccccccccccccccccccccccccccccccccc")
_CONTRACT_HEX = Web3.to_checksum_address("0xdddddddddddddddddddddddddddddddddddddddd")
_ERC20_TRANSFER_SELECTOR = "0xa9059cbb"


def _make_erc20_transfer_calldata(
    *,
    to_hex: str = _RECEIVER_HEX,
    value_int: int = 100_000_000,
) -> str:
    encoded_args = eth_abi.encode(["address", "uint256"], [to_hex, value_int]).hex()
    return f"{_ERC20_TRANSFER_SELECTOR}{encoded_args}"


def _make_erc20_transfer_log(
    *,
    contract_address: str = _CONTRACT_HEX,
    from_hex: str = _SENDER_HEX,
    to_hex: str = _RECEIVER_HEX,
    value_int: int = 100_000_000,
    log_index: int = 5,
) -> dict:
    """构造一条符合 ERC-20 Transfer 规范的 receipt log 字典。

    topics[0] = Transfer(address,address,uint256) keccak
    topics[1] = from（左填充 32 字节）
    topics[2] = to（左填充 32 字节）
    data       = value（32 字节大端整数，hex）
    """
    topic0_bytes = bytes.fromhex(ERC20_TRANSFER_TOPIC0.removeprefix("0x"))
    # 地址左填充到 32 字节（去掉 0x 前缀后补 0 到 64 位）
    from_padded = bytes.fromhex(from_hex.removeprefix("0x").zfill(64))
    to_padded = bytes.fromhex(to_hex.removeprefix("0x").zfill(64))
    value_hex = "0x" + hex(value_int)[2:].zfill(64)

    return {
        "address": contract_address,
        "topics": [topic0_bytes, from_padded, to_padded],
        "data": value_hex,
        "logIndex": log_index,
    }


# ---------------------------------------------------------------------------
# Task 5a：ERC20 Transfer 日志工具独立测试
# ---------------------------------------------------------------------------
class Erc20TransferLogUtilsTest(TestCase):
    """ERC20 Transfer 日志匹配工具的纯逻辑单元测试，不依赖 DB。"""

    def test_matches_valid_transfer_log(self):
        """正常 ERC-20 Transfer 事件可按合约、from、to、value 匹配。"""
        log = _make_erc20_transfer_log(
            contract_address=_CONTRACT_HEX,
            from_hex=_SENDER_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=100_000_000,
            log_index=5,
        )

        self.assertTrue(
            matches_transfer_log(
                log,
                token=_CONTRACT_HEX,
                from_address=_SENDER_HEX,
                to_address=_RECEIVER_HEX,
                value=Decimal(100_000_000),
            )
        )

    def test_skips_non_transfer_topics(self):
        """topic0 不匹配 Transfer 签名的日志（如 Approval）应被跳过。"""
        # 构造一条 Approval 日志：topic0 使用 Approval keccak
        approval_topic0 = Web3.keccak(text="Approval(address,address,uint256)")
        from_padded = bytes.fromhex(_SENDER_HEX.removeprefix("0x").zfill(64))
        to_padded = bytes.fromhex(_RECEIVER_HEX.removeprefix("0x").zfill(64))
        approval_log = {
            "topics": [approval_topic0, from_padded, to_padded],
            "data": "0x" + hex(1000)[2:].zfill(64),
            "logIndex": 0,
        }

        self.assertFalse(
            matches_transfer_log(
                approval_log,
                token=_CONTRACT_HEX,
                from_address=_SENDER_HEX,
                to_address=_RECEIVER_HEX,
                value=Decimal(1000),
            )
        )

    def test_filters_by_contract_sender_recipient_and_value(self):
        """内部任务解析 receipt 时必须按任务预期筛选唯一 Transfer 日志。"""
        wrong_contract = _make_erc20_transfer_log(
            contract_address=Web3.to_checksum_address(
                "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
            ),
            from_hex=_SENDER_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=100_000_000,
            log_index=1,
        )
        wrong_value = _make_erc20_transfer_log(
            contract_address=_CONTRACT_HEX,
            from_hex=_SENDER_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=1,
            log_index=2,
        )
        expected = _make_erc20_transfer_log(
            contract_address=_CONTRACT_HEX,
            from_hex=_SENDER_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=100_000_000,
            log_index=3,
        )

        matches = [
            log
            for log in [wrong_contract, wrong_value, expected]
            if matches_transfer_log(
                log,
                token=_CONTRACT_HEX,
                from_address=_SENDER_HEX,
                to_address=_RECEIVER_HEX,
                value=Decimal(100_000_000),
            )
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(normalize_log_index(matches[0]["logIndex"]), 3)


# ---------------------------------------------------------------------------
# Task 4：_observe_confirmed_transaction — 原生币路径
# ---------------------------------------------------------------------------
class ObserveConfirmedNativeTest(TestCase):
    """原生币路径：从 get_transaction 取 from/to/value，喂回扫描器管线。"""

    def setUp(self):
        self.eth = Crypto.objects.create(
            name="Ethereum Poller Native",
            symbol="ETHCN",
            decimals=18,
            coingecko_id="ethereum-poller-native",
        )
        self.chain = Chain.objects.create(
            code="eth-coord-native",
            name="Ethereum Poller Native",
            type=ChainType.EVM,
            chain_id=90_001,
            # rpc 设为空字符串以跳过 save() 内的自动 RPC 检测
            rpc="",
            native_coin=self.eth,
            active=True,
        )
        # currencies.signals 在 Chain 创建后自动触发 ensure_native_crypto_mapping_for_chain，
        # 已经建好 (eth, chain) 的 ChainToken；这里只需补齐精度覆盖字段即可。
        ChainToken.objects.filter(crypto=self.eth, chain=self.chain).update(decimals=18)
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=_VAULT_HEX,
        )
        self.base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.address,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        self.evm_task = EvmTxTask.objects.create(
            base_task=self.base_task,
            address=self.address,
            chain=self.chain,
            to=Web3.to_checksum_address(_RECEIVER_HEX),
            value=Decimal("1500000000000000000"),
            nonce=0,
            gas=21000,
            tx_kind=TxKind.NATIVE_TRANSFER,
        )

    def test_native_confirmed_feeds_to_scanner_pipeline(self):
        """原生币已确认时，_observe_confirmed_transaction 用正确载荷调用 TransferService。"""
        tx_hash = "0x" + "ab" * 32
        receipt = {
            "blockNumber": 100,
            "status": 1,
            "logs": [],
        }

        mock_block = {"timestamp": 1700000000}
        mock_tx = {
            "from": _SENDER_HEX,
            "to": _RECEIVER_HEX,
            "value": 1500000000000000000,
        }

        mock_w3 = MagicMock()
        mock_w3.eth.get_block.return_value = mock_block
        mock_w3.eth.get_transaction.return_value = mock_tx

        with (
            patch.object(type(self.chain), "w3", new_callable=lambda: property(lambda self: mock_w3)),
            patch("evm.internal_tx.processor.process_internal_transaction") as process_mock,
        ):
            EvmTaskPoller._observe_confirmed_transaction(
                evm_task=self.evm_task,
                tx_hash=tx_hash,
                receipt=receipt,
            )

        process_mock.assert_called_once()
        self.assertEqual(process_mock.call_args.kwargs["chain"], self.chain)
        self.assertEqual(process_mock.call_args.kwargs["receipt"], receipt)
        self.assertEqual(process_mock.call_args.kwargs["tx"], mock_tx)
        mock_w3.eth.get_transaction.assert_called_once_with(tx_hash)


# ---------------------------------------------------------------------------
# Task 5b：_observe_confirmed_transaction — ERC-20 路径
# ---------------------------------------------------------------------------
class ObserveConfirmedErc20Test(TestCase):
    """ERC-20 路径：从 receipt.logs 解析 Transfer，无需调用 get_transaction。"""

    def setUp(self):
        self.eth = Crypto.objects.create(
            name="Ethereum Poller ERC20",
            symbol="ETHCE",
            decimals=18,
            coingecko_id="ethereum-poller-erc20",
        )
        self.usdt = Crypto.objects.create(
            name="Tether Poller",
            symbol="USDTC",
            decimals=6,
            coingecko_id="tether-poller",
        )
        self.chain = Chain.objects.create(
            code="eth-coord-erc20",
            name="Ethereum Poller ERC20",
            type=ChainType.EVM,
            chain_id=90_002,
            rpc="",
            native_coin=self.eth,
            active=True,
        )
        # currencies.signals 在 Chain 创建后自动触发 ensure_native_crypto_mapping_for_chain，
        # 已经建好 (eth, chain) 的 ChainToken；仅补齐精度覆盖和补建 USDT ChainToken。
        ChainToken.objects.filter(crypto=self.eth, chain=self.chain).update(decimals=18)
        ChainToken.objects.create(
            crypto=self.usdt,
            chain=self.chain,
            address=_CONTRACT_HEX,
            decimals=6,
        )
        self.wallet = Wallet.objects.create()
        self.address = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=_VAULT_HEX,
        )
        self.base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.address,
            tx_type=TxTaskType.Withdrawal,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        # ERC-20 发送时 value=0，data 包含 transfer calldata
        self.evm_task = EvmTxTask.objects.create(
            base_task=self.base_task,
            address=self.address,
            chain=self.chain,
            to=Web3.to_checksum_address(_CONTRACT_HEX),
            value=Decimal("0"),
            nonce=0,
            gas=100000,
            data=_make_erc20_transfer_calldata(),
            tx_kind=TxKind.CONTRACT_CALL,
        )

    def test_erc20_confirmed_feeds_to_scanner_pipeline(self):
        """ERC-20 已确认时，从 receipt.logs 解析 Transfer，不调用 get_transaction。"""
        tx_hash = "0x" + "cd" * 32
        transfer_log = _make_erc20_transfer_log(
            contract_address=_CONTRACT_HEX,
            from_hex=_VAULT_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=100_000_000,  # 100 USDT（精度 6）
            log_index=5,
        )
        receipt = {
            "blockNumber": 200,
            "status": 1,
            "logs": [transfer_log],
        }

        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction.return_value = {
            "from": _VAULT_HEX,
            "hash": tx_hash,
        }

        with (
            patch.object(type(self.chain), "w3", new_callable=lambda: property(lambda self: mock_w3)),
            patch("evm.internal_tx.processor.process_internal_transaction") as process_mock,
        ):
            EvmTaskPoller._observe_confirmed_transaction(
                evm_task=self.evm_task,
                tx_hash=tx_hash,
                receipt=receipt,
            )

        process_mock.assert_called_once()
        self.assertEqual(process_mock.call_args.kwargs["chain"], self.chain)
        self.assertEqual(process_mock.call_args.kwargs["receipt"], receipt)
        mock_w3.eth.get_transaction.assert_called_once_with(tx_hash)

    def test_erc20_confirmed_ignores_mismatched_transfer_log(self):
        """receipt 内没有符合任务参数的 Transfer 日志时，不应创建观察转账。"""
        tx_hash = "0x" + "ce" * 32
        wrong_transfer_log = _make_erc20_transfer_log(
            contract_address=_CONTRACT_HEX,
            from_hex=_SENDER_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=100_000_000,
            log_index=5,
        )
        receipt = {
            "blockNumber": 201,
            "status": 1,
            "logs": [wrong_transfer_log],
        }

        mock_w3 = MagicMock()
        mock_w3.eth.get_transaction.return_value = {
            "from": _VAULT_HEX,
            "hash": tx_hash,
        }

        with (
            patch.object(type(self.chain), "w3", new_callable=lambda: property(lambda self: mock_w3)),
            patch("evm.internal_tx.processor.process_internal_transaction") as process_mock,
        ):
            EvmTaskPoller._observe_confirmed_transaction(
                evm_task=self.evm_task,
                tx_hash=tx_hash,
                receipt=receipt,
            )

        process_mock.assert_called_once()


# ---------------------------------------------------------------------------
# 集成测试：poll_chain → TransferService → process() 完整管线
# ---------------------------------------------------------------------------
class PollerIntegrationTest(TestCase):
    """协调器兜底路径集成测试：scanner 漏扫时，协调器作为兜底创建 Transfer 并完成匹配。"""

    def setUp(self):
        self.wallet = Wallet.objects.create()
        self.native = Crypto.objects.create(
            name="Ethereum Poller Integration",
            symbol="ETHCI",
            coingecko_id="ethereum-poller-integration",
            decimals=18,
        )
        self.token = Crypto.objects.create(
            name="USDC Poller Integration",
            symbol="USDCCI",
            coingecko_id="usdc-poller-integration",
            decimals=6,
        )
        self.chain = Chain.objects.create(
            code="eth-coord-integ",
            name="Ethereum Poller Integration",
            type=ChainType.EVM,
            chain_id=90_100,
            rpc="",
            native_coin=self.native,
            active=True,
        )
        # currencies.signals 自动创建 native ChainToken；只需创建 token ChainToken
        ChainToken.objects.create(
            crypto=self.token,
            chain=self.chain,
            address=_CONTRACT_HEX,
            decimals=6,
        )
        self.addr = Address.objects.create(
            wallet=self.wallet,
            chain_type=ChainType.EVM,
            usage=AddressUsage.HotWallet,
            bip44_account=0,
            address_index=0,
            address=_VAULT_HEX,
        )

    # ---- helpers ----

    def _create_erc20_withdrawal(self, *, tx_hash):
        """创建一个 ERC-20 提币场景的完整 fixture。"""
        from chains.models import TxHash
        from projects.models import Project
        from withdrawals.models import Withdrawal
        from withdrawals.models import WithdrawalStatus

        project = Project.objects.create(
            name=f"proj-{tx_hash[-6:]}",
            wallet=self.wallet,
            webhook="https://example.com/wh",
        )
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        TxHash.objects.create(
            tx_task=base_task, chain=self.chain, hash=tx_hash, version=0,
        )
        evm_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=self.addr,
            chain=self.chain,
            nonce=0,
            to=_CONTRACT_HEX,
            value=0,
            data=_make_erc20_transfer_calldata(),
            gas=60000,
            tx_kind=TxKind.CONTRACT_CALL,
            gas_price=1,
            signed_payload="0x01",
        )
        withdrawal = Withdrawal.objects.create(
            project=project,
            chain=self.chain,
            crypto=self.token,
            amount=Decimal("100"),
            worth=Decimal("100"),
            out_no=f"out-{tx_hash[-6:]}",
            to=_RECEIVER_HEX,
            tx_task=base_task,
            status=WithdrawalStatus.PENDING,
            hash=tx_hash,
        )
        return withdrawal, base_task, evm_task

    def _create_native_withdrawal(self, *, tx_hash):
        """创建一个 Native 提币场景的完整 fixture。"""
        from chains.models import TxHash
        from projects.models import Project
        from withdrawals.models import Withdrawal
        from withdrawals.models import WithdrawalStatus

        project = Project.objects.create(
            name=f"proj-native-{tx_hash[-6:]}",
            wallet=self.wallet,
            webhook="https://example.com/wh",
        )
        base_task = TxTask.objects.create(
            chain=self.chain,
            address=self.addr,
            tx_type=TxTaskType.Withdrawal,
            tx_hash=tx_hash,
            stage=TxTaskStage.PENDING_CHAIN,
            success=None,
        )
        TxHash.objects.create(
            tx_task=base_task, chain=self.chain, hash=tx_hash, version=0,
        )
        evm_task = EvmTxTask.objects.create(
            base_task=base_task,
            address=self.addr,
            chain=self.chain,
            nonce=0,
            to=_RECEIVER_HEX,
            value=Decimal("1500000000000000000"),
            data="",
            gas=21000,
            tx_kind=TxKind.NATIVE_TRANSFER,
            gas_price=1,
            signed_payload="0x01",
        )
        withdrawal = Withdrawal.objects.create(
            project=project,
            chain=self.chain,
            crypto=self.native,
            amount=Decimal("1.5"),
            worth=Decimal("1.5"),
            out_no=f"out-native-{tx_hash[-6:]}",
            to=_RECEIVER_HEX,
            tx_task=base_task,
            status=WithdrawalStatus.PENDING,
            hash=tx_hash,
        )
        return withdrawal, base_task, evm_task

    def _make_overdue(self, evm_task):
        from datetime import timedelta

        from evm.constants import EVM_PENDING_REBROADCAST_TIMEOUT

        evm_task.last_attempt_at = timezone.now() - timedelta(
            seconds=EVM_PENDING_REBROADCAST_TIMEOUT + 60
        )
        evm_task.save(update_fields=["last_attempt_at"])

    def _mock_erc20_rpc(self, *, tx_hash, block_number=100, timestamp=1700000000):
        """返回一个配好 ERC-20 场景 RPC mock 的 SimpleNamespace。"""
        transfer_log = _make_erc20_transfer_log(
            from_hex=_VAULT_HEX,
            to_hex=_RECEIVER_HEX,
            value_int=100_000_000,  # 100 USDC (6 decimals)
            log_index=3,
        )
        receipt = {"status": 1, "blockNumber": block_number, "logs": [transfer_log]}
        return SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value=receipt),
                get_block=Mock(return_value={"timestamp": timestamp}),
                get_transaction=Mock(return_value={
                    "hash": tx_hash,
                    "from": _VAULT_HEX,
                    "to": _CONTRACT_HEX,
                    "value": 0,
                }),
            ),
        )

    def _mock_native_rpc(self, *, tx_hash, block_number=100, timestamp=1700000000):
        """返回一个配好 native 场景 RPC mock 的 SimpleNamespace。"""
        receipt = {"status": 1, "blockNumber": block_number, "logs": []}
        return SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value=receipt),
                get_block=Mock(return_value={"timestamp": timestamp}),
                get_transaction=Mock(return_value={
                    "hash": tx_hash,
                    "from": _VAULT_HEX,
                    "to": _RECEIVER_HEX,
                    "value": 1500000000000000000,
                    "input": "0x",
                }),
            ),
        )

    # ---- Test 1 ----

    @patch("withdrawals.service.WebhookService.create_event")
    @patch("chains.tasks.process_transfer.apply_async")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_erc20_poller_creates_onchain_transfer_and_dispatches_process(
        self,
        chain_w3_mock,
        process_mock,
        webhook_mock,
    ):
        """ERC-20 提币超时后，协调器创建 Transfer 并派发 process 任务。"""
        tx_hash = "0x" + "a1" * 32
        withdrawal, base_task, evm_task = self._create_erc20_withdrawal(tx_hash=tx_hash)
        self._make_overdue(evm_task)
        chain_w3_mock.return_value = self._mock_erc20_rpc(tx_hash=tx_hash)

        with self.captureOnCommitCallbacks(execute=True):
            EvmTaskPoller.poll_chain(chain=self.chain)

        # 验证 Transfer 被创建且字段正确
        self.assertEqual(
            Transfer.objects.filter(chain=self.chain, hash=tx_hash).count(), 1,
        )
        transfer = Transfer.objects.get(chain=self.chain, hash=tx_hash)
        self.assertEqual(transfer.event_id, "erc20:3")
        self.assertEqual(transfer.from_address, _VAULT_HEX)
        self.assertEqual(transfer.to_address, _RECEIVER_HEX)
        self.assertEqual(transfer.value, Decimal("100000000"))
        self.assertEqual(transfer.amount, Decimal("100"))

        # process_transfer.apply_async 应被调用一次（on_commit 回调）
        process_mock.assert_called_once()

        # TxTask 应已被推进到 PENDING_CONFIRM
        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CONFIRM)

    # ---- Test 2 ----

    @patch("withdrawals.service.WebhookService.create_event")
    @patch("chains.tasks.process_transfer.apply_async")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_erc20_poller_observe_then_process_matches_withdrawal(
        self,
        chain_w3_mock,
        process_mock,
        webhook_mock,
    ):
        """ERC-20 完整管线：poll_chain 创建 Transfer → process() 匹配提币。"""
        from withdrawals.models import WithdrawalStatus

        tx_hash = "0x" + "a2" * 32
        withdrawal, base_task, evm_task = self._create_erc20_withdrawal(tx_hash=tx_hash)
        self._make_overdue(evm_task)
        chain_w3_mock.return_value = self._mock_erc20_rpc(tx_hash=tx_hash)

        with self.captureOnCommitCallbacks(execute=True):
            EvmTaskPoller.poll_chain(chain=self.chain)

        # 手动调用 process()（模拟 Celery worker 执行）
        transfer = Transfer.objects.get(chain=self.chain, hash=tx_hash)
        transfer.process()

        withdrawal.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.CONFIRMING)
        self.assertEqual(withdrawal.transfer, transfer)
        self.assertEqual(transfer.type, TransferType.Withdrawal)
        self.assertIsNotNone(transfer.processed_at)

    def test_process_ignores_internal_withdrawal_when_transfer_value_mismatches(self):
        """同 tx_hash 的异常事件不应仅凭 hash 绑定为内部提币。"""
        from withdrawals.models import WithdrawalStatus

        tx_hash = "0x" + "a6" * 32
        withdrawal, base_task, _evm_task = self._create_erc20_withdrawal(
            tx_hash=tx_hash
        )
        transfer = Transfer.objects.create(
            chain=self.chain,
            block=100,
            hash=tx_hash,
            event_id="erc20:bad",
            crypto=self.token,
            from_address=self.addr.address,
            to_address=_RECEIVER_HEX,
            value=Decimal("1"),
            amount=Decimal("0.000001"),
            timestamp=1700000000,
            datetime=timezone.now(),
        )

        transfer.process()

        withdrawal.refresh_from_db()
        base_task.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.PENDING)
        self.assertIsNone(withdrawal.transfer_id)
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CHAIN)
        self.assertEqual(transfer.type, "")
        self.assertIsNotNone(transfer.processed_at)

    # ---- Test 3 ----

    @patch("withdrawals.service.WebhookService.create_event")
    @patch("chains.tasks.process_transfer.apply_async")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_native_poller_observe_then_process_matches_withdrawal(
        self,
        chain_w3_mock,
        process_mock,
        webhook_mock,
    ):
        """Native 完整管线：poll_chain 创建 Transfer → process() 匹配提币。"""
        from withdrawals.models import WithdrawalStatus

        tx_hash = "0x" + "a3" * 32
        withdrawal, base_task, evm_task = self._create_native_withdrawal(tx_hash=tx_hash)
        self._make_overdue(evm_task)
        chain_w3_mock.return_value = self._mock_native_rpc(tx_hash=tx_hash)

        with self.captureOnCommitCallbacks(execute=True):
            EvmTaskPoller.poll_chain(chain=self.chain)

        transfer = Transfer.objects.get(chain=self.chain, hash=tx_hash)
        self.assertEqual(transfer.event_id, "native:tx")

        # 手动调用 process()
        transfer.process()

        withdrawal.refresh_from_db()
        transfer.refresh_from_db()
        self.assertEqual(withdrawal.status, WithdrawalStatus.CONFIRMING)
        self.assertEqual(withdrawal.transfer, transfer)
        self.assertEqual(transfer.type, TransferType.Withdrawal)
        self.assertIsNotNone(transfer.processed_at)

    # ---- Test 4 ----

    @patch("withdrawals.service.WebhookService.create_event")
    @patch("chains.tasks.process_transfer.apply_async")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_idempotent_when_scanner_already_created_transfer(
        self,
        chain_w3_mock,
        process_mock,
        webhook_mock,
    ):
        """Scanner 已创建同一 Transfer 时，协调器不重复创建也不重复派发 process。"""
        tx_hash = "0x" + "a4" * 32
        withdrawal, base_task, evm_task = self._create_erc20_withdrawal(tx_hash=tx_hash)
        self._make_overdue(evm_task)

        # 预先创建 Transfer，模拟 scanner 已处理
        Transfer.objects.create(
            chain=self.chain,
            block=100,
            hash=tx_hash,
            event_id="erc20:3",
            crypto=self.token,
            from_address=_VAULT_HEX,
            to_address=_RECEIVER_HEX,
            value=Decimal("100000000"),
            amount=Decimal("100"),
            timestamp=1700000000,
            datetime=timezone.now(),
        )

        chain_w3_mock.return_value = self._mock_erc20_rpc(tx_hash=tx_hash)

        with self.captureOnCommitCallbacks(execute=True):
            EvmTaskPoller.poll_chain(chain=self.chain)

        # 不应产生重复记录
        self.assertEqual(
            Transfer.objects.filter(chain=self.chain, hash=tx_hash).count(), 1,
        )
        # TxTask 仍被推进到 PENDING_CONFIRM（幂等路径也会调用 mark_pending_confirm）
        base_task.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.PENDING_CONFIRM)
        # 幂等路径不应派发 process（created=False）
        process_mock.assert_not_called()

    # ---- Test 5 ----

    @patch("withdrawals.service.WebhookService.create_event")
    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poller_marks_failed_when_receipt_status_zero(
        self,
        chain_w3_mock,
        webhook_mock,
    ):
        """链上 receipt status=0 时，协调器标记 TxTask 失败、提币失败。"""
        from withdrawals.models import WithdrawalStatus

        tx_hash = "0x" + "a5" * 32
        withdrawal, base_task, evm_task = self._create_erc20_withdrawal(tx_hash=tx_hash)
        self._make_overdue(evm_task)

        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"status": 0}),
            ),
        )

        with self.captureOnCommitCallbacks(execute=True):
            EvmTaskPoller.poll_chain(chain=self.chain)

        base_task.refresh_from_db()
        withdrawal.refresh_from_db()
        self.assertEqual(base_task.stage, TxTaskStage.FINALIZED)
        self.assertIs(base_task.success, False)
        self.assertEqual(withdrawal.status, WithdrawalStatus.FAILED)
        # 失败交易不应创建 Transfer
        self.assertEqual(
            Transfer.objects.filter(hash=tx_hash).count(), 0,
        )

    @patch.object(Chain, "w3", new_callable=PropertyMock)
    def test_poll_continues_when_failed_finalize_raises(
        self,
        chain_w3_mock,
    ):
        def create_evm_task(*, tx_hash: str, nonce: int) -> EvmTxTask:
            base_task = TxTask.objects.create(
                chain=self.chain,
                address=self.addr,
                tx_type=TxTaskType.Withdrawal,
                tx_hash=tx_hash,
                stage=TxTaskStage.PENDING_CHAIN,
                success=None,
            )
            return EvmTxTask.objects.create(
                base_task=base_task,
                address=self.addr,
                chain=self.chain,
                nonce=nonce,
                to=_RECEIVER_HEX,
                value=Decimal("1"),
                gas=21000,
                tx_kind=TxKind.NATIVE_TRANSFER,
                gas_price=1,
                signed_payload="0x01",
            )

        first_evm_task = create_evm_task(tx_hash="0x" + "b1" * 32, nonce=0)
        second_evm_task = create_evm_task(tx_hash="0x" + "b2" * 32, nonce=1)
        self._make_overdue(first_evm_task)
        self._make_overdue(second_evm_task)
        chain_w3_mock.return_value = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"status": 0}),
            ),
        )

        with patch.object(
            EvmTaskPoller,
            "_finalize_failed_task",
            side_effect=[RuntimeError("handler failed"), True],
        ) as finalize_failed_mock:
            EvmTaskPoller.poll_chain(chain=self.chain)

        self.assertEqual(finalize_failed_mock.call_count, 2)
