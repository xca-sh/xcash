# xcash Tron VaultSlot Contracts

这是 Tron / TVM 版本的 VaultSlot 合约资产。正式业务代码支持 TRC20 与原生
TRX 收款和归集；TRX 同时也是部署、触发合约和 Energy/Bandwidth 的系统资源币。

## 合约

与 EVM 共用同一套 Solidity 源码（单一真相）：`xcash/evm/contracts/src/` 的
`XcashVaultSlotTemplate.sol` 与 `XcashVaultSlotFactory.sol`。本工程 `foundry.toml`
通过 `src = "../../evm/contracts/src"` 直接编译它们，TVM 兼容该字节码（`evm_version=paris`）。

- `XcashVaultSlotTemplate`
  - 每个 clone 的 immutable args 中写入目标 `vault`。
  - `collect(token)` 把 TRC20 全额归集到 `vault`；`collect(address(0))`
    把 slot 内原生 TRX 全额归集到 `vault`。
  - Tron 原生 TRX 转账走 TransferContract，不会进入 TVM `receive()`；
    入账由扫描器逐块解析 TransferContract，归集靠显式调用 `collect(address(0))`。
- `XcashVaultSlotFactory`
  - `deployVaultSlot(vault, salt)` 使用 OpenZeppelin Clones immutable args 和
    TVM CREATE2 部署 slot。部署与归集是两段式：先由部署交易落地 slot，
    归集再对 slot 直调 `collect(token)`。
  - 合约内不含任何链上地址预测（EVM 0xff 与 TVM 0x41 的 CREATE2 preimage
    前缀不同，链上预测无法共源）。Tron 地址预测必须使用
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
