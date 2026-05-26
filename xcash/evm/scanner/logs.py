from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from evm.models import EvmScanCursor
from evm.scanner.constants import DEFAULT_DEPOSIT_LOG_SCAN_BATCH_SIZE
from evm.scanner.constants import DEFAULT_DEPOSIT_LOG_SCAN_REPLAY_BLOCKS
from evm.scanner.constants import ERC20_TRANSFER_TOPIC0
from evm.scanner.constants import XCASH_COLLECTED_TOPIC0
from evm.scanner.constants import XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0
from evm.scanner.constants import XCASH_NATIVE_DEPOSITED_TOPIC0
from evm.scanner.internal_events import EvmContractEventObserver
from evm.scanner.observed_transfers import EvmObservedTransferProcessor
from evm.scanner.rpc import EvmScannerRpcClient
from evm.scanner.rpc import EvmScannerRpcError
from evm.scanner.watchers import EvmWatchSet
from evm.scanner.watchers import load_watch_set

logger = structlog.get_logger()


@dataclass(frozen=True)
class EvmLogKindResult:
    """描述同一次 EVM 日志扫描中某类入账日志的处理结果。"""

    from_block: int
    to_block: int
    latest_block: int
    observed_logs: int
    created_transfers: int


@dataclass(frozen=True)
class EvmLogScanResult:
    """描述一次统一 EVM 日志扫描的结果。"""

    native: EvmLogKindResult
    erc20: EvmLogKindResult


@dataclass(frozen=True)
class EvmLogRangeResult:
    """描述一次不推进游标的区间扫描结果。"""

    raw_logs: list[dict[str, Any]]
    native_observed: int
    erc20_observed: int
    native_created: int
    erc20_created: int

    def __iter__(self):
        yield self.raw_logs
        yield self.native_observed
        yield self.erc20_observed
        yield self.native_created
        yield self.erc20_created


class EvmLogScanner:
    """按链统一扫描 EVM 入账日志与系统合约生命周期事件。"""

    @classmethod
    def scan_chain(
        cls,
        *,
        chain: Chain,
        batch_size: int = DEFAULT_DEPOSIT_LOG_SCAN_BATCH_SIZE,
        rpc_client: EvmScannerRpcClient | None = None,
    ) -> EvmLogScanResult:
        if chain.type != ChainType.EVM:
            raise ValueError(f"仅支持 EVM 链扫描，当前链为 {chain.code}")

        cursor = cls._get_or_create_cursor(chain=chain)
        if not cursor.enabled:
            return cls._empty_result(chain=chain)
        rpc_client = rpc_client or EvmScannerRpcClient(chain=chain)

        try:
            latest_block = rpc_client.get_latest_block_number()
            Chain.objects.filter(pk=chain.pk).update(
                latest_block_number=Greatest(F("latest_block_number"), latest_block)
            )

            watch_set = load_watch_set(chain=chain)
            if not watch_set.watched_addresses:
                cls._advance_cursor(cursor=cursor, scanned_to_block=latest_block)
                return cls._result_for_window(
                    from_block=0,
                    to_block=0,
                    latest_block=latest_block,
                )

            from_block, to_block = cls._compute_scan_window(
                cursor=cursor,
                latest_block=latest_block,
                batch_size=batch_size,
                replay_blocks=cls._replay_blocks_for_chain(chain=chain),
            )
            if from_block > to_block:
                cls._mark_cursor_idle(cursor=cursor)
                return cls._result_for_window(
                    from_block=from_block,
                    to_block=to_block,
                    latest_block=latest_block,
                )

            range_result = cls.scan_range_without_cursor(
                chain=chain,
                rpc_client=rpc_client,
                watch_set=watch_set,
                from_block=from_block,
                to_block=to_block,
            )
        except EvmScannerRpcError as exc:
            cls._mark_cursor_error(cursor=cursor, exc=exc)
            raise

        cls._advance_cursor(cursor=cursor, scanned_to_block=to_block)
        return cls._result_for_window(
            from_block=from_block,
            to_block=to_block,
            latest_block=latest_block,
            native_observed=range_result.native_observed,
            native_created=range_result.native_created,
            erc20_observed=range_result.erc20_observed,
            erc20_created=range_result.erc20_created,
        )

    @classmethod
    def scan_range_without_cursor(
        cls,
        *,
        chain: Chain,
        rpc_client: EvmScannerRpcClient,
        watch_set: EvmWatchSet,
        from_block: int,
        to_block: int,
    ) -> EvmLogRangeResult:
        """对 [from_block, to_block] 区间拉取一次日志并按类型落库。"""
        if from_block > to_block or not watch_set.watched_addresses:
            return cls._range_result(raw_logs=[])

        logs = cls._fetch_logs(
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=from_block,
            to_block=to_block,
        )
        return cls._process_logs(
            chain=chain,
            logs=logs,
            rpc_client=rpc_client,
            watch_set=watch_set,
            from_block=from_block,
            to_block=to_block,
        )

    @classmethod
    def _process_logs(
        cls,
        *,
        chain: Chain,
        logs: list[dict[str, Any]],
        rpc_client: EvmScannerRpcClient,
        watch_set: EvmWatchSet,
        from_block: int,
        to_block: int,
    ) -> EvmLogRangeResult:
        internal_tx_hashes = EvmContractEventObserver.observe_logs(
            chain=chain,
            logs=logs,
            rpc_client=rpc_client,
        )
        transfer_result = EvmObservedTransferProcessor.process(
            chain=chain,
            rpc_client=rpc_client,
            raw_logs=logs,
            watch_set=watch_set,
            ignored_tx_hashes=internal_tx_hashes,
            from_block=from_block,
            to_block=to_block,
        )
        return cls._range_result(
            raw_logs=logs,
            native_observed=transfer_result.native_observed,
            erc20_observed=transfer_result.erc20_observed,
            native_created=transfer_result.native_created,
            erc20_created=transfer_result.erc20_created,
        )

    @classmethod
    def _fetch_logs(
        cls,
        *,
        rpc_client: EvmScannerRpcClient,
        watch_set: EvmWatchSet,
        from_block: int,
        to_block: int,
    ) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        logs.extend(
            rpc_client.get_logs(
                from_block=from_block,
                to_block=to_block,
                addresses=None,
                topic0=[
                    XCASH_NATIVE_DEPOSITED_TOPIC0,
                    XCASH_COLLECTED_TOPIC0,
                    XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0,
                ],
                summary="获取 EVM Xcash 合约日志失败",
            )
        )
        erc20_addresses = cls._erc20_log_filter_addresses(watch_set=watch_set)
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
    def _erc20_log_filter_addresses(*, watch_set: EvmWatchSet) -> list[str]:
        return sorted(watch_set.tokens_by_address.keys())

    @classmethod
    def _get_or_create_cursor(cls, *, chain: Chain) -> EvmScanCursor:
        with transaction.atomic():
            cursor, _ = EvmScanCursor.objects.select_for_update().get_or_create(
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
        replay_blocks: int = DEFAULT_DEPOSIT_LOG_SCAN_REPLAY_BLOCKS,
    ) -> tuple[int, int]:
        if latest_block <= 0:
            return 0, -1

        replay_blocks = max(0, replay_blocks)
        if cursor.last_scanned_block <= 0:
            from_block = 1
        else:
            from_block = max(1, cursor.last_scanned_block + 1 - replay_blocks)

        forward_batch_size = max(1, batch_size)
        if cursor.last_scanned_block > 0:
            to_block = min(latest_block, cursor.last_scanned_block + forward_batch_size)
        else:
            to_block = min(latest_block, from_block + forward_batch_size - 1)
        return from_block, to_block

    @staticmethod
    def _replay_blocks_for_chain(*, chain: Chain) -> int:
        return max(
            DEFAULT_DEPOSIT_LOG_SCAN_REPLAY_BLOCKS,
            int(chain.confirm_block_count or 0),
        )

    @staticmethod
    def _mark_cursor_idle(*, cursor: EvmScanCursor) -> None:
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _advance_cursor(*, cursor: EvmScanCursor, scanned_to_block: int) -> None:
        EvmScanCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=Greatest(F("last_scanned_block"), scanned_to_block),
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def _mark_cursor_error(*, cursor: EvmScanCursor, exc: Exception) -> None:
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

    @staticmethod
    def _range_result(
        *,
        raw_logs: list[dict[str, Any]],
        native_observed: int = 0,
        erc20_observed: int = 0,
        native_created: int = 0,
        erc20_created: int = 0,
    ) -> EvmLogRangeResult:
        return EvmLogRangeResult(
            raw_logs=raw_logs,
            native_observed=native_observed,
            erc20_observed=erc20_observed,
            native_created=native_created,
            erc20_created=erc20_created,
        )

    @staticmethod
    def _empty_result(*, chain: Chain) -> EvmLogScanResult:
        return EvmLogScanner._result_for_window(
            from_block=0,
            to_block=0,
            latest_block=chain.latest_block_number,
        )

    @staticmethod
    def _result_for_window(
        *,
        from_block: int,
        to_block: int,
        latest_block: int,
        native_observed: int = 0,
        native_created: int = 0,
        erc20_observed: int = 0,
        erc20_created: int = 0,
    ) -> EvmLogScanResult:
        return EvmLogScanResult(
            native=EvmLogKindResult(
                from_block=from_block,
                to_block=to_block,
                latest_block=latest_block,
                observed_logs=native_observed,
                created_transfers=native_created,
            ),
            erc20=EvmLogKindResult(
                from_block=from_block,
                to_block=to_block,
                latest_block=latest_block,
                observed_logs=erc20_observed,
                created_transfers=erc20_created,
            ),
        )
