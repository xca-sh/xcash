from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import structlog
from django.utils import timezone
from web3.exceptions import TransactionNotFound

from chains.models import Chain
from chains.models import TxHash
from chains.service import MAX_TRANSFER_VALUE
from chains.service import ObservedTransferPayload
from chains.service import TransferService
from currencies.models import CryptoOnChain
from evm.hexutil import checksum_address
from evm.hexutil import hex_lower
from evm.hexutil import parse_int
from evm.hexutil import parse_optional_int
from evm.hexutil import prefixed_hex
from evm.hexutil import required_hash
from evm.hexutil import topic_to_address
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_RECEIVED_TOPIC0
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError

logger = structlog.get_logger()


@dataclass(frozen=True)
class ParsedEvmTransferLog:
    """扫描器已验证可进入 Transfer 管线的一条外部入账日志。"""

    block_number: int
    block_hash: str
    tx_hash: str
    block_log_index: int | None
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
        token_registry: dict[str, CryptoOnChain],
        owned_addresses: frozenset[str],
        internal_sender_addresses: frozenset[str],
    ) -> None:
        """解析外部入账日志并幂等落库。"""
        candidate_logs = [
            parsed
            for log in raw_logs
            if (
                parsed := cls._parse_log(
                    log=log,
                    chain=chain,
                    token_registry=token_registry,
                    owned_addresses=owned_addresses,
                    internal_sender_addresses=internal_sender_addresses,
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
        token_registry: dict[str, CryptoOnChain],
        owned_addresses: frozenset[str],
        internal_sender_addresses: frozenset[str],
    ) -> ParsedEvmTransferLog | None:
        """按 topic0 分派到原生币或 ERC20 解析；非入账日志返回 None。"""
        if log.get("removed"):
            return None
        topics = list(log.get("topics") or [])
        if not topics:
            return None

        topic0 = prefixed_hex(topics[0])
        if topic0 == XCASH_NATIVE_RECEIVED_TOPIC0.lower():
            return cls._parse_native_log(
                log=log,
                chain=chain,
                owned_addresses=owned_addresses,
                internal_sender_addresses=internal_sender_addresses,
            )
        if topic0 == ERC20_TRANSFER_TOPIC0.lower():
            return cls._parse_erc20_log(
                log=log,
                chain=chain,
                token_registry=token_registry,
                owned_addresses=owned_addresses,
                internal_sender_addresses=internal_sender_addresses,
            )
        return None

    @classmethod
    def _parse_native_log(
        cls,
        *,
        log: dict[str, Any],
        chain: Chain,
        owned_addresses: frozenset[str],
        internal_sender_addresses: frozenset[str],
    ) -> ParsedEvmTransferLog | None:
        """解析 VaultSlot 上的原生币入账事件，并过滤掉不在观察集中的 slot。"""
        topics = list(log.get("topics") or [])
        if len(topics) < 2:
            return None

        try:
            slot_address = checksum_address(str(log.get("address", "")))
            payer = topic_to_address(topics[1])
            value = Decimal(int(hex_lower(log.get("data", "0x0")), 16))
            block_number = parse_int(log["blockNumber"])
            block_hash = required_hash(log["blockHash"])
            tx_hash = required_hash(log["transactionHash"])
            block_log_index = parse_optional_int(log.get("logIndex"))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "EVM 原生币充值日志解析失败，已跳过",
                chain=chain.code,
                error=str(exc),
            )
            return None

        if value <= 0 or slot_address not in owned_addresses:
            return None
        if value > MAX_TRANSFER_VALUE:
            logger.warning(
                "EVM 原生币充值数值超过 Transfer.value 范围，已跳过",
                chain=chain.code,
                tx_hash=tx_hash,
                value=str(value),
            )
            return None
        if payer in internal_sender_addresses:
            return None

        return ParsedEvmTransferLog(
            block_number=block_number,
            block_hash=block_hash,
            tx_hash=tx_hash,
            block_log_index=block_log_index,
            from_address=payer,
            to_address=slot_address,
            crypto=chain.native_coin,
            value=value,
            amount=value.scaleb(-chain.native_coin.get_decimals(chain)),
        )

    @classmethod
    def _parse_erc20_log(
        cls,
        *,
        log: dict[str, Any],
        chain: Chain,
        token_registry: dict[str, CryptoOnChain],
        owned_addresses: frozenset[str],
        internal_sender_addresses: frozenset[str],
    ) -> ParsedEvmTransferLog | None:
        """解析 ERC20 Transfer 日志，仅保留外部地址打入系统观察地址的入账。"""
        topics = list(log.get("topics") or [])
        if len(topics) < 3:
            return None

        try:
            token_address = checksum_address(str(log.get("address", "")))
            token = token_registry.get(token_address)
            if token is None:
                return None

            # 只观察外部地址打入系统观察地址的入账事实；
            # 付款方只排除系统 Address / VaultSlot，商户钱包直收地址可以正常充值。
            # 先判收款方：窗口内绝大多数日志与本系统无关，不必再解码付款方。
            to_address = topic_to_address(topics[2])
            if to_address not in owned_addresses:
                return None
            from_address = topic_to_address(topics[1])
            if from_address in internal_sender_addresses:
                return None

            raw_hex = hex_lower(log.get("data", "0x0"))
            if not raw_hex:
                return None
            value = Decimal(int(raw_hex, 16))
            block_number = parse_int(log["blockNumber"])
            block_hash = required_hash(log["blockHash"])
            tx_hash = required_hash(log["transactionHash"])
            block_log_index = parse_optional_int(log.get("logIndex"))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "EVM ERC20 Transfer 日志解析失败，已跳过",
                chain=chain.code,
                error=str(exc),
            )
            return None

        if value <= 0:
            return None
        if value > MAX_TRANSFER_VALUE:
            logger.warning(
                "EVM ERC20 Transfer 数值超过 Transfer.value 范围，已跳过",
                chain=chain.code,
                tx_hash=tx_hash,
                block_log_index=block_log_index,
                value=str(value),
            )
            return None

        decimals = token.decimals
        return ParsedEvmTransferLog(
            block_number=block_number,
            block_hash=block_hash,
            tx_hash=tx_hash,
            block_log_index=block_log_index,
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
        """逐条外部入账事件幂等落库。

        event_index 采用交易 receipt 内 logs 数组下标，而不是区块级 logIndex，
        也不是本轮过滤后日志的相对序号。receipt-local 下标只依赖该交易自身发出
        的完整日志序列，不会因为扫描窗口 replay、owned address 注册、token_registry
        配置或 value 校验过滤集变化而漂移，从而保持 (chain, hash, event_index)
        这个持久幂等键稳定。

        receipt 暂不可见或 logIndex 无法在 receipt 中匹配时，绝不能跳过该条日志：
        调用方会在本轮结束后把游标推进到 to_block（批次最多 100 块、只回放 LOG_SCAN_REPLAY_BLOCKS 块），
        跳过等于把一笔真实入账永久静默丢弃——链上钱已进 VaultSlot、会被归集扫走，
        客户账上却没有 Deposit，且无任何自愈路径。故这里上抛 EvmScannerRpcError，
        让本轮扫描中断、游标停在原处、错误落到 EvmScanCursor.last_error 供运维观察，
        由下一轮重扫幂等恢复。若该 tx 确实已被重组丢弃，下一轮 eth_getLogs 自然不再
        返回该日志，扫描随即继续推进，不会永久卡死。

        落库失败同理：任何异常都原样上抛，不按单事件跳过（见 create_observed_transfer
        调用处的说明）。
        """
        timestamp_cache: dict[int, int] = {}
        receipt_cache: dict[str, dict[str, Any] | None] = {}

        for log in logs:
            receipt = cls.receipt_for_log(
                log=log,
                rpc_client=rpc_client,
                receipt_cache=receipt_cache,
            )
            if receipt is None:
                raise EvmScannerRpcError(
                    "EVM 入账日志 receipt 暂不可见，中断本轮扫描以保住游标: "
                    f"chain={chain.code} tx_hash={log.tx_hash}"
                )

            event_index = cls.receipt_event_index_for_log(
                receipt=receipt,
                block_log_index=log.block_log_index,
            )
            if event_index is None:
                raise EvmScannerRpcError(
                    "EVM 入账日志无法在交易 receipt 中定位 event_index，"
                    "中断本轮扫描以保住游标: "
                    f"chain={chain.code} tx_hash={log.tx_hash} "
                    f"block_log_index={log.block_log_index}"
                )

            timestamp = timestamp_cache.get(log.block_number)
            if timestamp is None:
                timestamp = rpc_client.get_block_timestamp(
                    block_number=log.block_number
                )
                timestamp_cache[log.block_number] = timestamp

            observed = ObservedTransferPayload(
                chain=chain,
                block=log.block_number,
                tx_hash=log.tx_hash,
                event_index=event_index,
                from_address=log.from_address,
                to_address=log.to_address,
                crypto=log.crypto,
                value=log.value,
                amount=log.amount,
                timestamp=timestamp,
                datetime=datetime.fromtimestamp(
                    timestamp,
                    tz=timezone.get_current_timezone(),
                ),
                block_hash=log.block_hash,
                source="evm-scan",
            )
            # 落库失败一律上抛，中断本轮、游标停在本段之前，由下一轮重扫幂等恢复。
            # 不能按异常类型挑出"可跳过"的单条事件：入队业务处理与落库同一事务，
            # Procrastinate 会把死锁、连接中断等暂时性 DB 故障改包成非 django.db.Error
            # 的 ConnectorException，按类型放行就会回滚 Transfer 后照常推进游标，
            # 把真实入账永久丢弃。确定性脏数据（值超界 DataError、唯一键冲突
            # IntegrityError）已在 create_observed_transfer 内部用 savepoint 消化。
            TransferService.create_observed_transfer(observed=observed)

    @staticmethod
    def receipt_for_log(
        *,
        log: ParsedEvmTransferLog,
        rpc_client: EvmScannerRpcClient,
        receipt_cache: dict[str, dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        if log.tx_hash in receipt_cache:
            return receipt_cache[log.tx_hash]

        try:
            receipt = rpc_client.get_transaction_receipt(tx_hash=log.tx_hash)
        except EvmScannerRpcError as exc:
            if isinstance(exc.__cause__, TransactionNotFound):
                receipt_cache[log.tx_hash] = None
                return None
            raise
        receipt_cache[log.tx_hash] = receipt
        return receipt

    @classmethod
    def receipt_event_index_for_log(
        cls,
        *,
        receipt: dict[str, Any],
        block_log_index: int | None,
    ) -> int | None:
        """把区块级 logIndex 转成交易 receipt 内日志序号。"""
        if block_log_index is None:
            return None
        for index, receipt_log in enumerate(receipt.get("logs") or []):
            if parse_optional_int(receipt_log.get("logIndex")) == block_log_index:
                return index
        return None
