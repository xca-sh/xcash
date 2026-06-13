"""本地联调：把 XcashVaultSlot 工厂 / 实现合约确定性部署到本地 EVM 链。

生产 / 测试网由 `contracts/scripts/DeployXcashVaultSlot.s.sol` 经 Foundry 部署；
本地 compose 会持久化 anvil state，使系统热钱包 nonce、合约代码和数据库同生命周期。
`ensure_local_chains` 仍在 bootstrap 时用同一套 CREATE2 规则幂等检查并部署工厂 / 实现合约，
覆盖首次启动或显式重建本地链后的初始化，让合约账单 / 充币的 VaultSlot 归集链路
（deployVaultSlot + collect）在本地可用。

部署不经 Foundry，而是直接把 `salt || init_code` 发给链上的 Arachnid CREATE2
Deployer，纯 web3 调用，不依赖宿主安装 forge。init_code 取自合约编译产物
（`contracts/artifacts/*.bin`，单一可信源），落地地址断言等于 `evm.constants`
里的全网统一地址，任何编译产物漂移都会立即抛错。
"""

from __future__ import annotations

from pathlib import Path

import structlog
from eth_utils import keccak
from eth_utils import to_canonical_address
from web3 import Web3

from evm.constants import XCASH_VAULT_SLOT_FACTORY_ADDRESS
from evm.constants import XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS

logger = structlog.get_logger()

# Foundry / Arachnid 确定性部署代理：anvil 创世内置，主流公链也已存在。
# 向它发送 `salt(32) || init_code` 即触发 CREATE2 部署。
CREATE2_DEPLOYER_ADDRESS = "0x4e59b44847b379578588920cA78FbF26c0B4956C"

# 全网统一 salt，必须与 contracts/scripts/DeployXcashVaultSlot.s.sol 的 DEPLOY_SALT 一致：
# keccak256("xcash:evm-vault-slot:v1")。改动会让工厂 / 实现合约地址漂移，破坏跨链同地址假设。
XCASH_VAULT_SLOT_DEPLOY_SALT = keccak(b"xcash:evm-vault-slot:v1")

# 本地一次性部署给足 gas，避免对 CREATE2 deployer 代理做 estimateGas 的边界问题。
_LOCAL_DEPLOY_GAS = 3_000_000

_ARTIFACTS_DIR = Path(__file__).resolve().parent / "contracts" / "artifacts"


def _load_creation_code(filename: str) -> bytes:
    """读取 Foundry 编译产物里的 creation bytecode（容忍 0x 前缀）。"""
    raw = _ARTIFACTS_DIR.joinpath(filename).read_text(encoding="utf-8").strip()
    return bytes.fromhex(raw.removeprefix("0x"))


def build_implementation_init_code() -> bytes:
    """XcashVaultSlot 无构造参数，init_code 即 creationCode。"""
    return _load_creation_code("XcashVaultSlot.bin")


def build_factory_init_code(*, implementation_address: str) -> bytes:
    """XcashVaultSlotFactory 构造参数为 implementation 地址（ABI 左填充 32 字节）。"""
    return (
        _load_creation_code("XcashVaultSlotFactory.bin")
        + bytes(12)
        + to_canonical_address(implementation_address)
    )


def predict_create2_address(init_code: bytes) -> str:
    """按 CREATE2 deployer + 统一 salt 预测 init_code 的落地地址。"""
    digest = keccak(
        b"\xff"
        + to_canonical_address(CREATE2_DEPLOYER_ADDRESS)
        + XCASH_VAULT_SLOT_DEPLOY_SALT
        + keccak(init_code)
    )
    return Web3.to_checksum_address(digest[-20:])


def ensure_local_vault_slot_contracts(*, w3: Web3) -> None:
    """把工厂 / 实现合约确定性部署到本地 EVM 链；幂等：地址已有代码则跳过。

    依赖 CREATE2 deployer 已在链上（anvil 创世内置）。部署后断言落地地址等于
    `evm.constants` 的全网统一地址，编译产物漂移立即抛错，避免本地与生产地址错位。
    """
    deployer = Web3.to_checksum_address(CREATE2_DEPLOYER_ADDRESS)
    if len(w3.eth.get_code(deployer)) == 0:
        raise RuntimeError(
            f"CREATE2 deployer {deployer} 不在链上，无法确定性部署 VaultSlot 合约"
        )

    sender = w3.eth.accounts[0]

    # 1. 实现合约：无构造参数。
    implementation_init = build_implementation_init_code()
    implementation_address = predict_create2_address(implementation_init)
    _assert_no_address_drift(
        predicted=implementation_address,
        expected=XCASH_VAULT_SLOT_IMPLEMENTATION_ADDRESS,
        label="implementation",
    )
    _deploy_via_create2(
        w3=w3,
        sender=sender,
        deployer=deployer,
        init_code=implementation_init,
        expected_address=implementation_address,
        label="implementation",
    )

    # 2. 工厂：构造参数引用上面部署的实现合约地址。
    factory_init = build_factory_init_code(implementation_address=implementation_address)
    factory_address = predict_create2_address(factory_init)
    _assert_no_address_drift(
        predicted=factory_address,
        expected=XCASH_VAULT_SLOT_FACTORY_ADDRESS,
        label="factory",
    )
    _deploy_via_create2(
        w3=w3,
        sender=sender,
        deployer=deployer,
        init_code=factory_init,
        expected_address=factory_address,
        label="factory",
    )


def _assert_no_address_drift(*, predicted: str, expected: str, label: str) -> None:
    if Web3.to_checksum_address(predicted) != Web3.to_checksum_address(expected):
        raise RuntimeError(
            f"VaultSlot {label} 编译产物漂移："
            f"预测地址 {predicted} ≠ 期望 {expected}（检查 artifacts 与编译参数）"
        )


def _deploy_via_create2(
    *,
    w3: Web3,
    sender: str,
    deployer: str,
    init_code: bytes,
    expected_address: str,
    label: str,
) -> None:
    expected_checksum = Web3.to_checksum_address(expected_address)
    if len(w3.eth.get_code(expected_checksum)) > 0:
        # 幂等：已部署（同进程重复 bootstrap / anvil 未重启）直接跳过。
        return

    tx_hash = w3.eth.send_transaction(
        {
            "from": sender,
            "to": deployer,
            "data": XCASH_VAULT_SLOT_DEPLOY_SALT + init_code,
            "gas": _LOCAL_DEPLOY_GAS,
        }
    )
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30, poll_latency=0.5)
    if receipt.get("status") != 1:
        raise RuntimeError(f"本地部署 VaultSlot {label} 交易失败：tx={tx_hash.hex()}")
    if len(w3.eth.get_code(expected_checksum)) == 0:
        raise RuntimeError(
            f"本地部署 VaultSlot {label} 后地址 {expected_checksum} 仍无代码"
        )

    logger.info(
        "evm.local_vault_slot.deployed",
        contract=label,
        address=expected_checksum,
    )
