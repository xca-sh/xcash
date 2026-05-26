from __future__ import annotations

from dataclasses import dataclass

from chains.models import Chain
from chains.models import ChainType
from evm.models import EvmScanCursor
from evm.scanner.logs import EvmLogKindResult
from evm.scanner.logs import EvmLogScanner
from evm.scanner.logs import EvmLogScanResult
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import load_watch_set

RESCAN_MAX_BLOCK_SPAN = 64


@dataclass(frozen=True)
class EvmRescanResult:
    """汇总一次重扫指定块的产出，供调用方观测命中情况。"""

    from_block: int
    to_block: int
    observed_native: int
    created_native: int
    observed_erc20: int
    created_erc20: int


class EvmChainScannerService:
    """统一编排一条 EVM 链上的日志扫描流程。"""

    @staticmethod
    def _iter_rescan_block_ranges(
        block_numbers: set[int],
        *,
        max_span: int = RESCAN_MAX_BLOCK_SPAN,
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
    def _empty_result(*, chain: Chain) -> EvmLogScanResult:
        empty_kind = EvmLogKindResult(
            from_block=0,
            to_block=0,
            latest_block=chain.latest_block_number,
            observed_logs=0,
            created_transfers=0,
        )
        return EvmLogScanResult(native=empty_kind, erc20=empty_kind)

    @staticmethod
    def scan_chain(*, chain: Chain) -> EvmLogScanResult:
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持扫描 EVM 链，当前链为 {chain.code}")
        if not EvmChainScannerService._is_enabled(chain=chain):
            return EvmChainScannerService._empty_result(chain=chain)

        try:
            return EvmLogScanner.scan_chain(
                chain=chain,
                rpc_client=EvmScannerRpcClient(chain=chain),
            )
        except EvmScannerRpcError:
            return EvmChainScannerService._empty_result(chain=chain)

    @classmethod
    def rescan_blocks(
        cls,
        *,
        chain: Chain,
        block_numbers: set[int],
    ) -> EvmRescanResult:
        """对指定块集合执行一次重扫，不推进任何游标。"""
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持扫描 EVM 链，当前链为 {chain.code}")
        if not block_numbers:
            return EvmRescanResult(
                from_block=0,
                to_block=-1,
                observed_native=0,
                created_native=0,
                observed_erc20=0,
                created_erc20=0,
            )

        from_block = min(block_numbers)
        to_block = max(block_numbers)
        rpc_client = EvmScannerRpcClient(chain=chain)
        watch_set = load_watch_set(chain=chain)
        scanner_enabled = cls._is_enabled(chain=chain)

        observed_native, created_native = 0, 0
        observed_erc20, created_erc20 = 0, 0

        for range_from_block, range_to_block in cls._iter_rescan_block_ranges(
            block_numbers
        ):
            if not scanner_enabled:
                continue
            range_result = EvmLogScanner.scan_range_without_cursor(
                chain=chain,
                rpc_client=rpc_client,
                watch_set=watch_set,
                from_block=range_from_block,
                to_block=range_to_block,
            )
            observed_native += range_result.native_observed
            observed_erc20 += range_result.erc20_observed
            created_native += range_result.native_created
            created_erc20 += range_result.erc20_created

        return EvmRescanResult(
            from_block=from_block,
            to_block=to_block,
            observed_native=observed_native,
            created_native=created_native,
            observed_erc20=observed_erc20,
            created_erc20=created_erc20,
        )
