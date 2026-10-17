from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any

import structlog
from celery.exceptions import SoftTimeLimitExceeded
from web3.exceptions import ExtraDataLengthError

from evm.hexutil import prefixed_hex

if TYPE_CHECKING:
    from web3 import Web3

    from chains.models import Chain


logger = structlog.get_logger()

# RPC 失败时的退避时长（秒），数组长度即"最多重试次数"。
# 初次失败 → 等 0.2s 重试 → 等 0.8s 重试 → 仍失败上抛，整体上限约 1s 的等待开销。
_EVM_RPC_RETRY_BACKOFF_SECONDS = (0.2, 0.8)


class EvmScannerRpcError(RuntimeError):
    """统一包装 EVM 自扫描涉及的 RPC 异常。"""


class EvmScannerRpcClient:
    """对扫描器暴露最小 RPC 面，隔离 Web3 原始异常细节。"""

    def __init__(self, *, chain: Chain):
        self.chain = chain
        # eth_getBlockReceipts 特性检测结果：None 未探测，True 支持，False 不支持。
        # 每个扫描 tick 构造新 client，单 tick 最多触发一次"不支持"探测，避免每块重试。
        self._block_receipts_supported: bool | None = None
        # 单 tick 内 latest_block 缓存：统一日志扫描和兜底复扫复用时只打一次 RPC。
        self._cached_latest_block: int | None = None

    def get_latest_block_number(self) -> int:
        if self._cached_latest_block is not None:
            return self._cached_latest_block
        latest_block = int(
            self._call_with_retry(
                fn=lambda: self.chain.get_latest_block_number,
                summary="获取最新区块失败",
                method="eth_blockNumber",
            )
        )
        self._cached_latest_block = latest_block
        return latest_block

    def get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        addresses: list[str] | None,
        topic0: str | list[str],
        summary: str = "获取 EVM 日志失败",
    ) -> list[dict[str, Any]]:
        """拉取 [from_block, to_block] 的日志。

        区块跨度由调用方按 chain.evm_log_max_block_range 分段控制（EvmLogScanner 逐段
        调用并逐段提交游标），这里整段请求；节点报结果超限时由 _get_logs_chunk 二分。
        """
        if from_block > to_block or addresses == []:
            return []
        return self._get_logs_chunk(
            from_block=from_block,
            to_block=to_block,
            addresses=addresses,
            topic0=topic0,
            summary=summary,
        )

    def _get_logs_chunk(
        self,
        *,
        from_block: int,
        to_block: int,
        addresses: list[str] | None,
        topic0: str | list[str],
        summary: str,
    ) -> list[dict[str, Any]]:
        """拉取单个块区间日志；命中"结果过多/范围过大"类限制时按块二分重试。

        eth_getLogs 只按代币合约地址过滤、不按收款地址过滤，主网高频稳定币在一个
        窗口内可能命中的日志数超过节点上限（如 "query returned more than 10000
        results"）。这类是确定性失败，盲目重试无用、只会让游标停在同一窗口反复超限
        导致整链漏账；这里改为把区间二分递归下探，直到单块。

        单块仍超限时分两种情况：
        - 不带合约地址过滤的查询（原生币 XcashNativeReceived）：任何人都能部署合约
          发出同签名事件，单块刷爆节点上限即可让扫描永久停在该块（整链入账 DoS）。
          此时改用 eth_getBlockReceipts 取整块回执、本地按 topic0 抽取日志，结果与
          eth_getLogs 等价，后续按自有地址过滤的流程不变。
        - 带合约地址过滤的查询（ERC20），或节点不支持整块回执：上抛告警，绝不静默
          跳过（跳过等于永久漏账），由运维放宽节点上限或调小批次后自愈。
        """
        try:
            return self._get_logs_single_range(
                from_block=from_block,
                to_block=to_block,
                addresses=addresses,
                topic0=topic0,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001
            if not self._is_result_too_large_error(exc):
                raise
            if from_block >= to_block:
                if addresses is None:
                    receipt_logs = self.get_logs_from_block_receipts(
                        block_number=from_block,
                        topic0=topic0,
                    )
                    if receipt_logs is not None:
                        logger.warning(
                            "EVM eth_getLogs 单块结果超限，已改用整块回执本地过滤",
                            chain=self.chain.code,
                            block=from_block,
                            matched_logs=len(receipt_logs),
                        )
                        return receipt_logs
                raise EvmScannerRpcError(
                    self._format_rpc_error(
                        f"{summary}（单块日志数超过节点上限，无法继续二分）",
                        method="eth_getLogs",
                        exc=exc,
                        context=f"from={from_block} to={to_block}",
                    )
                ) from exc

        mid_block = (from_block + to_block) // 2
        logger.warning(
            "EVM eth_getLogs 结果超限，按块二分重试",
            chain=self.chain.code,
            from_block=from_block,
            to_block=to_block,
            mid_block=mid_block,
        )
        lower = self._get_logs_chunk(
            from_block=from_block,
            to_block=mid_block,
            addresses=addresses,
            topic0=topic0,
            summary=summary,
        )
        upper = self._get_logs_chunk(
            from_block=mid_block + 1,
            to_block=to_block,
            addresses=addresses,
            topic0=topic0,
            summary=summary,
        )
        return lower + upper

    def _get_logs_single_range(
        self,
        *,
        from_block: int,
        to_block: int,
        addresses: list[str] | None,
        topic0: str | list[str],
        summary: str,
    ) -> list[dict[str, Any]]:
        filter_params: dict[str, Any] = {
            "fromBlock": from_block,
            "toBlock": to_block,
            "topics": [topic0],
        }
        if addresses is not None:
            filter_params["address"] = addresses
        return list(
            self._call_with_retry(
                fn=lambda: self.chain.w3.eth.get_logs(filter_params),  # noqa: SLF001
                summary=summary,
                method="eth_getLogs",
                context=f"from={from_block} to={to_block}",
                non_retriable_predicate=self._is_result_too_large_error,
            )
        )

    @staticmethod
    def _is_result_too_large_error(exc: Exception) -> bool:
        """判断 eth_getLogs 错误是否为"返回结果过多 / 查询范围过大"类的确定性限制。

        只匹配明确指向"结果集/区块范围过大"的措辞——这类靠二分区间可解。故意不匹配
        泛化的"limit exceeded / -32005"，因其常是按请求频率的限流（rate limit），
        对其二分只会发出更多请求、无助于缩小结果集，应交由退避重试而非二分处理。
        """
        msg = str(exc).lower()
        return (
            "returned more than"
            in msg  # Geth/Infura: query returned more than N results
            or "logs matched by the query exceeds" in msg
            or "response size exceed" in msg  # Alchemy: response size exceeded
            or "response is too large" in msg
            or "query timeout exceeded" in msg  # 范围过大导致节点侧超时
            or "block range" in msg  # ...too wide / is too large / exceeds limit
            or "range too large" in msg
            or "too many results" in msg
        )

    def get_block_timestamp(self, *, block_number: int) -> int:
        block = self._call_with_retry(
            fn=lambda: self._get_block_with_poa_retry(
                block_number=block_number,
                full_transactions=False,
            ),
            summary="获取区块时间失败",
            method="eth_getBlockByNumber",
            context=f"block={block_number}",
        )
        return int(block["timestamp"])

    def get_block_receipts(self, *, block_number: int) -> dict[str, dict] | None:
        """整块拉取所有交易 receipt，返回 hash -> receipt 映射。"""
        if self._block_receipts_supported is False:
            return None
        try:
            receipts = self._call_with_retry(
                fn=lambda: self.chain.w3.eth.get_block_receipts(
                    block_number
                ),  # noqa: SLF001
                summary="获取整块 receipt 失败",
                method="eth_getBlockReceipts",
                context=f"block={block_number}",
                non_retriable_predicate=self._is_method_unavailable_error,
            )
        except EvmScannerRpcError:
            raise
        except Exception as exc:  # noqa: BLE001
            if self._is_method_unavailable_error(exc):
                self._block_receipts_supported = False
                return None
            raise

        self._block_receipts_supported = True
        result: dict[str, dict] = {}
        for item in receipts or []:
            tx_hash = prefixed_hex(item.get("transactionHash"))
            if tx_hash:
                result[tx_hash] = dict(item)
        return result

    def get_logs_from_block_receipts(
        self,
        *,
        block_number: int,
        topic0: str | list[str],
    ) -> list[dict[str, Any]] | None:
        """从整块回执中抽取 topic0 命中的日志，作为单块 eth_getLogs 超限时的等价替代。

        回执按交易顺序返回、每笔交易内日志按 logIndex 升序，故结果与 eth_getLogs
        的块内顺序一致；失败交易的回执不带日志，天然被排除。节点不支持
        eth_getBlockReceipts 时返回 None，由调用方决定是否上抛。
        """
        receipts = self.get_block_receipts(block_number=block_number)
        if receipts is None:
            return None
        wanted_topics = {
            prefixed_hex(topic)
            for topic in (topic0 if isinstance(topic0, list) else [topic0])
        }
        logs: list[dict[str, Any]] = []
        for receipt in receipts.values():
            for log in receipt.get("logs") or []:
                topics = list(log.get("topics") or [])
                if topics and prefixed_hex(topics[0]) in wanted_topics:
                    logs.append(dict(log))
        return logs

    @staticmethod
    def _is_method_unavailable_error(exc: Exception) -> bool:
        # 不同 EVM 客户端 / 节点商对"方法不存在"的措辞各异，统一靠 lowercase 关键词匹配。
        msg = str(exc).lower()
        return (
            "method not found" in msg
            or "method not supported" in msg
            or "does not exist" in msg
            or "not available" in msg
            or "-32601" in msg
        )

    def get_transaction(self, *, tx_hash: str) -> dict[str, Any] | None:
        tx: dict[str, Any] | None = self._call_with_retry(
            fn=lambda: self.chain.w3.eth.get_transaction(tx_hash),  # noqa: SLF001
            summary="获取交易详情失败",
            method="eth_getTransactionByHash",
            context=f"tx_hash={tx_hash}",
        )
        return dict(tx) if tx is not None else None

    def get_transaction_receipt(self, *, tx_hash: str) -> dict[str, Any] | None:
        receipt: dict[str, Any] | None = self._call_with_retry(
            fn=lambda: self.chain.w3.eth.get_transaction_receipt(
                tx_hash
            ),  # noqa: SLF001
            summary="获取交易回执失败",
            method="eth_getTransactionReceipt",
            context=f"tx_hash={tx_hash}",
        )
        return dict(receipt) if receipt is not None else None

    def _call_with_retry(
        self,
        *,
        fn: Callable[[], Any],
        summary: str,
        method: str,
        context: str = "",
        non_retriable_predicate: Callable[[Exception], bool] | None = None,
    ) -> Any:
        """对单次 RPC 调用做"指数退避重试 + 统一错误包装"。

        重试时长：_EVM_RPC_RETRY_BACKOFF_SECONDS（默认 200ms、800ms 两档），
        最多尝试 3 次（初次 + 2 次退避重试）；非瞬时错误（如 method unavailable）
        通过 non_retriable_predicate 立即上抛，由调用方负责语义化处理，
        避免在已知"必败"的错误上浪费 1s 的等待开销。
        """
        max_attempts = len(_EVM_RPC_RETRY_BACKOFF_SECONDS) + 1
        for attempt in range(max_attempts):
            try:
                return fn()
            except SoftTimeLimitExceeded:
                # 软超时必须原样立即穿透。它继承自 Exception，若落进下面的通用分支，
                # 会被当成一次普通 RPC 故障：先 sleep 退避、再重试，最后包成
                # EvmScannerRpcError——而调用方 _scan_evm_chain 对该错误只打 warning
                # 并继续跑确认调度。结果是已经到达 40s 软限制的任务被一路拖到 50s 硬
                # 超时，那是 SIGKILL：任务的 finally（mark_scanned）与本段游标提交都
                # 来不及执行，还连带触发 prefork 子进程重建。
                raise
            except Exception as exc:  # noqa: BLE001
                if non_retriable_predicate is not None and non_retriable_predicate(exc):
                    raise
                # 最后一次尝试仍失败：此分支 exc 必为已捕获异常，直接包装上抛，
                # 省去 Exception | None 的可空中间变量，类型与控制流都更清晰。
                if attempt == max_attempts - 1:
                    raise EvmScannerRpcError(
                        self._format_rpc_error(
                            summary,
                            method=method,
                            exc=exc,
                            context=context,
                        )
                    ) from exc
                backoff_seconds = _EVM_RPC_RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "EVM RPC 调用失败，准备重试",
                    chain=self.chain.code,
                    method=method,
                    attempt=attempt + 1,
                    backoff_seconds=backoff_seconds,
                    error=str(exc),
                )
                time.sleep(backoff_seconds)
        return None

    def _get_block_with_poa_retry(
        self,
        *,
        block_number: int,
        full_transactions: bool,
    ) -> Any:
        try:
            return self.chain.w3.eth.get_block(
                block_number,
                full_transactions=full_transactions,
            )  # noqa: SLF001
        except ExtraDataLengthError:
            # is_poa 现由 chains.constants 单一事实源持有，正常路径不会触发兜底；
            # 新接入链若未及时打 POA 标记，这里仅做即时降级，不再回写 DB。
            retry_w3 = self._build_poa_retry_w3()
            return retry_w3.eth.get_block(
                block_number,
                full_transactions=full_transactions,
            )

    def _build_poa_retry_w3(self) -> Web3:
        # 复用链模型的构造逻辑，保证超时与重试策略与常规连接一致。
        w3 = self.chain._build_w3(force_poa=True)  # noqa: SLF001
        self.chain.__dict__["w3"] = w3
        return w3

    def _format_rpc_error(
        self,
        summary: str,
        *,
        method: str,
        exc: Exception,
        context: str = "",
    ) -> str:
        raw_error = self._format_raw_exception(exc)
        parts = [
            f"{summary}: rpc={method}",
            f"error={exc.__class__.__name__}: {raw_error}",
            f"chain={self.chain.code}",
        ]
        if context:
            parts.append(context)
        return " ".join(parts)

    @staticmethod
    def _format_raw_exception(exc: Exception) -> str:
        raw_error = " ".join(str(exc).split())
        if not raw_error:
            raw_error = repr(exc)
        return raw_error
