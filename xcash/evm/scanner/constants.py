from __future__ import annotations

from web3 import Web3

# ERC20 Transfer 事件签名主题，所有日志扫描都依赖这一稳定标识。
ERC20_TRANSFER_TOPIC0 = Web3.to_hex(
    Web3.keccak(text="Transfer(address,address,uint256)")
)

# DepositSlot 原生币充值事件签名主题；log.address 即 DepositSlot 地址。
XCASH_NATIVE_DEPOSITED_TOPIC0 = Web3.to_hex(
    Web3.keccak(text="XcashNativeDeposited(address,uint256)")
)

# DepositSlot 归集事件签名主题；log.address 即 DepositSlot 地址。
XCASH_COLLECTED_TOPIC0 = Web3.to_hex(Web3.keccak(text="XcashCollected(address,uint256)"))

# DepositFactory 部署 DepositSlot 事件签名主题；log.address 即固定 Factory 地址。
XCASH_DEPOSIT_SLOT_DEPLOYED_TOPIC0 = Web3.to_hex(
    Web3.keccak(text="XcashDepositSlotDeployed(address,address,bytes32)")
)

# 单次 EVM 日志扫描默认净推进块数：首版先保守一些，后续可结合链和节点能力再调大。
DEFAULT_DEPOSIT_LOG_SCAN_BATCH_SIZE = 100

# EVM 日志扫描每轮至少复扫的旧块数；实际扫描还会取 max(该值, chain.confirm_block_count)。
DEFAULT_DEPOSIT_LOG_SCAN_REPLAY_BLOCKS = 2
