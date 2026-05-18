from types import SimpleNamespace
from unittest.mock import Mock

from django.test import TestCase
from web3 import Web3

from chains.models import Chain
from chains.models import ChainType
from currencies.models import ChainToken
from currencies.models import Crypto


class EvmAdapterTests(TestCase):
    def test_get_balance_treats_native_symbol_token_as_erc20_on_non_native_chain(self):
        native = Crypto.objects.create(
            name="Adapter Ether",
            symbol="AETH",
            coingecko_id="adapter-ether",
        )
        token = Crypto.objects.create(
            name="Adapter BSC Token",
            symbol="BSC",
            coingecko_id="adapter-bsc-token",
        )
        chain = Chain.objects.create(
            code="adapter-erc20-chain",
            name="Adapter ERC20 Chain",
            type=ChainType.EVM,
            chain_id=919_001,
            native_coin=native,
        )
        token_address = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000b01"
        )
        owner = Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000b02"
        )
        ChainToken.objects.create(chain=chain, crypto=token, address=token_address)
        assert token.is_native
        assert token != chain.native_coin
        balance_call = Mock(return_value=77)
        balance_of = Mock(return_value=SimpleNamespace(call=balance_call))
        contract = SimpleNamespace(functions=SimpleNamespace(balanceOf=balance_of))
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_balance=Mock(return_value=5),
                contract=Mock(return_value=contract),
            ),
        )

        from evm.adapter import EvmAdapter

        self.assertEqual(EvmAdapter.get_balance(owner, chain, token), 77)
        chain.w3.eth.get_balance.assert_not_called()
        chain.w3.eth.contract.assert_called_once()
        balance_of.assert_called_once_with(owner)

    def test_tx_result_returns_confirmed_when_status_is_one(self):
        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"status": 1}),
            ),
        )

        from chains.adapters import TxCheckStatus
        from evm.adapter import EvmAdapter

        result = EvmAdapter.tx_result(chain, "0x" + "ab" * 32)
        self.assertEqual(result, TxCheckStatus.CONFIRMED)

    def test_tx_result_returns_failed_when_status_is_zero(self):
        # 链上执行失败（revert）应返回 FAILED，而不是和 pending / not found 混为一类。
        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"status": 0}),
            ),
        )

        from chains.adapters import TxCheckStatus
        from evm.adapter import EvmAdapter

        result = EvmAdapter.tx_result(chain, "0x" + "ab" * 32)
        self.assertEqual(result, TxCheckStatus.FAILED)

    def test_tx_result_returns_confirming_when_transaction_not_found(self):
        from web3.exceptions import TransactionNotFound

        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(
                    side_effect=TransactionNotFound("0x" + "ab" * 32),
                ),
            ),
        )

        from chains.adapters import TxCheckStatus
        from evm.adapter import EvmAdapter

        result = EvmAdapter.tx_result(chain, "0x" + "ab" * 32)
        self.assertEqual(result, TxCheckStatus.CONFIRMING)

    def test_tx_result_returns_confirming_when_receipt_is_none(self):
        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value=None),
            ),
        )

        from chains.adapters import TxCheckStatus
        from evm.adapter import EvmAdapter

        result = EvmAdapter.tx_result(chain, "0x" + "ab" * 32)
        self.assertEqual(result, TxCheckStatus.CONFIRMING)

    def test_tx_result_returns_exception_when_receipt_missing_status(self):
        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(return_value={"transactionHash": "0x01"}),
            ),
        )

        from evm.adapter import EvmAdapter

        result = EvmAdapter.tx_result(chain, "0x" + "ab" * 32)
        self.assertIsInstance(result, RuntimeError)

    def test_tx_result_returns_exception_on_rpc_error(self):
        # RPC 调用异常（网络问题等）应返回异常对象，由上层决定是否重试。
        chain = Chain(
            code="eth",
            name="Ethereum",
            type=ChainType.EVM,
            chain_id=1,
            native_coin=Crypto(name="Ethereum", symbol="ETH", coingecko_id="ethereum"),
        )
        rpc_error = ConnectionError("node unreachable")
        chain.__dict__["w3"] = SimpleNamespace(
            eth=SimpleNamespace(
                get_transaction_receipt=Mock(side_effect=rpc_error),
            ),
        )

        from evm.adapter import EvmAdapter

        result = EvmAdapter.tx_result(chain, "0x" + "ab" * 32)
        self.assertIsInstance(result, ConnectionError)
