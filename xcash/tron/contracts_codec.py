"""Tron XcashVaultSlot init_code 与 CREATE2 地址预测。

Tron TVM 的 CREATE2 地址 preimage 使用 0x41 前缀；这里刻意不调用链上
EVM-style predict 视图，因为 Solidity 汇编里的常规 CREATE2 预测使用 EVM 0xff。
"""

from __future__ import annotations

from eth_utils import keccak
from tron.codec import TronAddressCodec

from evm.contracts_codec import build_xcash_vault_slot_init_code


def tron_base58_to_evm_address(address: str) -> str:
    """把 Tron Base58/hex41 地址转换为 Solidity ABI 使用的 20 字节 0x 地址。"""
    if TronAddressCodec.is_valid_base58(address):
        hex41 = TronAddressCodec.base58_to_hex41(address)
    else:
        normalized = address.strip().removeprefix("0x").removeprefix("0X").lower()
        if len(normalized) == 40:
            hex41 = f"{TronAddressCodec.ADDRESS_HEX_PREFIX}{normalized}"
        else:
            hex41 = normalized
        TronAddressCodec.hex41_to_base58(hex41)
    return f"0x{hex41[2:]}"


def tron_address_to_20_bytes(address: str) -> bytes:
    return bytes.fromhex(tron_base58_to_evm_address(address)[2:])


def build_tron_vault_slot_init_code(
    *,
    vault_slot_implementation: str,
    vault: str,
) -> bytes:
    """用 Tron 地址构造与 EVM VaultSlot 相同结构的 clone immutable init_code。"""
    return build_xcash_vault_slot_init_code(
        vault_slot_implementation=tron_base58_to_evm_address(vault_slot_implementation),
        vault=tron_base58_to_evm_address(vault),
    )


def predict_tron_vault_slot_address(
    *,
    vault: str,
    salt: bytes,
    factory: str,
    vault_slot_implementation: str,
) -> str:
    """预测 Tron XcashVaultSlotFactory.deployVaultSlot(vault, salt) 的 Base58 地址。"""
    if len(salt) != 32:
        raise ValueError(f"salt must be 32 bytes, got {len(salt)}")

    factory_bytes = tron_address_to_20_bytes(factory)
    init_code = build_tron_vault_slot_init_code(
        vault_slot_implementation=vault_slot_implementation,
        vault=vault,
    )
    digest = keccak(b"\x41" + factory_bytes + bytes(salt) + keccak(init_code))
    return TronAddressCodec.hex41_to_base58(
        f"{TronAddressCodec.ADDRESS_HEX_PREFIX}{digest[-20:].hex()}"
    )
