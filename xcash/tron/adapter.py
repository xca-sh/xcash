from __future__ import annotations

from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.intents import trc20_balance_of_parameter
from tron.resources import decode_hex_text

from chains.adapters import AdapterInterface
from chains.adapters import TxCheckResult
from chains.adapters import TxCheckStatus
from common.tron_codec import TronAddressCodec


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
                payload = client.get_account(address=address)
            except TronClientError as exc:
                raise RuntimeError("failed to fetch Tron native balance") from exc
            # 网关错误响应可能带 200 + Error 字段；未激活账户返回空 dict 才是合法 0。
            if not isinstance(payload, dict) or "Error" in payload:
                raise RuntimeError(
                    "failed to fetch Tron native balance: invalid response"
                )
            return int(payload.get("balance") or 0)

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

        # 异常响应形态绝不能静默当 0：假 0 会让归集调度删除计划并把余额快照写脏，
        # 且对账兜底按快照 value>0 补建，同样被假 0 短路，资金会滞留到下笔入账。
        # 这里与 estimate_contract_call_energy 对齐，先确认节点明确应答成功再读余额，
        # 失败一律抛错，让 refresh_vault_slot_balance_safely 返回 None 走退避重试。
        result = payload.get("result") or {}
        if not isinstance(result, dict) or result.get("result") is not True:
            code = str(result.get("code") or "") if isinstance(result, dict) else ""
            message = (
                decode_hex_text(result.get("message"))
                if isinstance(result, dict)
                else ""
            )
            raise RuntimeError(
                "failed to fetch Tron TRC20 balance: "
                f"{message or code or 'invalid response'}"
            )
        constant_result = payload.get("constant_result") or []
        if not constant_result:
            raise RuntimeError(
                "failed to fetch Tron TRC20 balance: constant_result missing"
            )
        return int(str(constant_result[0]), 16)

    def tx_result(
        self, chain, tx_hash: str
    ) -> TxCheckStatus | TxCheckResult | Exception:
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
        # TransactionInfo.result 枚举只有 SUCESS（协议原文拼写，少一个 C）/FAILED
        # 两值，java-tron 按 proto3 语义对成功（默认值）省略该字段；只认 FAILED
        # （或数字枚举 1）为失败，防 include-defaults 网关输出 "SUCESS" 时把成功
        # 交易误判失败。
        top_level_result = str(payload.get("result") or "").upper()
        if top_level_result in ("FAILED", "1"):
            return TxCheckResult(
                status=TxCheckStatus.FAILED,
                block_number=block_number,
                block_hash=block_hash,
            )
        result = receipt.get("result")
        # 原生 TRX TransferContract 成功回执可能没有 result；tx 已入块即视为成功。
        if result in (None, "", "SUCCESS"):
            if block_number is None:
                return TxCheckStatus.MISSING
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
