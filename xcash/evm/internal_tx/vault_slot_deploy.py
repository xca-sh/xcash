from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal

import structlog
from django.db import transaction as db_transaction
from web3 import Web3
from web3.exceptions import TransactionNotFound

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
from evm.hexutil import hex_lower
from evm.hexutil import parse_int
from evm.hexutil import required_hash
from evm.hexutil import topic_to_address

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
    return record_initial_native_balance(
        chain=chain,
        slot=slot,
        tx_hash=tx_hash,
        receipt=receipt,
        payment_datetime=tx_task.created_at,
    )


def record_initial_native_balance(
    *,
    chain: Chain,
    slot: VaultSlot,
    tx_hash: str,
    receipt: dict,
    payment_datetime: datetime,
) -> Transfer | None:
    """把部署事件里的 initialNativeBalance 落成一笔观测入账（部署前打进预测地址的原生币）。

    EVM 原生币只靠 receive() 事件识别；部署前打进来的原生币没有事件，工厂在部署时把
    余额写进 XcashVaultSlotDeployed 事件。按 (tx_hash, event_index) 幂等落库，
    引擎自身部署与商户外部部署共用这一入口。
    """
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
    block_hash = required_hash(receipt["blockHash"])
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


def process_external_deploy_initial_native_balance(
    *, slot: VaultSlot, tx_hash: str
) -> None:
    """商户在系统外部署 VaultSlot 后，补建部署前已打进预测地址的原生币入账。

    外部部署不是引擎的 TxTask，回执处理器不会经过 process_vault_slot_initial_native_balance。
    tx_hash 来自商户前端，不可信：这里自己读回执，只认官方工厂发出、slot / vault / salt
    都与本槽位一致的部署事件（伪造哈希找不到这样的事件，直接忽略）；未达确认数或已脱离
    canonical 链时抛错，交由调用任务退避重试。入账按 (tx_hash, event_index) 幂等。
    """
    if slot.usage not in (VaultSlotUsage.DEPOSIT, VaultSlotUsage.INVOICE):
        return
    chain = slot.chain
    try:
        receipt = chain.w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound as exc:
        raise RuntimeError(f"部署交易回执暂不可用: {tx_hash}") from exc
    if receipt is None:
        raise RuntimeError(f"部署交易回执暂不可用: {tx_hash}")
    receipt = dict(receipt)
    if receipt.get("status") != 1:
        return

    block_number = parse_int(receipt["blockNumber"])
    if block_number > chain.latest_block_number - chain.confirm_block_count:
        raise RuntimeError(f"部署交易确认数不足: {tx_hash}")
    block = chain.w3.eth.get_block(block_number)
    if hex_lower(block.get("hash")) != hex_lower(receipt.get("blockHash")):
        raise RuntimeError(f"部署交易已脱离 canonical 链: {tx_hash}")

    with db_transaction.atomic():
        transfer = record_initial_native_balance(
            chain=chain,
            slot=slot,
            tx_hash=required_hash(receipt["transactionHash"]),
            receipt=receipt,
            payment_datetime=datetime.fromtimestamp(int(block["timestamp"]), UTC),
        )
        confirm_initial_native_balance_transfer(transfer)
