from __future__ import annotations

import binascii
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import eth_abi
from eth_abi.exceptions import DecodingError
from eth_typing import HexStr
from web3 import Web3

from chains.models import Chain
from chains.models import Transfer
from chains.models import TransferType
from chains.models import TxTask
from currencies.models import ChainToken
from currencies.models import Crypto
from evm.internal_tx.facts import MatchedTransferFact
from evm.internal_tx.log_utils import matches_transfer_log
from evm.internal_tx.log_utils import normalize_log_index
from evm.models import VaultSlot

_COLLECT_SELECTOR = "0x06ec16f8"
_XCASH_COLLECTED_TOPIC0 = Web3.keccak(text="XcashCollected(address,uint256)").hex()
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _hex_lower(value: Any) -> str:
    """提取去掉 0x 前缀的小写十六进制串。"""
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return raw.removeprefix("0x").lower()


def _topic_to_address(topic: Any) -> str:
    """从 32 字节 topic 取后 20 字节作为 checksum 地址。"""
    return Web3.to_checksum_address(f"0x{_hex_lower(topic)[-40:]}")


def _decode_collect_token(data: str) -> str | None:
    """从 collect(token) 的 calldata 解析目标代币地址；selector 不匹配返回 None。"""
    raw = data.lower() if data.startswith("0x") else f"0x{data.lower()}"
    if not raw.startswith(_COLLECT_SELECTOR):
        return None
    try:
        (token,) = eth_abi.decode(
            ["address"],
            Web3.to_bytes(hexstr=HexStr(f"0x{raw[10:]}")),
        )
    except (ValueError, binascii.Error, DecodingError):
        return None
    return Web3.to_checksum_address(token)


def _crypto_for_collect_token(*, chain: Chain, token_address: str) -> Crypto | None:
    """零地址映射为原生币，其余按 ChainToken 查 Crypto；未登记返回 None。"""
    if Web3.to_checksum_address(token_address) == Web3.to_checksum_address(
        _ZERO_ADDRESS
    ):
        return chain.native_coin

    chain_token = (
        ChainToken.objects.select_related("crypto")
        .filter(chain=chain, address__iexact=Web3.to_checksum_address(token_address))
        .first()
    )
    return chain_token.crypto if chain_token is not None else None


@dataclass(frozen=True)
class _CollectedLog:
    token_address: str
    value: Decimal
    log_index: int


def _find_collected_log(
    *,
    receipt: dict,
    slot_address: str,
    token_address: str,
) -> _CollectedLog | None:
    """在 receipt 中找到对应 slot 与 token 的 XcashCollected 事件。"""
    for log in receipt.get("logs") or []:
        if Web3.to_checksum_address(str(log.get("address") or "")) != slot_address:
            continue
        topics = list(log.get("topics") or [])
        if len(topics) < 2 or _hex_lower(topics[0]) != _hex_lower(
            _XCASH_COLLECTED_TOPIC0
        ):
            continue
        emitted_token = _topic_to_address(topics[1])
        if emitted_token != token_address:
            continue
        raw_data = _hex_lower(log.get("data", "0x0"))
        if not raw_data:
            return None
        value = Decimal(int(raw_data, 16))
        if value <= 0:
            return None
        return _CollectedLog(
            token_address=emitted_token,
            value=value,
            log_index=normalize_log_index(log.get("logIndex")),
        )
    return None


def vault_slot_collect_matcher(
    *,
    chain: Chain,
    tx_task: TxTask,
    receipt: dict,
    tx: dict | None = None,
) -> MatchedTransferFact | None:
    """从 VaultSlot collect(receipt) 提取归集到 vault 的资产移动事实。"""
    try:
        evm_task = tx_task.evm_task
    except AttributeError:
        return None

    slot_address = Web3.to_checksum_address(evm_task.to)
    slot = (
        VaultSlot.objects.filter(chain=chain, address__iexact=slot_address)
        .only("vault_address")
        .first()
    )
    if slot is None:
        return None

    token_address = _decode_collect_token(evm_task.data)
    if token_address is None:
        return None
    crypto = _crypto_for_collect_token(chain=chain, token_address=token_address)
    if crypto is None:
        return None

    vault_address = Web3.to_checksum_address(slot.vault_address)
    collected_log = _find_collected_log(
        receipt=receipt,
        slot_address=slot_address,
        token_address=token_address,
    )
    if collected_log is None:
        return None

    if crypto != chain.native_coin:
        if not any(
            matches_transfer_log(
                log,
                token=token_address,
                from_address=slot_address,
                to_address=vault_address,
                value=collected_log.value,
            )
            for log in receipt.get("logs") or []
        ):
            return None

    return MatchedTransferFact(
        from_address=slot_address,
        to_address=vault_address,
        crypto=crypto,
        value=collected_log.value,
        amount=collected_log.value.scaleb(-crypto.get_decimals(chain)),
    )


@dataclass
class VaultSlotCollectHandler:
    """VaultSlot 归集任务的生命周期 handler，仅打标无额外业务流转。"""

    def match(self, transfer: Transfer, tx_task: TxTask) -> bool:
        """把 Transfer 类型标记为 Collect。"""
        transfer.type = TransferType.Collect
        transfer.save(update_fields=["type"])
        return True

    def confirm(self, transfer: Transfer) -> None:
        """归集确认不需要额外业务动作。"""

    def drop(self, transfer: Transfer) -> None:
        """reorg 撤销时无须额外回滚。"""

    def finalize_failed(self, tx_task: TxTask) -> None:
        """归集失败无须额外业务收尾。"""


vault_slot_collect_handler = VaultSlotCollectHandler()
