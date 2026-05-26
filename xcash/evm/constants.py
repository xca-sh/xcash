# 同一 (address, chain) 同时允许在 mempool 中等待确认的最大交易数。
EVM_PIPELINE_DEPTH = 50

# PENDING_CHAIN 状态的交易超过此时长（秒）仍无 receipt，视为已被 mempool 丢弃并触发重新广播。
EVM_PENDING_REBROADCAST_TIMEOUT = 120

# XcashDepositTemplate / XcashDepositFactory 全网统一地址。
# 通过 Foundry 默认 Arachnid CREATE2 Deployer + salt=keccak256("xcash:deposit:v1")
# 部署，所有 EVM 链必须落到同一地址；新链部署走 contracts/scripts/DeployXcashDeposit.s.sol，
# 脚本内 EXPECTED_* 常量与下面两个值必须保持同步，任何偏差都会让 require revert。
XCASH_DEPOSIT_TEMPLATE_ADDRESS = "0x3e0Cdb17Bc3E22adF994AC8c0c36083B5f04C408"
XCASH_DEPOSIT_FACTORY_ADDRESS = "0x4652Dc955698Fbe9AFcBBf910B3D30d8Df81bB01"
