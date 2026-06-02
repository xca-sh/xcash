DEFAULT_VAULT_SLOT_DEPLOY_GAS = 160_000
DEFAULT_VAULT_SLOT_COLLECT_GAS = 120_000

# 同一 (address, chain) 同时允许在 mempool 中等待确认的最大交易数。
EVM_PIPELINE_DEPTH = 50

# PENDING_CHAIN 状态的交易超过此时长（秒）后开始查 receipt。
# 该值只影响内部交易入账速度，不代表交易已丢失。
EVM_PENDING_RECEIPT_POLL_DELAY = 32

# PENDING_CHAIN 状态的交易超过此时长（秒）仍无 receipt，视为已被 mempool 丢弃并触发重新广播。
EVM_PENDING_REBROADCAST_TIMEOUT = 120

# XcashVaultSlotTemplate / XcashVaultSlotFactory 全网统一地址。
# 通过 Foundry 默认 Arachnid CREATE2 Deployer + salt=keccak256("xcash:vault-slot:v1")
# 部署，所有 EVM 链必须落到同一地址；新链部署走 contracts/scripts/DeployXcashVaultSlot.s.sol，
# 脚本内 EXPECTED_* 常量与下面两个值必须保持同步，任何偏差都会让 require revert。
XCASH_VAULT_SLOT_TEMPLATE_ADDRESS = "0xe9171AAEEaA814354220620839822AB3AB614f52"
XCASH_VAULT_SLOT_FACTORY_ADDRESS = "0x6bB0F2479fF95a6B41456Ac06187522c701B75C6"
