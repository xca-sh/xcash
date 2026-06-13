from __future__ import annotations

from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.codec import TronAddressCodec
from tron.intents import trc20_balance_of_parameter

from chains.adapters import AdapterInterface
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus


class TronAdapter(AdapterInterface):
    @staticmethod
    def validate_address(address: str) -> bool:
        return TronAddressCodec.is_valid_base58(address)

    def is_address(self, chain, address: str) -> bool:
        return self.validate_address(address)

    def is_contract(self, chain, address: str) -> bool:
        if not self.validate_address(address):
            return False
        try:
            payload = TronHttpClient(chain=chain).get_contract(address=address)
        except TronClientError:
            return False
        # wallet/getcontract 只对顶层 DeployContract 部署回填 bytecode/abi；工厂用
        # CREATE2 内部创建的 VaultSlot clone 不带这两个字段，只回填 code_hash。非合约
        # 地址（EOA / 未部署的反事实地址）返回空 payload，code_hash 缺失。故以 code_hash
        # 作为「链上存在合约代码」的判据，同时覆盖顶层合约与 CREATE2 clone 两种部署。
        return bool(payload.get("code_hash"))

    def get_balance(self, address, chain, crypto) -> int:
        if not self.validate_address(address):
            raise ValueError(f"invalid tron address: {address}")

        client = TronHttpClient(chain=chain)
        if crypto == chain.native_coin:
            try:
                return int(client.get_account(address=address).get("balance") or 0)
            except TronClientError as exc:
                raise RuntimeError("failed to fetch Tron native balance") from exc

        token_address = crypto.address(chain)
        if not token_address:
            raise ValueError(
                f"Crypto {crypto.symbol} is not deployed on chain {chain.code}."
            )

        try:
            payload = client.trigger_constant_contract(
                owner_address=address,
                contract_address=token_address,
                function_selector="balanceOf(address)",
                parameter=trc20_balance_of_parameter(address),
            )
        except TronClientError as exc:
            raise RuntimeError("failed to fetch Tron TRC20 balance") from exc

        constant_result = payload.get("constant_result") or []
        if not constant_result:
            return 0
        return int(str(constant_result[0]), 16)

    def tx_result(self, chain, tx_hash: str) -> TxCheckStatus | TxCheckResult | Exception:
        try:
            client = TronHttpClient(chain=chain)
            payload = client.get_transaction_info_by_id(tx_hash)
        except TronClientError as exc:
            return exc

        if not payload or payload.get("id") != tx_hash:
            return TxCheckStatus.MISSING

        receipt = payload.get("receipt") or {}
        block_number = self.receipt_block_number(payload)
        block_hash = self.receipt_block_hash(
            client=client,
            block_number=block_number,
        )
        result = receipt.get("result")
        if result == "SUCCESS":
            return TxCheckResult(
                status=TxCheckStatus.SUCCEEDED,
                block_number=block_number,
                block_hash=block_hash,
            )
        if result:
            return TxCheckResult(
                status=TxCheckStatus.FAILED,
                block_number=block_number,
                block_hash=block_hash,
            )
        return TxCheckStatus.MISSING

    @staticmethod
    def receipt_block_number(payload: dict) -> int | None:
        try:
            block_number = int(payload.get("blockNumber") or 0)
        except (TypeError, ValueError):
            return None
        return block_number if block_number > 0 else None

    @staticmethod
    def receipt_block_hash(
        *,
        client: TronHttpClient,
        block_number: int | None,
    ) -> str | None:
        if block_number is None:
            return None
        try:
            return client.get_solid_block_id(block_number=block_number)
        except TronClientError:
            return None
