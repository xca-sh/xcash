from __future__ import annotations

import contextlib
from dataclasses import dataclass

from chains.models import Chain
from chains.models import ChainType
from evm.models import EvmScanCursor
from evm.scanner.logs import EvmLogScanner
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import load_watch_set

RECONCILE_MAX_BLOCK_SPAN = 64


@dataclass(frozen=True)
class EvmReconcileResult:
    """汇总一次对账重扫的产出。"""

    from_block: int
    to_block: int


class EvmScannerService:
    """统一编排一条 EVM 链上的日志扫描流程。"""

    @staticmethod
    def _iter_reconcile_block_ranges(
        block_numbers: set[int],
        *,
        max_span: int = RECONCILE_MAX_BLOCK_SPAN,
    ):
        """把命中块拆成连续且限宽的扫描窗口，避免稀疏块拉成长区间。"""
        if max_span <= 0:
            raise ValueError("max_span 必须大于 0")

        sorted_blocks = sorted(set(block_numbers))
        if not sorted_blocks:
            return

        start = end = sorted_blocks[0]
        for block_number in sorted_blocks[1:]:
            is_contiguous = block_number == end + 1
            exceeds_span = block_number - start + 1 > max_span
            if is_contiguous and not exceeds_span:
                end = block_number
                continue

            yield start, end
            start = end = block_number

        yield start, end

    @staticmethod
    def _is_enabled(*, chain: Chain) -> bool:
        enabled = (
            EvmScanCursor.objects.filter(chain=chain)
            .values_list("enabled", flat=True)
            .first()
        )
        return True if enabled is None else bool(enabled)

    @staticmethod
    def scan_chain(*, chain: Chain) -> None:
        """按链触发一次正向扫描，并吞掉 RPC 异常。"""
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持扫描 EVM 链，当前链为 {chain.name}")

        with contextlib.suppress(EvmScannerRpcError):
            EvmLogScanner.scan_chain(
                chain=chain,
                rpc_client=EvmScannerRpcClient(chain=chain),
            )

    @classmethod
    def reconcile_blocks(
        cls,
        *,
        chain: Chain,
        block_numbers: set[int],
    ) -> EvmReconcileResult:
        """对账：将外部指定的块号回放进外部入账扫描管线，不推进游标。

        典型触发场景是需要对某些块做外部入账复扫。复用 EvmLogScanner.scan_range
        做真值通路，确保 Transfer 落库与 observed_transfer 流转和正常扫描一致。
        """
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持扫描 EVM 链，当前链为 {chain.code}")
        if not block_numbers:
            return EvmReconcileResult(
                from_block=0,
                to_block=-1,
            )

        from_block = min(block_numbers)
        to_block = max(block_numbers)
        if not cls._is_enabled(chain=chain):
            return EvmReconcileResult(
                from_block=from_block,
                to_block=to_block,
            )

        rpc_client = EvmScannerRpcClient(chain=chain)
        watch_set = load_watch_set(chain=chain)

        for range_from_block, range_to_block in cls._iter_reconcile_block_ranges(
            block_numbers
        ):
            EvmLogScanner.scan_range(
                chain=chain,
                rpc_client=rpc_client,
                watch_set=watch_set,
                from_block=range_from_block,
                to_block=range_to_block,
            )

        return EvmReconcileResult(
            from_block=from_block,
            to_block=to_block,
        )
