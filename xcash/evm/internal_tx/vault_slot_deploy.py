from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import structlog
from web3 import Web3

from chains.constants import EVM_UNKNOWN_SOURCE_ADDRESS
from chains.models import Chain
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TxTask
from chains.models import VaultSlot
from chains.models import VaultSlotUsage
from chains.service import MAX_TRANSFER_VALUE
from chains.service import ObservedTransferPayload
from chains.service import TransferService

logger = structlog.get_logger()

VAULT_SLOT_INITIAL_NATIVE_BALANCE_SOURCE = "evm-vault-slot-initial-native-balance"
XCASH_VAULT_SLOT_DEPLOYED_TOPIC0 = Web3.keccak(
    text="XcashVaultSlotDeployed(address,address,bytes32,uint256)"
).hex()


@dataclass(frozen=True)
class VaultSlotDeployInitialNativeBalanceLog:
    slot_address: str
    vault_address: str
    salt: bytes
    initial_native_balance: Decimal
    event_index: int


def process_vault_slot_initial_native_balance(
    *,
    chain: Chain,
    tx_task: TxTask,
    tx_hash: str,
    receipt: dict,
) -> Transfer | None:
    slot = (
        VaultSlot.objects.select_related("project", "chain")
        .filter(
            chain=chain,
            deploy_tx_task=tx_task,
            usage__in=[VaultSlotUsage.DEPOSIT, VaultSlotUsage.INVOICE],
        )
        .first()
    )
    if slot is None:
        return None

    balance_log = find_initial_native_balance_log(
        chain=chain,
        slot=slot,
        receipt=receipt,
    )
    if balance_log is None or balance_log.initial_native_balance <= 0:
        return None
    if balance_log.initial_native_balance > MAX_TRANSFER_VALUE:
        logger.warning(
            "EVM VaultSlot 部署初始原生币余额超过 Transfer.value 范围，已跳过",
            chain=chain.code,
            tx_hash=tx_hash,
            vault_slot_id=slot.pk,
            value=str(balance_log.initial_native_balance),
        )
        return None

    block_number = parse_int(receipt["blockNumber"])
    block_hash = normalize_required_hash(receipt["blockHash"])
    payment_datetime = tx_task.created_at
    timestamp = int(payment_datetime.timestamp())
    native_crypto = chain.native_coin

    result = TransferService.create_observed_transfer(
        observed=ObservedTransferPayload(
            chain=chain,
            block=block_number,
            block_hash=block_hash,
            tx_hash=tx_hash,
            event_index=balance_log.event_index,
            from_address=EVM_UNKNOWN_SOURCE_ADDRESS,
            to_address=balance_log.slot_address,
            crypto=native_crypto,
            value=balance_log.initial_native_balance,
            amount=balance_log.initial_native_balance.scaleb(
                -native_crypto.get_decimals(chain)
            ),
            timestamp=timestamp,
            datetime=payment_datetime,
            source=VAULT_SLOT_INITIAL_NATIVE_BALANCE_SOURCE,
        )
    )
    if result.conflict:
        logger.warning(
            "EVM VaultSlot 部署初始原生币余额落库存在冲突",
            chain=chain.code,
            tx_hash=tx_hash,
            event_index=balance_log.event_index,
            vault_slot_id=slot.pk,
            value=str(balance_log.initial_native_balance),
        )
    return result.transfer


def confirm_initial_native_balance_transfer(transfer: Transfer | None) -> None:
    if transfer is None:
        return
    transfer.process()
    transfer.refresh_from_db()
    if transfer.status == TransferStatus.CONFIRMED:
        return
    transfer.confirm()


def find_initial_native_balance_log(
    *,
    chain: Chain,
    slot: VaultSlot,
    receipt: dict,
) -> VaultSlotDeployInitialNativeBalanceLog | None:
    factory_address = Web3.to_checksum_address(
        chain.vault_slot_contract_addresses().factory
    )
    expected_slot = Web3.to_checksum_address(slot.address)
    expected_vault_raw = slot.project.vault_address_for_chain_type(chain.type)
    if not expected_vault_raw:
        return None
    expected_vault = Web3.to_checksum_address(expected_vault_raw)
    expected_salt = bytes(slot.salt)

    for event_index, log in enumerate(receipt.get("logs") or []):
        try:
            log_address = Web3.to_checksum_address(str(log.get("address") or ""))
        except ValueError:
            continue
        if log_address != factory_address:
            continue

        topics = list(log.get("topics") or [])
        if len(topics) < 4:
            continue
        if hex_lower(topics[0]) != hex_lower(XCASH_VAULT_SLOT_DEPLOYED_TOPIC0):
            continue

        try:
            slot_address = topic_to_address(topics[1])
            vault_address = topic_to_address(topics[2])
            salt = bytes.fromhex(hex_lower(topics[3])[-64:])
            initial_native_balance = Decimal(int(hex_lower(log.get("data", "0x0")), 16))
        except (TypeError, ValueError, OverflowError) as exc:
            logger.warning(
                "EVM VaultSlot 部署事件解析失败，已跳过",
                chain=chain.code,
                vault_slot_id=slot.pk,
                error=str(exc),
            )
            continue

        if (
            slot_address != expected_slot
            or vault_address != expected_vault
            or salt != expected_salt
        ):
            continue

        return VaultSlotDeployInitialNativeBalanceLog(
            slot_address=slot_address,
            vault_address=vault_address,
            salt=salt,
            initial_native_balance=initial_native_balance,
            event_index=event_index,
        )
    return None


def topic_to_address(topic: Any) -> str:
    return Web3.to_checksum_address(f"0x{hex_lower(topic)[-40:]}")


def hex_lower(value: Any) -> str:
    raw = value.hex() if hasattr(value, "hex") else str(value)
    return raw.removeprefix("0x").lower()


def normalize_required_hash(value: object) -> str:
    raw = hex_lower(value)
    if not raw:
        raise ValueError("hash is empty")
    return f"0x{raw.lower()}"


def parse_int(raw_value: Any) -> int:
    if isinstance(raw_value, int):
        return raw_value
    value = str(raw_value).strip()
    if value.startswith(("0x", "0X")):
        return int(value, 16)
    return int(value) if value else 0
