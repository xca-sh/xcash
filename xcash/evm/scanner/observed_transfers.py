from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from django.utils import timezone
from web3 import Web3

from chains.models import Chain
from chains.models import TxHash
from chains.service import ObservedTransferPayload
from chains.service import TransferService
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_RECEIVED_TOPIC0
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.watchers import EvmWatchSet

logger = structlog.get_logger()


@dataclass(frozen=True)
class ParsedEvmTransferLog:
    """扫描器已验证可进入 Transfer 管线的一条外部入账日志。"""

    block_number: int
    block_hash: str
    tx_hash: str
    from_address: str
    to_address: str
    crypto: Any
    value: Decimal
    amount: Decimal


class EvmObservedTransferProcessor:
    """处理 scanner 已解析出的外部入账事实：过滤与幂等落库。"""

    @classmethod
    def process(
        cls,
        *,
        chain: Chain,
        rpc_client: EvmScannerRpcClient,
        raw_logs: list[dict[str, Any]],
        watch_set: EvmWatchSet,
    ) -> None:
        """解析外部入账日志并幂等落库。"""
        candidate_logs = [
            parsed
            for log in raw_logs
            if (
                parsed := cls._parse_log(
                    log=log,
                    chain=chain,
                    watch_set=watch_set,
                )
            )
            is not None
        ]
        internal_tx_hashes = cls._known_internal_tx_hashes(
            chain=chain,
            logs=candidate_logs,
        )
        parsed_logs = [
            log for log in candidate_logs if log.tx_hash not in internal_tx_hashes
        ]
        cls._persist_logs(
            chain=chain,
            logs=parsed_logs,
            rpc_client=rpc_client,
        )

    @staticmethod
    def _known_internal_tx_hashes(
        *,
        chain: Chain,
        logs: list[ParsedEvmTransferLog],
    ) -> set[str]:
        """返回已登记 TxHash 的本系统主动交易 hash，scanner 必须整体跳过。"""
        tx_hashes = {log.tx_hash for log in logs}
        if not tx_hashes:
            return set()
        return set(
            TxHash.objects.filter(chain=chain, hash__in=tx_hashes).values_list(
                "hash",
                flat=True,
            )
        )

    @classmethod
    def _parse_log(
        cls,
        *,
        log: dict[str, Any],
        chain: Chain,
        watch_set: EvmWatchSet,
    ) -> ParsedEvmTransferLog | None:
        """按 topic0 分派到原生币或 ERC20 解析；非入账日志返回 None。"""
        if log.get("removed"):
            return None
        topics = list(log.get("topics") or [])
        if not topics:
            return None

        topic0 = cls._normalize_hash(topics[0])
        if topic0 == XCASH_NATIVE_RECEIVED_TOPIC0.lower():
            return cls._parse_native_log(log=log, chain=chain, watch_set=watch_set)
        if topic0 == ERC20_TRANSFER_TOPIC0.lower():
            return cls._parse_erc20_log(log=log, chain=chain, watch_set=watch_set)
        return None

    @classmethod
    def _parse_native_log(
        cls,
        *,
        log: dict[str, Any],
        chain: Chain,
        watch_set: EvmWatchSet,
    ) -> ParsedEvmTransferLog | None:
        """解析 VaultSlot 上的原生币入账事件，并过滤掉不在观察集中的 slot。"""
        topics = list(log.get("topics") or [])
        if len(topics) < 2:
            return None

        try:
            slot_address = Web3.to_checksum_address(str(log.get("address", "")))
            payer = cls._topic_to_address(topics[1])
            value = Decimal(int(cls._to_hex(log.get("data", "0x0")), 16))
            block_number = cls._parse_int(log["blockNumber"])
            block_hash = cls._normalize_required_hash(log["blockHash"])
            tx_hash = cls._normalize_required_hash(log["transactionHash"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "EVM 原生币充值日志解析失败，已跳过",
                chain=chain.code,
                error=str(exc),
            )
            return None

        if value <= 0 or slot_address not in watch_set.matched_addresses:
            return None

        return ParsedEvmTransferLog(
            block_number=block_number,
            block_hash=block_hash,
            tx_hash=tx_hash,
            from_address=payer,
            to_address=slot_address,
            crypto=chain.native_coin,
            value=value,
            amount=value.scaleb(-chain.native_coin.decimals),
        )

    @classmethod
    def _parse_erc20_log(
        cls,
        *,
        log: dict[str, Any],
        chain: Chain,
        watch_set: EvmWatchSet,
    ) -> ParsedEvmTransferLog | None:
        """解析 ERC20 Transfer 日志，仅保留外部地址打入系统观察地址的入账。"""
        topics = list(log.get("topics") or [])
        if len(topics) < 3:
            return None

        try:
            token_address = Web3.to_checksum_address(str(log.get("address", "")))
            token = watch_set.tokens_by_address.get(token_address)
            if token is None:
                return None

            from_address = cls._topic_to_address(topics[1])
            to_address = cls._topic_to_address(topics[2])
            # 只观察外部地址打入系统观察地址的入账事实；
            # 系统地址或 VaultSlot 发出的资产移动由 internal_tx receipt 路径收口。
            if to_address not in watch_set.matched_addresses:
                return None
            if from_address in watch_set.matched_addresses:
                return None

            raw_hex = cls._to_hex(log.get("data", "0x0"))
            if not raw_hex:
                return None
            value = Decimal(int(raw_hex, 16))
            block_number = cls._parse_int(log["blockNumber"])
            block_hash = cls._normalize_required_hash(log["blockHash"])
            tx_hash = cls._normalize_required_hash(log["transactionHash"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "EVM ERC20 Transfer 日志解析失败，已跳过",
                chain=chain.code,
                error=str(exc),
            )
            return None

        if value <= 0:
            return None

        decimals = (
            token.decimals if token.decimals is not None else token.crypto.decimals
        )
        return ParsedEvmTransferLog(
            block_number=block_number,
            block_hash=block_hash,
            tx_hash=tx_hash,
            from_address=from_address,
            to_address=to_address,
            crypto=token.crypto,
            value=value,
            amount=value.scaleb(-decimals),
        )

    @classmethod
    def _persist_logs(
        cls,
        *,
        chain: Chain,
        logs: list[ParsedEvmTransferLog],
        rpc_client: EvmScannerRpcClient,
    ) -> None:
        """按 tx_hash 收敛外部入账事实并幂等落库。"""
        timestamp_cache: dict[int, int] = {}

        for tx_hash, tx_logs in cls._group_logs_by_tx_hash(logs=logs).items():
            if len(tx_logs) != 1:
                if len(tx_logs) > 1:
                    logger.warning(
                        "EVM scanner skipped tx with multiple observed inbound events",
                        chain=chain.code,
                        tx_hash=tx_hash,
                        log_count=len(tx_logs),
                    )
                continue

            log = tx_logs[0]
            timestamp = timestamp_cache.get(log.block_number)
            if timestamp is None:
                timestamp = rpc_client.get_block_timestamp(
                    block_number=log.block_number
                )
                timestamp_cache[log.block_number] = timestamp

            TransferService.create_observed_transfer(
                observed=ObservedTransferPayload(
                    chain=chain,
                    block=log.block_number,
                    tx_hash=log.tx_hash,
                    from_address=log.from_address,
                    to_address=log.to_address,
                    crypto=log.crypto,
                    value=log.value,
                    amount=log.amount,
                    timestamp=timestamp,
                    occurred_at=datetime.fromtimestamp(
                        timestamp,
                        tz=timezone.get_current_timezone(),
                    ),
                    block_hash=log.block_hash,
                    source="evm-scan",
                )
            )

    @staticmethod
    def _group_logs_by_tx_hash(
        *,
        logs: list[ParsedEvmTransferLog],
    ) -> dict[str, list[ParsedEvmTransferLog]]:
        grouped_logs: dict[str, list[ParsedEvmTransferLog]] = {}
        for log in logs:
            grouped_logs.setdefault(log.tx_hash, []).append(log)
        return grouped_logs

    @staticmethod
    def _to_hex(value: Any) -> str:
        """提取原始十六进制字面（无 0x 前缀），兼容 bytes 与 str。"""
        if hasattr(value, "hex"):
            hex_value = value.hex()
        else:
            hex_value = str(value)
        return hex_value[2:] if hex_value.startswith("0x") else hex_value

    @classmethod
    def _normalize_hash(cls, value: object | None) -> str | None:
        """转成带 0x 前缀的小写哈希串，空值返回 None。"""
        if value is None:
            return None
        raw_hex = cls._to_hex(value)
        return f"0x{raw_hex.lower()}" if raw_hex else None

    @classmethod
    def _normalize_required_hash(cls, value: object) -> str:
        """要求哈希必填的归一化变体，空值直接抛错。"""
        normalized = cls._normalize_hash(value)
        if normalized is None:
            raise ValueError("hash is empty")
        return normalized

    @staticmethod
    def _parse_int(raw_value: Any) -> int:
        """兼容十进制 / 0x 十六进制 / int 的整数解析。"""
        if isinstance(raw_value, int):
            return raw_value
        value = str(raw_value).strip()
        if value.startswith(("0x", "0X")):
            return int(value, 16)
        return int(value) if value else 0

    @staticmethod
    def _normalize_address(value: Any) -> str | None:
        if value is None:
            return None
        try:
            return Web3.to_checksum_address(str(value))
        except ValueError:
            return None

    @staticmethod
    def _topic_to_address(topic: object) -> str:
        """从 32 字节 topic 取后 20 字节作为 checksum 地址。"""
        raw_hex = EvmObservedTransferProcessor._to_hex(topic)
        return Web3.to_checksum_address(f"0x{raw_hex[-40:]}")
