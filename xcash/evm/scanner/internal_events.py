from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.utils import timezone
from web3 import Web3

from chains.models import Address
from chains.models import Chain
from chains.models import ChainType
from evm.internal_tx import processor as internal_tx_processor
from evm.internal_tx.routing import UnknownInternalBroadcastError
from evm.scanner.constants import XCASH_COLLECTED_TOPIC0
from evm.scanner.constants import XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0
from evm.scanner.rpc import EvmScannerRpcClient

logger = structlog.get_logger()


class EvmContractEventObserver:
    """观察系统合约生命周期事件，并把系统地址发起的交易交给 internal tx 收口。"""

    @classmethod
    def observe_logs(
        cls,
        *,
        chain: Chain,
        logs: list[dict[str, Any]],
        rpc_client: EvmScannerRpcClient,
    ) -> set[str]:
        tx_hashes = cls._contract_lifecycle_tx_hashes(logs)
        internal_tx_hashes: set[str] = set()
        for tx_hash in sorted(tx_hashes):
            tx = rpc_client.get_transaction(tx_hash=tx_hash)
            from_address = cls._tx_from_address(tx)
            if from_address is None:
                logger.warning(
                    "EVM 合约事件交易发送方解析失败，已跳过内部处理",
                    chain=chain.code,
                    tx_hash=tx_hash,
                )
                continue
            if not cls._is_system_address(from_address):
                continue

            internal_tx_hashes.add(tx_hash)
            receipt = rpc_client.get_transaction_receipt(tx_hash=tx_hash)
            if receipt is None:
                logger.warning(
                    "EVM 系统合约事件缺少 receipt，已等待后续重扫",
                    chain=chain.code,
                    tx_hash=tx_hash,
                )
                continue
            block_number = cls._parse_int(receipt.get("blockNumber", 0))
            block_timestamp = rpc_client.get_block_timestamp(block_number=block_number)
            try:
                internal_tx_processor.process_internal_transaction(
                    chain=chain,
                    tx=dict(tx),
                    receipt=dict(receipt),
                    block_timestamp=block_timestamp,
                    occurred_at=datetime.fromtimestamp(
                        block_timestamp,
                        tz=timezone.get_current_timezone(),
                    ),
                )
            except UnknownInternalBroadcastError as exc:
                logger.warning(
                    "EVM 系统合约事件未找到对应 TxTask",
                    chain=chain.code,
                    tx_hash=tx_hash,
                    error=str(exc),
                )
        return internal_tx_hashes

    @classmethod
    def _contract_lifecycle_tx_hashes(cls, logs: list[dict[str, Any]]) -> set[str]:
        tx_hashes: set[str] = set()
        for log in logs:
            if log.get("removed"):
                continue
            topics = list(log.get("topics") or [])
            if not topics:
                continue
            topic0 = cls._normalize_hash(topics[0])
            if topic0 not in {
                XCASH_COLLECTED_TOPIC0.lower(),
                XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0.lower(),
            }:
                continue
            try:
                tx_hashes.add(cls._normalize_required_hash(log["transactionHash"]))
            except (KeyError, TypeError, ValueError):
                logger.warning("EVM 合约事件缺少 transactionHash，已跳过")
        return tx_hashes

    @staticmethod
    def _tx_from_address(tx: Any) -> str | None:
        if tx is None:
            return None
        raw_from = None
        if isinstance(tx, dict):
            raw_from = tx.get("from")
        if raw_from is None:
            raw_from = getattr(tx, "from_", None) or getattr(tx, "fromAddress", None)
        if raw_from is None and hasattr(tx, "__getitem__"):
            try:
                raw_from = tx["from"]
            except (KeyError, TypeError):
                raw_from = None
        if not raw_from:
            return None
        try:
            return Web3.to_checksum_address(str(raw_from))
        except ValueError:
            return None

    @staticmethod
    def _is_system_address(address: str) -> bool:
        return Address.objects.filter(
            chain_type=ChainType.EVM,
            address=Web3.to_checksum_address(address),
        ).exists()

    @staticmethod
    def _to_hex(value: object) -> str:
        if hasattr(value, "hex"):
            hex_value = value.hex()
        else:
            hex_value = str(value)
        return hex_value[2:] if hex_value.startswith("0x") else hex_value

    @classmethod
    def _normalize_hash(cls, value: object | None) -> str | None:
        if value is None:
            return None
        raw_hex = cls._to_hex(value)
        return f"0x{raw_hex.lower()}" if raw_hex else None

    @classmethod
    def _normalize_required_hash(cls, value: object) -> str:
        normalized = cls._normalize_hash(value)
        if normalized is None:
            raise ValueError("hash is empty")
        return normalized

    @staticmethod
    def _parse_int(raw_value: object) -> int:
        if isinstance(raw_value, int):
            return raw_value
        value = str(raw_value).strip()
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value) if value else 0
