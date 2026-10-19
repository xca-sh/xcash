from __future__ import annotations

from typing import Any

import structlog
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from chains.scanning import ScanRoundBudgetExceeded
from chains.scanning import check_scan_round_deadline
from currencies.models import CryptoOnChain
from evm.hexutil import checksum_address
from evm.hexutil import hex_lower
from evm.hexutil import topic_to_address
from evm.models import EvmScanCursor
from evm.scanner.constants import DEFAULT_LOG_SCAN_BATCH_SIZE
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_RECEIVED_TOPIC0
from evm.scanner.observed_transfers import EvmObservedTransferProcessor
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import load_owned_addresses_for_candidates
from evm.scanner.watchers import load_token_registry

logger = structlog.get_logger()

# 每轮回头重扫的块数，用来兜住两类"已扫高度后来才出现数据"的漏账：
# - 负载均衡下 latest 与 getLogs 落到不同节点：geth ≤1.15.0 及其分叉对超出本节点
#   链头的 toBlock 静默返回部分结果，落后节点那几块的日志会被当作空块越过；
# - 链重组把原分叉没有的入账交易带进已扫过的高度。
# 扫描按 evm_log_max_block_range（默认 10 块）一段调用 getLogs，取 4 时各链每轮的
# 分段数与取 2 相同、不增加 RPC 调用；若快链（BSC / Arbitrum）观察到漏账再调大。
LOG_SCAN_REPLAY_BLOCKS = 4

XCASH_NATIVE_RECEIVED_TOPIC0_HEX = hex_lower(XCASH_NATIVE_RECEIVED_TOPIC0)
ERC20_TRANSFER_TOPIC0_HEX = hex_lower(ERC20_TRANSFER_TOPIC0)


class EvmLogScanner:
    """按链扫描外部入账日志。"""

    @classmethod
    def scan_chain(
        cls,
        *,
        chain: Chain,
        batch_size: int = DEFAULT_LOG_SCAN_BATCH_SIZE,
        rpc_client: EvmScannerRpcClient | None = None,
        deadline: float | None = None,
    ):
        """根据游标推进一次正向日志扫描，成功后更新游标。

        deadline 是本轮截止时刻（monotonic），只在 chunk 之间检查，见 chains.scanning。
        """
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持 EVM 链扫描，当前链为 {chain.code}")

        cursor = cls._get_or_create_cursor(chain=chain)
        if not cursor.enabled:
            return
        rpc_client = rpc_client or EvmScannerRpcClient(chain=chain)
        previous_latest_block = chain.latest_block_number

        try:
            latest_block = rpc_client.get_latest_block_number()
            Chain.objects.filter(pk=chain.pk).update(
                latest_block_number=Greatest(F("latest_block_number"), latest_block)
            )

            from chains.tasks import dispatch_block_confirmation_checks_if_needed

            # 已入库充值的确认只依赖链高，不依赖本轮日志全部扫完。先派发再扫日志，
            # 避免持续撞预算饿死 FULL 确认。
            dispatch_block_confirmation_checks_if_needed(
                chain=chain,
                previous_latest_block=previous_latest_block,
            )

            token_registry = load_token_registry(chain=chain)
            scan_window = cls._compute_scan_window(
                cursor=cursor,
                latest_block=latest_block,
                batch_size=batch_size,
            )
            if scan_window is None:
                cls._mark_cursor_idle(cursor=cursor)
                return
            from_block, to_block = scan_window

            # 按 chunk 推进，每扫完一段立即提交游标。整窗口最多 batch_size 块、会被
            # 拆成多次 eth_getLogs，单轮总耗时可能超过轮次预算或撞上进程重启；若等整
            # 窗口扫完才提交游标，中断就会把已经扫完的区块一并回退，下一轮从同一起点
            # 重来——节点稍慢即原地踏步、永远追不上链头。分段提交后中断只损失当前这
            # 一段，已完成的进度不丢。
            chunk_size = max(1, chain.evm_log_max_block_range)
            chunk_from = from_block
            while chunk_from <= to_block:
                if chunk_from > from_block:
                    # 每轮至少扫完一段再看预算，保证节点再慢游标也在前进。
                    check_scan_round_deadline(deadline)
                chunk_to = min(to_block, chunk_from + chunk_size - 1)
                cls.scan_range(
                    chain=chain,
                    rpc_client=rpc_client,
                    token_registry=token_registry,
                    from_block=chunk_from,
                    to_block=chunk_to,
                )
                cls._advance_cursor(cursor=cursor, scanned_to_block=chunk_to)
                chunk_from = chunk_to + 1
        except (EvmScannerRpcError, ScanRoundBudgetExceeded) as exc:
            # 预算耗尽同样是本轮扫描失败；此前 chunk 的进度已提交，只记录错误并原样
            # 上抛，否则 last_scanned_at 刷新后，外部探针会把持续追不上链头报成健康。
            cls._mark_cursor_error(cursor=cursor, exc=exc)
            raise

    @classmethod
    def scan_range(
        cls,
        *,
        chain: Chain,
        rpc_client: EvmScannerRpcClient,
        token_registry: dict[str, CryptoOnChain],
        from_block: int,
        to_block: int,
    ) -> None:
        """对 [from_block, to_block] 区间拉取一次日志并按类型落库。"""
        logs = cls._fetch_logs(
            rpc_client=rpc_client,
            token_registry=token_registry,
            from_block=from_block,
            to_block=to_block,
        )
        cls._process_logs(
            chain=chain,
            logs=logs,
            rpc_client=rpc_client,
            token_registry=token_registry,
        )

    @classmethod
    def _process_logs(
        cls,
        *,
        chain: Chain,
        logs: list[dict[str, Any]],
        rpc_client: EvmScannerRpcClient,
        token_registry: dict[str, CryptoOnChain],
    ) -> None:
        """把外部入账日志交给 Transfer 落库。

        代币表（token_registry）决定关注哪些代币与如何解码；本轮命中的系统自有
        地址（owned_addresses）由本窗口日志即时匹配得出，分两段：
        1. 只用收款方（ERC20 的 to、原生事件的槽位地址）查库。主网一个窗口有数千条
           代币转账，几乎都与本系统无关，这一步就把它们全部排除。
        2. 只对收款方命中的日志，再查付款方是否也是自有地址：自有地址之间的转移不是
           外部入账（由内部交易路径收口），处理器据此跳过。
        处理器只在收款方命中时才看付款方，所以两段的并集与把全部地址一次查完等价，
        候选集与 address__in 查询却小得多。
        """
        parties = cls._transfer_parties_from_logs(logs=logs)
        owned_addresses = load_owned_addresses_for_candidates(
            chain=chain,
            addresses={recipient for recipient, _ in parties},
        )
        owned_senders = {
            sender
            for recipient, sender in parties
            if recipient in owned_addresses and sender is not None
        } - owned_addresses
        if owned_senders:
            owned_addresses |= load_owned_addresses_for_candidates(
                chain=chain,
                addresses=owned_senders,
            )
        EvmObservedTransferProcessor.process(
            chain=chain,
            rpc_client=rpc_client,
            raw_logs=logs,
            token_registry=token_registry,
            owned_addresses=owned_addresses,
        )

    @classmethod
    def _fetch_logs(
        cls,
        *,
        rpc_client: EvmScannerRpcClient,
        token_registry: dict[str, CryptoOnChain],
        from_block: int,
        to_block: int,
    ) -> list[dict[str, Any]]:
        """拉取本轮关注的外部入账日志。"""
        logs: list[dict[str, Any]] = []
        logs.extend(
            rpc_client.get_logs(
                from_block=from_block,
                to_block=to_block,
                addresses=None,
                topic0=XCASH_NATIVE_RECEIVED_TOPIC0,
                summary="获取 EVM Xcash 原生币入账日志失败",
            )
        )
        erc20_addresses = cls._erc20_log_filter_addresses(token_registry=token_registry)
        if erc20_addresses:
            logs.extend(
                rpc_client.get_logs(
                    from_block=from_block,
                    to_block=to_block,
                    addresses=erc20_addresses,
                    topic0=ERC20_TRANSFER_TOPIC0,
                    summary="获取 EVM ERC20 Transfer 日志失败",
                )
            )
        return logs

    @staticmethod
    def _erc20_log_filter_addresses(
        *, token_registry: dict[str, CryptoOnChain]
    ) -> list[str]:
        """返回需要在 eth_getLogs 中作为合约地址过滤的 ERC20 列表。"""
        return sorted(token_registry.keys())

    @classmethod
    def _transfer_parties_from_logs(
        cls,
        *,
        logs: list[dict[str, Any]],
    ) -> list[tuple[str, str | None]]:
        """从本轮日志里抽出每笔转账的 (收款方, 付款方)，地址非法的一侧记为 None。"""
        parties: list[tuple[str, str | None]] = []
        for log in logs:
            if log.get("removed"):
                continue
            topics = list(log.get("topics") or [])
            if not topics:
                continue
            topic0 = hex_lower(topics[0])
            if topic0 == XCASH_NATIVE_RECEIVED_TOPIC0_HEX:
                recipient = cls._safe_address(log.get("address"))
                sender = (
                    cls._safe_topic_address(topics[1]) if len(topics) >= 2 else None
                )
            elif topic0 == ERC20_TRANSFER_TOPIC0_HEX and len(topics) >= 3:
                recipient = cls._safe_topic_address(topics[2])
                sender = cls._safe_topic_address(topics[1])
            else:
                continue
            if recipient is not None:
                parties.append((recipient, sender))
        return parties

    @staticmethod
    def _safe_address(value: Any) -> str | None:
        """转 checksum 地址，非法地址返回 None 而不是抛错。"""
        try:
            return checksum_address(str(value or ""))
        except ValueError:
            return None

    @staticmethod
    def _safe_topic_address(topic: Any) -> str | None:
        """从 32 字节 topic 取最后 20 字节作为地址，非法返回 None。"""
        try:
            return topic_to_address(topic)
        except ValueError:
            return None

    @classmethod
    def _get_or_create_cursor(cls, *, chain: Chain) -> EvmScanCursor:
        """取出或新建本链的扫描游标。

        不加行锁：同链扫描已由 chain-runner 的按链 advisory lock 串行化，游标推进全部
        是带 Greatest 的条件 UPDATE；并发新建由 chain 唯一约束收口（get_or_create 撞约束后回查）。
        """
        cursor, _ = EvmScanCursor.objects.get_or_create(
            chain=chain,
            defaults={"last_scanned_block": 0, "enabled": True},
        )
        return cursor

    @staticmethod
    def _compute_scan_window(
        *,
        cursor: EvmScanCursor,
        latest_block: int,
        batch_size: int,
    ) -> tuple[int, int] | None:
        """根据游标和批次大小算出本轮扫描区间；永远只扫到最新块前一块。

        新建游标（last_scanned_block<=0）只把扫描起点锚定到当前链头，绝不从
        创世块全量回扫历史日志：首轮仅观测最新确认块，之后再按批次正向推进。
        """
        target_block = latest_block - 1
        if target_block <= 0:
            return None

        # 首次扫描：游标尚未锚定，直接对齐链头，避免从区块 1 全量回扫历史。
        if cursor.last_scanned_block <= 0:
            return target_block, target_block

        forward_batch_size = max(1, batch_size)
        to_block = min(target_block, cursor.last_scanned_block + forward_batch_size)
        from_block = max(1, cursor.last_scanned_block + 1 - LOG_SCAN_REPLAY_BLOCKS)
        if from_block > to_block:
            return None
        return from_block, to_block

    @staticmethod
    def _mark_cursor_idle(*, cursor: EvmScanCursor) -> None:
        """无新块可扫时只清空错误状态，不推进游标。"""
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _advance_cursor(*, cursor: EvmScanCursor, scanned_to_block: int) -> None:
        """把游标推进到本轮扫描末端，并清空错误状态。"""
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=Greatest(F("last_scanned_block"), scanned_to_block),
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _mark_cursor_error(*, cursor: EvmScanCursor, exc: Exception) -> None:
        """记录本轮 RPC 错误或预算耗尽到游标，便于运维观察。"""
        logger.warning(
            "EVM 日志扫描失败",
            chain=cursor.chain.code,
            error=str(exc),
        )
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_error=str(exc),
            last_error_at=timezone.now(),
            updated_at=timezone.now(),
        )


def scan_evm_chain_round(chain: Chain, deadline: float) -> None:
    """chain-runner 按链循环调用的 EVM 单轮扫描（登记为链族的 scan_round）。

    RPC 失败只记 warning 后返回：游标错误已由 scan_chain 写入，/health/scanning 据此
    报 failing，runner 照常推进 last_scanned_at、按扫描周期重试。预算耗尽与其他异常
    原样上抛，由 runner 统一记录。
    """
    try:
        EvmLogScanner.scan_chain(chain=chain, deadline=deadline)
    except EvmScannerRpcError:
        logger.warning("EVM 自扫描 RPC 失败", chain=chain.code)
        return
    logger.info("EVM 自扫描完成", chain=chain.code)
