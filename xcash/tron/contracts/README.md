# xcash Tron VaultSlot Contracts

这是 Tron / TVM 版本的 VaultSlot 合约资产。正式业务代码只支持 TRC20
收款和归集；TRX 只作为部署、触发合约和 Energy/Bandwidth 的系统资源币。

## 合约

与 EVM 共用同一套 Solidity 源码（单一真相）：`xcash/evm/contracts/src/` 的
`XcashVaultSlotTemplate.sol` 与 `XcashVaultSlotFactory.sol`。本工程 `foundry.toml`
通过 `src = "../../evm/contracts/src"` 直接编译它们，TVM 兼容该字节码（`evm_version=paris`）。

- `XcashVaultSlotTemplate`
  - 每个 clone 的 immutable args 中写入目标 `vault`。
  - `collect(token)` 把 TRC20 全额归集到 `vault`。
  - `receive()` 只用于转发误入 slot 的 TRX，不作为用户侧 TRX 支付入口。
- `XcashVaultSlotFactory`
  - `deployVaultSlot(vault, salt)` 使用 OpenZeppelin Clones immutable args 和
    TVM CREATE2 部署 slot。
  - `ensureDeployedAndCollect(vault, salt, token)` 在 slot 尚未部署时先部署，
    然后调用 slot 的 `collect(token)`，用于首次 TRC20 入账后的部署 + 归集收口。
  - 不提供链上 `predict`（源码层面已删，EVM/TVM 同此一份）。Tron 地址预测必须使用
    `xcash/tron/contracts_codec.py` 的 Python `0x41` 预测器。

## 构建与部署

源码与 EVM 共用，用 Foundry 编译本工程（产物落在 `out/`，供 nile 部署脚本读取）：

```bash
cd xcash/tron/contracts && forge build
```

先部署 template，再用 template 地址部署 factory（见 `nile_verification/deploy_contracts.py`）。

部署完成后写入：

```bash
export TRON_VAULT_SLOT_TEMPLATE_ADDRESS="T..."
export TRON_VAULT_SLOT_FACTORY_ADDRESS="T..."
export TRON_VAULT_SLOT_FEE_LIMIT="..."
```

Nile 验收脚本在：

```text
xcash/tron/nile_verification/
```

最小验收顺序：

```bash
.venv/bin/python xcash/tron/nile_verification/dry_run_predictions.py --count 5
.venv/bin/python xcash/tron/nile_verification/nile_deploy_compare.py --broadcast --wait
.venv/bin/python xcash/tron/nile_verification/clone_collect_verify.py --broadcast --wait
.venv/bin/python xcash/tron/nile_verification/activation_create2_ab.py --case a --broadcast --wait
.venv/bin/python xcash/tron/nile_verification/activation_create2_ab.py --case b --broadcast --wait
```

只有上述 Nile 验收通过并回填结论后，才允许设置
`TRON_VAULT_SLOT_NILE_VERIFIED=True` 对普通项目暴露 Tron 收款/充币。
