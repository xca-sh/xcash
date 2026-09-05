from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

import structlog
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import Error as DatabaseLayerError
from django.db import transaction
from django.db.models import F
from django.db.models.functions import Greatest
from django.utils import timezone
from eth_utils import keccak
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
# TRC20 走整块 receipt 拉取（每块 1 次，与代币数无关），命中事件的块再补 1 次 blockID，
# batch=32 时单 tick 最坏约 65 次 RPC，符合 TronGrid 限速。
DEFAULT_TRON_SCAN_BATCH_SIZE = 32

# TRC20 与原生扫描均走 walletsolidity 数据面，与固化头同源，已无 /v1 事件索引器
# 滞后问题。保留少量 lag 是防 TronGrid 负载均衡下不同节点固化头的微小偏差：
# getnowblock 命中的节点可能领先 gettransactioninfobyblocknum 命中的节点，
# 后者对"尚未固化的块"返回空列表，与空块不可区分。
DEFAULT_TRON_SCAN_SAFE_LAG_BLOCKS = 1

# TRC20 Transfer(address,address,uint256) 事件签名主题（64 位小写 hex，无 0x 前缀），
# 用于从 raw TVM log 自行识别 Transfer 事件，不依赖事件索引服务的 event_name 过滤。
TRC20_TRANSFER_TOPIC0_HEX = keccak(text="Transfer(address,address,uint256)").hex()


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
    游标。TRC20 逐块经 walletsolidity 读整块交易 receipt 的 raw TVM log、按 topic0 解析
    Transfer 事件（与固化头同一数据面，见 _collect_block_events）。原生 TRX 走
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

            from chains.tasks import dispatch_block_confirmation_checks_if_needed

            # 与 EVM 对齐：刷新链高后先派发已有充值的确认，扫块阶段即使持续软超时，
            # 也不会把确认调度一起跳过。
            dispatch_block_confirmation_checks_if_needed(
                chain=chain,
                previous_latest_block=previous_latest_block,
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
                        cls._persist_observed_transfer_safely(observed=event.observed)
                    blocks_scanned += 1
                    last_successfully_scanned = block_number
        except (TronClientError, SoftTimeLimitExceeded) as exc:
            # 已经成功扫完的块仍需落到游标，避免下一轮重新扫一遍 + 让错误信息可见。
            # 顺序：先 advance（清 last_error），再 mark_error（写新 last_error），
            # 保证 last_error 反映最近一次失败而非被 advance 覆盖；软超时也必须记录，
            # 否则任务 finally 刷新 last_scanned_at 后，外部探针会误报健康。
            cls.flush_scan_progress(
                cursor=cursor,
                latest_block=latest_block,
                scanned_block=last_successfully_scanned,
            )
            cls._mark_cursor_error(cursor=cursor, exc=exc)
            raise
        except BaseException:
            # 其他中断同样必须落盘已完成的进度，避免下一轮从同一起点重来。
            cls.flush_scan_progress(
                cursor=cursor,
                latest_block=latest_block,
                scanned_block=last_successfully_scanned,
            )
            raise
        else:
            cls.flush_scan_progress(
                cursor=cursor,
                latest_block=latest_block,
                scanned_block=last_successfully_scanned,
            )
            if cursor.enabled and last_successfully_scanned is None:
                # RPC 已恢复但没有新块时，本轮同样成功；不能等下一次游标推进才解除
                # 故障探针，否则会把已恢复的节点继续报成失败。
                TronWatchCursor.objects.filter(pk=cursor.pk).update(
                    last_error="",
                    last_error_at=None,
                    updated_at=timezone.now(),
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
        """加载本链已激活的 TRC20 合约集合，按合约地址索引。

        原生 TRX 由 _load_native_on_chain 单独加载；合约资产即使只配置一个 USDT
        也照常走全量查询，避免把 symbol/地址写死，后续接入其它 TRC20 无需改扫描器。
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
        """逐块从 walletsolidity 读全部交易 receipt 的 raw log，解析注册代币的 Transfer。

        与固化头同一数据面：块已固化则 receipt 必可读，不存在 /v1 事件索引器
        滞后导致"未索引被当作无事件"而漏账的问题。每块一次整块 receipt 拉取，
        成本与代币数量无关。
        """
        if not tokens_by_address:
            return []
        infos = client.get_transaction_infos_by_block(block_number=block_number)
        if not infos:
            # 空 infos 有两种成因，且无法从响应本身区分：真·空块，或本次命中的
            # TronGrid 后端节点固化头落后、该块尚未在此节点固化（对未固化块同样返回
            # 空列表）。若直接按空块推进游标，会越过一个可能尚未固化的块，导致该块内
            # 的 TRC20 入账永久静默漏账。这里补一次固化块存在性校验：blockID 可读即为
            # 真·空块、可安全跳过；块尚未固化时 get_solid_block_id 抛 TronClientError
            # 中断本轮，游标停在该块之前，由下一轮重扫。Tron 主网几乎无空块，此额外
            # RPC 在正常路径基本不触发。
            client.get_solid_block_id(block_number=block_number)
            return []
        candidates: list[ParsedTronTransferEvent] = []
        for info in infos:
            candidates.extend(
                cls._parse_transaction_info_events(
                    chain=chain,
                    info=info,
                    expected_block_number=block_number,
                    tokens_by_address=tokens_by_address,
                )
            )
        matched = cls.filter_matched_events(chain=chain, candidates=candidates)
        if not matched:
            return []
        # TransactionInfo 不携带 blockID，只有确实命中系统收款地址的块才需要补拉。
        # 必须放在地址过滤之后：注册 USDT 后主网几乎每块都有 Transfer 日志，过滤前的
        # candidates 恒为非空，按"有候选就补拉"等于每块都多打一次整块请求。
        block_hash = client.get_solid_block_id(block_number=block_number)
        return [
            ParsedTronTransferEvent(
                observed=replace(event.observed, block_hash=block_hash)
            )
            for event in matched
        ]

    @classmethod
    def _parse_transaction_info_events(
        cls,
        *,
        chain: Chain,
        info: object,
        expected_block_number: int,
        tokens_by_address: dict[str, CryptoOnChain],
    ) -> list[ParsedTronTransferEvent]:
        """单条 TransactionInfo → 命中注册 TRC20 合约的 Transfer 入账候选列表。

        event_index 取 log 在交易全部日志中的序号，与旧 TronGrid 事件接口的
        event_index 语义一致，保证 (chain, hash, event_index) 唯一键在数据面
        切换前后连续、幂等重扫不产生重复行。
        """
        if not isinstance(info, dict):
            return []
        tx_id = str(info.get("id") or "")
        if not tx_id:
            return []
        try:
            block_number = int(info.get("blockNumber") or 0)
            timestamp_ms = int(info.get("blockTimeStamp") or 0)
        except (TypeError, ValueError):
            return []
        if block_number != expected_block_number or timestamp_ms <= 0:
            return []
        # TransactionInfo.result 枚举只有 SUCESS（协议原文拼写，少一个 C）/FAILED
        # 两值，java-tron 按 proto3 语义对成功（默认值）省略该字段；只认 FAILED
        # （或数字枚举 1）为失败，防 include-defaults 网关输出 "SUCESS" 时整链
        # 静默漏账。
        top_level_result = str(info.get("result") or "").upper()
        if top_level_result in ("FAILED", "1"):
            return []
        # revert 的交易不会留下有效事件；以 receipt.result 为准过滤非成功交易。
        receipt = info.get("receipt") or {}
        if isinstance(receipt, dict):
            result = receipt.get("result")
            if result not in (None, "", "SUCCESS"):
                return []
        logs = info.get("log") or []
        if not isinstance(logs, list):
            return []
        events: list[ParsedTronTransferEvent] = []
        for event_index, log in enumerate(logs):
            event = cls._parse_raw_transfer_log(
                chain=chain,
                log=log,
                tx_id=tx_id,
                event_index=event_index,
                block_number=block_number,
                timestamp_ms=timestamp_ms,
                tokens_by_address=tokens_by_address,
            )
            if event is not None:
                events.append(event)
        return events

    @classmethod
    def _parse_raw_transfer_log(
        cls,
        *,
        chain: Chain,
        log: object,
        tx_id: str,
        event_index: int,
        block_number: int,
        timestamp_ms: int,
        tokens_by_address: dict[str, CryptoOnChain],
    ) -> ParsedTronTransferEvent | None:
        """单条 raw TVM log → TRC20 Transfer 入账候选；非 Transfer / 非注册代币跳过。

        log.address 是 20 字节 hex（无 41 前缀）、topics 为 64 位 hex；按 topic0
        自行识别 Transfer 事件。block_hash 由调用方在确认本块有候选后统一回填。
        """
        if not isinstance(log, dict):
            return None
        topics = log.get("topics") or []
        if not isinstance(topics, list) or len(topics) < 3:
            return None
        if cls._normalize_hex(topics[0]) != TRC20_TRANSFER_TOPIC0_HEX:
            return None
        try:
            contract_address = cls._event_address_to_base58(log.get("address"))
        except ValueError:
            return None
        token = tokens_by_address.get(contract_address)
        if token is None:
            return None
        try:
            from_address = cls._event_address_to_base58(topics[1])
            to_address = cls._event_address_to_base58(topics[2])
        except ValueError:
            return None
        data_hex = cls._normalize_hex(log.get("data"))
        if not data_hex:
            return None
        try:
            value = Decimal(int(data_hex, 16))
        except (TypeError, ValueError):
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
                amount=value.scaleb(-token.decimals),
                timestamp=timestamp_ms // 1000,
                datetime=occurred_at,
                block_hash="",
                source="tron-scan",
            )
        )

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
        except SoftTimeLimitExceeded:
            # 软超时继承自 Exception，被下面的逐事件容错分支吞掉会让扫描继续往下跑，
            # 直到硬超时被 SIGKILL——那条路径连 except 都来不及执行，已扫块的进度会
            # 彻底丢失。上抛后交给 scan_chain 的中断分支落盘进度。
            raise
        except DatabaseLayerError:
            # 数据库层异常多为暂时性故障（死锁被牺牲、连接抖动、超时），必须上抛，
            # 让本轮扫描中断、游标停在该块之前，由下一轮重扫幂等恢复；
            # 在这里吞掉会推进游标，把真实入账事件永久静默丢弃。
            # 确定性脏数据（值超界 DataError、唯一键冲突 IntegrityError）已在
            # create_observed_transfer 内部用 savepoint 消化，不会传播到这里。
            raise
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
    def flush_scan_progress(
        cls,
        *,
        cursor: TronWatchCursor,
        latest_block: int,
        scanned_block: int | None,
    ) -> None:
        """把本轮已成功扫完的最高块落到游标；一块都没扫成时保持游标不动。

        正常结束、RPC 失败、被中断（软超时/进程回收）三条路径共用，确保任何退出方式
        都不会丢弃已完成的扫描进度。
        """
        if scanned_block is None:
            return
        cls._advance_cursor(
            cursor=cursor,
            latest_block=latest_block,
            scanned_block=scanned_block,
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
