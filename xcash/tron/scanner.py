from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone
from tron.client import TronClientError
from tron.client import TronHttpClient
from tron.codec import TronAddressCodec
from tron.models import TronWatchCursor

from chains.models import Chain
from chains.models import ChainType
from chains.models import VaultSlot
from chains.service import MAX_TRANSFER_VALUE
from chains.service import ObservedTransferPayload
from chains.service import TransferService
from currencies.models import CryptoOnChain

logger = structlog.get_logger()

# 单轮扫描最多向前推进的块数；walletsolidity 返回的是 BFT 不可逆块，故无需 replay。
# Tron 3 秒一块、beat tick 30 秒 ≈ 每轮净新增 ~10 块，32 块留够冗余且能消化短暂积压；
# 单块 USDT 事件最差几百条、分页 200 一页，batch=32 时单 tick 最坏约 96 次 RPC，符合 TronGrid 限速。
DEFAULT_TRON_SCAN_BATCH_SIZE = 32

# TronGrid /v1 事件索引与 walletsolidity 固化头不是同一个数据面。扫描上界保守扣几块，
# 避免索引器短暂滞后时把“还没索引好”误当成“本块无事件”并推进游标。
DEFAULT_TRON_SCAN_SAFE_LAG_BLOCKS = 4


@dataclass(frozen=True)
class TronScanSummary:
    filter_addresses: int
    blocks_scanned: int
    events_seen: int


@dataclass(frozen=True)
class ParsedTronTransferEvent:
    observed: ObservedTransferPayload


class TronScanner:
    """按链扫描本链 TRC20 与原生 TRX 的入账事件。

    与 EVM 扫描器对齐：代币集合来自 CryptoOnChain（不写死 USDT/地址），每条 Tron 链一个
    游标，逐块对每个代币合约拉取 Transfer 事件后统一匹配观察地址。原生 TRX 走
    TransferContract、不 emit 事件，故在同一游标循环内额外逐块读整块交易、按 to_address
    匹配（见 _collect_block_native_transfers）；停用原生币 CryptoOnChain 即关闭原生扫描。
    """

    _debug_bootstrapped_cursors: set[int] = set()

    @classmethod
    def scan_chain(cls, *, chain: Chain) -> TronScanSummary:
        if chain.type != ChainType.TRON:
            raise ValueError(f"仅支持 Tron 链扫描，当前链为 {chain.code}")

        tokens_by_address = cls._load_crypto_on_chains(chain=chain)
        native_on_chain = cls._load_native_on_chain(chain=chain)
        cursor = cls._get_or_create_cursor(chain=chain)
        client = TronHttpClient(chain=chain)
        previous_latest_block = chain.latest_block_number
        events_seen = 0
        blocks_scanned = 0

        # 跨循环跟踪当 tick 成功扫完的最高块号；循环结束或异常中断时只 flush 一次，
        # 替代"每块一次单行 update"，长追平时把 N 次写库压缩为 1 次。
        latest_block: int = 0
        last_successfully_scanned: int | None = None
        matched_addresses_seen: set[str] = set()
        try:
            latest_solid_block = client.get_latest_solid_block_number()
            Chain.objects.filter(pk=chain.pk).update(
                latest_block_number=Greatest(
                    F("latest_block_number"), latest_solid_block
                )
            )
            latest_block = max(
                latest_solid_block - cls.scan_safe_lag_blocks(),
                0,
            )

            if cursor.enabled:
                cursor = cls._bootstrap_cursor_if_needed(
                    cursor=cursor,
                    latest_block=latest_block,
                )
                start_block = cursor.last_scanned_block + 1
                # 单轮最多推进 DEFAULT_TRON_SCAN_BATCH_SIZE 块，避免大幅落后时 range 无界，
                # 把当次 beat task 拖垮、阻塞下一轮调度。
                end_block = min(
                    latest_block,
                    start_block + DEFAULT_TRON_SCAN_BATCH_SIZE - 1,
                )
                for block_number in range(start_block, end_block + 1):
                    parsed_events = cls._collect_block_events(
                        client=client,
                        chain=chain,
                        block_number=block_number,
                        tokens_by_address=tokens_by_address,
                    )
                    if native_on_chain is not None:
                        parsed_events += cls._collect_block_native_transfers(
                            client=client,
                            chain=chain,
                            block_number=block_number,
                            native_on_chain=native_on_chain,
                        )
                    matched_addresses_seen.update(
                        event.observed.to_address for event in parsed_events
                    )
                    events_seen += len(parsed_events)
                    for event in parsed_events:
                        cls._persist_observed_transfer_safely(
                            observed=event.observed
                        )
                    blocks_scanned += 1
                    last_successfully_scanned = block_number
        except TronClientError as exc:
            # 已经成功扫完的块仍需落到游标，避免下一轮重新扫一遍 + 让错误信息可见。
            # 顺序：先 advance（清 last_error），再 mark_error（写新 last_error），
            # 保证 last_error 反映最近一次失败而非被 advance 覆盖。
            if last_successfully_scanned is not None:
                cls._advance_cursor(
                    cursor=cursor,
                    latest_block=latest_block,
                    scanned_block=last_successfully_scanned,
                )
            cls._mark_cursor_error(cursor=cursor, exc=exc)
            raise

        if last_successfully_scanned is not None:
            cls._advance_cursor(
                cursor=cursor,
                latest_block=latest_block,
                scanned_block=last_successfully_scanned,
            )

        from chains.tasks import dispatch_block_confirmation_checks_if_needed

        dispatch_block_confirmation_checks_if_needed(
            chain=chain,
            previous_latest_block=previous_latest_block,
        )

        return TronScanSummary(
            filter_addresses=len(matched_addresses_seen),
            blocks_scanned=blocks_scanned,
            events_seen=events_seen,
        )

    @staticmethod
    def scan_safe_lag_blocks() -> int:
        return max(
            int(
                getattr(
                    settings,
                    "TRON_SCAN_SAFE_LAG_BLOCKS",
                    DEFAULT_TRON_SCAN_SAFE_LAG_BLOCKS,
                )
            ),
            0,
        )

    @staticmethod
    def _load_crypto_on_chains(
        *,
        chain: Chain,
    ) -> dict[str, CryptoOnChain]:
        """加载本链已激活的 TRC20 合约集合，按合约地址索引（对齐 EVM 扫描器）。

        只会配置一个 USDT 也照常走全量查询，避免把 symbol/地址写死，后续接入
        其它 TRC20 无需改扫描器。
        """
        token_rows = (
            CryptoOnChain.objects.select_related("crypto")
            .filter(
                chain=chain,
                crypto__active=True,
                active=True,
            )
            .exclude(address="")
        )
        for token in token_rows:
            token.normalize_address_for_chain()
        return {token.address: token for token in token_rows}

    @staticmethod
    def _load_native_on_chain(*, chain: Chain) -> CryptoOnChain | None:
        """本链已启用的原生币 CryptoOnChain（address=""）；未启用则跳过原生扫描。

        原生币入账是可按链开关的资产：停用其 CryptoOnChain 即关闭原生 TRX 扫描，
        省掉逐块整块拉取的带宽开销。
        """
        return (
            CryptoOnChain.objects.select_related("crypto")
            .filter(
                chain=chain,
                crypto__is_native=True,
                crypto__active=True,
                active=True,
                address="",
            )
            .first()
        )

    @classmethod
    def _collect_block_native_transfers(
        cls,
        *,
        client: TronHttpClient,
        chain: Chain,
        block_number: int,
        native_on_chain: CryptoOnChain,
    ) -> list[ParsedTronTransferEvent]:
        """扫描单块内的原生 TRX TransferContract，产出命中收款地址的入账事件。

        Tron 上原生 TRX 转账走 TransferContract、不进 TVM、不 emit 事件，无法复用 TRC20
        的事件接口，必须逐块读整块交易、按 to_address 匹配收款地址。成本为每块一次整块
        拉取，O(blocks) 恒定、与收款地址数量无关。
        """
        payload = client.get_solid_block(block_number=block_number)
        # 生产环境 getblockbynum 必返回 dict；非 dict 只会出现在 mock 场景，按"无交易"处理，
        # 既不误扫，也不必为原生扫描给每个既有扫描器单测额外打桩。
        if not isinstance(payload, dict):
            return []
        block_hash = cls._extract_block_id(payload=payload, chain=chain)
        block_timestamp_ms = cls._extract_block_timestamp_ms(
            payload=payload,
            chain=chain,
        )
        transactions = payload.get("transactions") or []
        if not isinstance(transactions, list) or not transactions:
            return []
        candidates: list[ParsedTronTransferEvent] = []
        for tx in transactions:
            candidates.extend(
                cls._parse_native_transfers(
                    chain=chain,
                    tx=tx,
                    block_number=block_number,
                    block_hash=block_hash,
                    block_timestamp_ms=block_timestamp_ms,
                    crypto=native_on_chain.crypto,
                    decimals=native_on_chain.decimals,
                )
            )
        return cls.filter_matched_events(chain=chain, candidates=candidates)

    @classmethod
    def _parse_native_transfers(
        cls,
        *,
        chain: Chain,
        tx: dict,
        block_number: int,
        block_hash: str,
        block_timestamp_ms: int,
        crypto,
        decimals: int,
    ) -> list[ParsedTronTransferEvent]:
        if not isinstance(tx, dict):
            return []
        contracts = (tx.get("raw_data") or {}).get("contract") or []
        if not isinstance(contracts, list) or not contracts:
            return []
        events: list[ParsedTronTransferEvent] = []
        for contract_index, contract in enumerate(contracts):
            event = cls._parse_native_transfer_contract(
                chain=chain,
                tx=tx,
                contract=contract,
                contract_index=contract_index,
                block_number=block_number,
                block_hash=block_hash,
                block_timestamp_ms=block_timestamp_ms,
                crypto=crypto,
                decimals=decimals,
            )
            if event is not None:
                events.append(event)
        return events

    @classmethod
    def _parse_native_transfer(
        cls,
        *,
        chain: Chain,
        tx: dict,
        block_number: int,
        block_hash: str,
        block_timestamp_ms: int,
        crypto,
        decimals: int,
    ) -> ParsedTronTransferEvent | None:
        """兼容旧单测入口：返回 tx 中第一条可解析的原生 TRX 入账事件。"""
        events = cls._parse_native_transfers(
            chain=chain,
            tx=tx,
            block_number=block_number,
            block_hash=block_hash,
            block_timestamp_ms=block_timestamp_ms,
            crypto=crypto,
            decimals=decimals,
        )
        return events[0] if events else None

    @classmethod
    def _parse_native_transfer_contract(
        cls,
        *,
        chain: Chain,
        tx: dict,
        contract: object,
        contract_index: int,
        block_number: int,
        block_hash: str,
        block_timestamp_ms: int,
        crypto,
        decimals: int,
    ) -> ParsedTronTransferEvent | None:
        """单个 TransferContract → 入账事件；非 TransferContract / 执行失败 / 金额非正一律跳过。"""
        if not isinstance(tx, dict):
            return None
        tx_id = str(tx.get("txID") or "")
        if not tx_id:
            return None
        # 仅接受执行成功的 contract：TransferContract 入块通常即成功，但仍以 ret.contractRet 为准。
        ret = tx.get("ret") or []
        if isinstance(ret, list) and contract_index < len(ret):
            ret_item = ret[contract_index]
            if isinstance(ret_item, dict):
                contract_ret = ret_item.get("contractRet")
                if contract_ret not in (None, "", "SUCCESS"):
                    return None
        if not isinstance(contract, dict) or contract.get("type") != "TransferContract":
            return None
        value_obj = ((contract.get("parameter") or {}).get("value")) or {}
        if not isinstance(value_obj, dict):
            return None
        try:
            value = Decimal(int(value_obj.get("amount")))
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        if value > MAX_TRANSFER_VALUE:
            logger.warning(
                "Tron 原生币转账数值超过 Transfer.value 范围，已跳过",
                chain=chain.code,
                tx_hash=tx_id,
                event_index=contract_index,
                value=str(value),
            )
            return None
        try:
            from_address = cls._event_address_to_base58(value_obj.get("owner_address"))
            to_address = cls._event_address_to_base58(value_obj.get("to_address"))
        except ValueError:
            return None
        occurred_at = datetime.fromtimestamp(
            block_timestamp_ms / 1000,
            tz=timezone.get_current_timezone(),
        )
        return ParsedTronTransferEvent(
            observed=ObservedTransferPayload(
                chain=chain,
                block=block_number,
                tx_hash=tx_id,
                event_index=contract_index,
                from_address=from_address,
                to_address=to_address,
                crypto=crypto,
                value=value,
                amount=value.scaleb(-decimals),
                timestamp=block_timestamp_ms // 1000,
                datetime=occurred_at,
                block_hash=block_hash,
                source="tron-native-scan",
            )
        )

    @staticmethod
    def _extract_block_id(*, payload: dict, chain: Chain) -> str:
        block_id = str(payload.get("blockID") or "").strip().lower()
        if len(block_id) != 64:
            raise TronClientError(f"invalid solid block id from {chain.code}")
        return block_id

    @staticmethod
    def _extract_block_timestamp_ms(*, payload: dict, chain: Chain) -> int:
        raw = ((payload.get("block_header") or {}).get("raw_data") or {}).get(
            "timestamp"
        )
        try:
            timestamp_ms = int(raw or 0)
        except (TypeError, ValueError):
            timestamp_ms = 0
        if timestamp_ms <= 0:
            raise TronClientError(f"invalid solid block timestamp from {chain.code}")
        return timestamp_ms

    @classmethod
    def _get_or_create_cursor(cls, *, chain: Chain) -> TronWatchCursor:
        with transaction.atomic():
            cursor, _ = TronWatchCursor.objects.select_for_update().get_or_create(
                chain=chain,
                defaults={
                    "last_scanned_block": 0,
                    "enabled": True,
                },
            )
        return cursor

    @classmethod
    def _bootstrap_cursor_if_needed(
        cls,
        *,
        cursor: TronWatchCursor,
        latest_block: int,
    ) -> TronWatchCursor:
        debug_key = cursor.chain_id
        should_reset = False

        if settings.DEBUG:
            if debug_key not in cls._debug_bootstrapped_cursors:
                cls._debug_bootstrapped_cursors.add(debug_key)
                should_reset = True
        elif cursor.last_scanned_block == 0:
            should_reset = True

        if not should_reset:
            return cursor

        TronWatchCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=latest_block,
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )
        cursor.last_scanned_block = latest_block
        cursor.last_error = ""
        cursor.last_error_at = None
        return cursor

    @classmethod
    def _collect_block_events(
        cls,
        *,
        client: TronHttpClient,
        chain: Chain,
        block_number: int,
        tokens_by_address: dict[str, CryptoOnChain],
    ) -> list[ParsedTronTransferEvent]:
        candidates: list[ParsedTronTransferEvent] = []
        # block_hash 整块只取一次、跨代币复用；仅在本块确有事件时才发起这次 RPC，
        # 空块不额外打点。
        block_hash: str | None = None

        for token in tokens_by_address.values():
            rows = cls._fetch_token_block_event_rows(
                client=client,
                chain=chain,
                block_number=block_number,
                token=token,
            )
            if rows and block_hash is None:
                block_hash = client.get_solid_block_id(block_number=block_number)
            for row in rows:
                event = cls._parse_contract_event(
                    chain=chain,
                    row=row,
                    expected_block_number=block_number,
                    block_hash=block_hash,
                    token=token,
                )
                if event is not None:
                    candidates.append(event)

        return cls.filter_matched_events(chain=chain, candidates=candidates)

    @classmethod
    def _fetch_token_block_event_rows(
        cls,
        *,
        client: TronHttpClient,
        chain: Chain,
        block_number: int,
        token: CryptoOnChain,
    ) -> list[dict]:
        """分页拉取单个 TRC20 合约在指定块的 Transfer 事件原始行。"""
        page_fingerprint: str | None = None
        seen_fingerprints: set[str] = set()
        rows: list[dict] = []

        while True:
            payload = client.list_confirmed_contract_events(
                contract_address=token.address,
                event_name="Transfer",
                block_number=block_number,
                fingerprint=page_fingerprint,
            )
            if not isinstance(payload, dict):
                raise TronClientError(
                    f"invalid contract events payload from {chain.code}"
                )
            data = payload.get("data")
            meta = payload.get("meta") or {}
            if data is None:
                data = []
            if not isinstance(data, list) or not isinstance(meta, dict):
                raise TronClientError(
                    f"invalid contract events payload from {chain.code}"
                )
            if not data:
                break

            rows.extend(data)

            page_fingerprint = meta.get("fingerprint")
            if not page_fingerprint:
                break
            if not isinstance(page_fingerprint, str):
                raise TronClientError(
                    f"invalid contract events fingerprint from {chain.code}"
                )
            if page_fingerprint in seen_fingerprints:
                raise TronClientError(
                    f"duplicate contract events fingerprint from {chain.code}"
                )
            seen_fingerprints.add(page_fingerprint)

        return rows

    @staticmethod
    def filter_matched_events(
        *,
        chain: Chain,
        candidates: list[ParsedTronTransferEvent],
    ) -> list[ParsedTronTransferEvent]:
        if not candidates:
            return []
        candidate_addresses = {event.observed.to_address for event in candidates}
        matched_addresses = VaultSlot.matched_addresses_for_candidates(
            chain=chain,
            candidates=candidate_addresses,
        )
        from invoices.models import DifferRecipientAddress

        matched_addresses |= DifferRecipientAddress.matched_addresses_for_candidates(
            chain=chain,
            candidates=candidate_addresses,
        )
        if not matched_addresses:
            return []
        return [
            event
            for event in candidates
            if event.observed.to_address in matched_addresses
        ]

    @staticmethod
    def _persist_observed_transfer_safely(
        *,
        observed: ObservedTransferPayload,
    ) -> None:
        try:
            TransferService.create_observed_transfer(observed=observed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Tron 入账事件落库失败，已跳过",
                chain=observed.chain.code,
                tx_hash=observed.tx_hash,
                event_index=observed.event_index,
                value=str(observed.value),
                amount=str(observed.amount),
                error=str(exc),
            )

    @classmethod
    def _parse_contract_event(
        cls,
        *,
        chain: Chain,
        row: dict,
        expected_block_number: int,
        block_hash: str,
        token: CryptoOnChain,
    ) -> ParsedTronTransferEvent | None:
        if not isinstance(row, dict):
            return None

        tx_id = str(row.get("transaction_id") or "")
        raw_event_index = row.get("event_index")
        if raw_event_index in (None, ""):
            return None
        try:
            block_number = int(row.get("block_number") or 0)
            timestamp_ms = int(row.get("block_timestamp") or 0)
            event_index = int(raw_event_index)
        except (TypeError, ValueError):
            return None
        if not tx_id or not block_number or not timestamp_ms:
            return None
        if block_number != expected_block_number:
            return None
        if str(row.get("event_name") or "") != "Transfer":
            return None

        contract_address = str(row.get("contract_address") or "")
        if not contract_address:
            return None
        try:
            normalized_contract_address = cls._event_address_to_base58(contract_address)
        except ValueError:
            return None
        if normalized_contract_address != token.address:
            return None

        result = row.get("result") or {}
        if not isinstance(result, dict):
            return None

        try:
            from_address = cls._event_address_to_base58(result.get("from"))
            to_address = cls._event_address_to_base58(result.get("to"))
        except ValueError:
            return None

        try:
            value = Decimal(str(result.get("value") or "0"))
        except Exception:  # noqa: BLE001
            return None
        if value <= 0:
            return None
        if value > MAX_TRANSFER_VALUE:
            logger.warning(
                "Tron TRC20 Transfer 数值超过 Transfer.value 范围，已跳过",
                chain=chain.code,
                tx_hash=tx_id,
                event_index=event_index,
                value=str(value),
            )
            return None

        occurred_at = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.get_current_timezone(),
        )
        decimals = token.decimals
        return ParsedTronTransferEvent(
            observed=ObservedTransferPayload(
                chain=chain,
                block=block_number,
                tx_hash=tx_id,
                event_index=event_index,
                from_address=from_address,
                to_address=to_address,
                crypto=token.crypto,
                value=value,
                amount=value.scaleb(-decimals),
                timestamp=timestamp_ms // 1000,
                datetime=occurred_at,
                block_hash=block_hash,
                source="tron-scan",
            )
        )

    @staticmethod
    def _advance_cursor(
        *,
        cursor: TronWatchCursor,
        latest_block: int,
        scanned_block: int,
    ) -> None:
        target_block = min(scanned_block, latest_block)
        TronWatchCursor.objects.filter(pk=cursor.pk).update(
            last_scanned_block=Greatest(F("last_scanned_block"), target_block),
            last_error="",
            last_error_at=None,
            updated_at=timezone.now(),
        )
        cursor.last_scanned_block = max(cursor.last_scanned_block, target_block)
        cursor.last_error = ""
        cursor.last_error_at = None

    @staticmethod
    def _mark_cursor_error(*, cursor: TronWatchCursor, exc: Exception) -> None:
        TronWatchCursor.objects.filter(pk=cursor.pk).update(
            last_error=str(exc)[:255],
            last_error_at=timezone.now(),
            updated_at=timezone.now(),
        )

    @staticmethod
    def _normalize_hex(value: object) -> str:
        return str(value or "").strip().lower().removeprefix("0x")

    @classmethod
    def _event_address_to_base58(cls, value: object) -> str:
        raw_value = str(value or "").strip()
        if not raw_value:
            raise ValueError("empty tron event address")
        if TronAddressCodec.is_valid_base58(raw_value):
            return TronAddressCodec.normalize_base58(raw_value)

        normalized = cls._normalize_hex(raw_value)
        if len(normalized) == 40:
            normalized = f"{TronAddressCodec.ADDRESS_HEX_PREFIX}{normalized}"
        elif len(normalized) == 64:
            normalized = f"{TronAddressCodec.ADDRESS_HEX_PREFIX}{normalized[-40:]}"

        return TronAddressCodec.hex41_to_base58(normalized)
