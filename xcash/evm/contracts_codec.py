"""EVM XcashDeposit slot init_code 编码模块。"""

from __future__ import annotations

from eth_utils import keccak
from eth_utils import to_canonical_address
from eth_utils import to_checksum_address

from evm.constants import XCASH_DEPOSIT_FACTORY_ADDRESS
from evm.constants import XCASH_DEPOSIT_TEMPLATE_ADDRESS

ZERO_ADDRESS: bytes = b"\x00" * 20
OZ_CONTRACTS_VERSION = "v5.6.1"
_OZ_CLONE_IMMUTABLE_ARGS_MAX_LENGTH = 0x5FD3
_OZ_CLONE_IMMUTABLE_ARGS_RUNTIME_LENGTH = 0x2D


def build_xcash_deposit_slot_init_code(
    *,
    deposit_template: str,
    vault: str,
) -> bytes:
    """按 OpenZeppelin Contracts v5.6.1 Clones immutable args 构造 slot init_code。"""
    deposit_template_bytes = to_canonical_address(deposit_template)
    vault_bytes = to_canonical_address(vault)
    if deposit_template_bytes == ZERO_ADDRESS:
        raise ValueError("deposit_template address must not be zero")
    if vault_bytes == ZERO_ADDRESS:
        raise ValueError("vault address must not be zero")

    args = vault_bytes
    if len(args) > _OZ_CLONE_IMMUTABLE_ARGS_MAX_LENGTH:
        raise ValueError("slot immutable args too long")

    return b"".join(
        (
            bytes.fromhex("61"),
            (len(args) + _OZ_CLONE_IMMUTABLE_ARGS_RUNTIME_LENGTH).to_bytes(2, "big"),
            bytes.fromhex("3d81600a3d39f3363d3d373d3d3d363d73"),
            deposit_template_bytes,
            bytes.fromhex("5af43d82803e903d91602b57fd5bf3"),
            args,
        )
    )


def predict_xcash_deposit_slot_address(
    *,
    vault: str,
    salt: bytes,
    factory: str = XCASH_DEPOSIT_FACTORY_ADDRESS,
    deposit_template: str = XCASH_DEPOSIT_TEMPLATE_ADDRESS,
) -> str:
    """预测 XcashDepositFactory.deployDepositSlot(vault, salt) 的 slot 地址。"""
    if len(salt) != 32:
        raise ValueError(f"salt must be 32 bytes, got {len(salt)}")

    factory_bytes = to_canonical_address(factory)
    init_code = build_xcash_deposit_slot_init_code(
        deposit_template=deposit_template,
        vault=vault,
    )
    digest = keccak(b"\xff" + factory_bytes + bytes(salt) + keccak(init_code))
    return to_checksum_address(digest[-20:])
