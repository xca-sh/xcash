# 同一 (address, chain) 同时允许在 mempool 中等待确认的最大交易数。
EVM_PIPELINE_DEPTH = 50

# PENDING_CHAIN 状态的交易超过此时长（秒）仍无 receipt，视为已被 mempool 丢弃并触发重新广播。
EVM_PENDING_REBROADCAST_TIMEOUT = 120


# x402 facilitate（EIP-3009）gas 上限：按 EIP-155 chain_id 配置。
# 主流稳定币 transferWithAuthorization 实测 100k-180k；200k 为覆盖性保守值。
# 未在表中的链触发 ValueError，强制新链接入前补齐配置。
X402_EIP3009_FACILITATE_GAS_LIMIT: dict[int, int] = {
    1: 200_000,        # Ethereum mainnet
    56: 200_000,       # BSC
    137: 200_000,      # Polygon
    8453: 200_000,     # Base
    42161: 250_000,    # Arbitrum One（calldata 计费规则不同，略高）
    10: 200_000,       # Optimism
}


def get_x402_eip3009_facilitate_gas(chain) -> int:
    """按 chain.chain_id 取 x402 EIP-3009 facilitate gas 上限。

    未配置时显式抛错而非走默认值——避免新链上线后 facilitate 任务因 gas 不足
    在链上 OOG 失败。
    """
    try:
        return X402_EIP3009_FACILITATE_GAS_LIMIT[chain.chain_id]
    except KeyError as e:
        raise ValueError(
            f"Chain {chain.code} (chain_id={chain.chain_id}) "
            f"未配置 X402_EIP3009_FACILITATE_GAS_LIMIT；"
            f"在 evm/constants.py 中补齐后再上线 x402 facilitate"
        ) from e
