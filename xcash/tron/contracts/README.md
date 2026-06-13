# xcash Tron VaultSlot Contracts

这是 Tron / TVM 版本的 VaultSlot 合约资产。正式业务代码支持 TRC20 与原生
TRX 收款和归集；TRX 同时也是部署、触发合约和 Energy/Bandwidth 的系统资源币。

## 合约

Tron 合约源码独立维护在 `xcash/tron/contracts/src/`，不再复用
`xcash/evm/contracts/src/`。两边仍保持相同的 clone immutable args 结构，便于业务层
沿用地址预测和归集模型；但 Tron 可以按 TVM 真实语义删减无用入口、独立锁定编译器版本。

- `XcashVaultSlot`
  - 每个 clone 的 immutable args 中写入目标 `vault`。
  - `collect(token)` 把 TRC20 全额归集到 `vault`；`collect(address(0))`
    把 slot 内原生 TRX 全额归集到 `vault`。
  - 合约不定义 `receive()`：Tron 原生 TRX 转账走 TransferContract，不会进入 TVM；
    入账由扫描器逐块解析 TransferContract，归集靠显式调用 `collect(address(0))`。
- `XcashVaultSlotFactory`
  - `deployVaultSlot(vault, salt)` 使用本地 `OpenZeppelinClones.cloneDeterministicWithImmutableArgs`
    和 TVM CREATE2 部署 slot。部署与归集是两段式：先由部署交易落地 slot，
    归集再对 slot 直调 `collect(token)`。
  - 合约内不含任何链上地址预测（EVM 0xff 与 TVM 0x41 的 CREATE2 preimage
    前缀不同，链上预测无法共源）。Tron 地址预测必须使用
    `xcash/tron/contracts_codec.py` 的 Python `0x41` 预测器。
- `OpenZeppelinClones`
  - 只 vendored Xcash VaultSlot 实际用到的 OpenZeppelin Clones 函数：
    `cloneDeterministicWithImmutableArgs`、`fetchCloneArgs` 和内部
    `_cloneCodeWithImmutableArgs`。
  - 平铺在 `src/` 目录，避免 Tronscan 验证时依赖 `@openzeppelin/...` import 路径。

## 构建与部署

用官方 Tron `solc.tron` 编译 Tron 专用源码（产物落在 `out/`，供 Nile 部署脚本读取）。
不要使用裸 `forge build`；Foundry 会下载 upstream Solidity 0.8.26，生成的字节码与
Tronscan 的 `tron_v0.8.26+commit.733b4d28` 不一致。也不要开启 `--no-metadata`；
Tronscan 表单会按 Solidity 默认 metadata 编译，部署字节码必须保留 CBOR metadata 才能验证。

```bash
TRON_SOLC=/private/tmp/tron-solc-0.8.26
cd xcash/tron/contracts && forge build --use "$TRON_SOLC"
```

先部署 implementation，再用 implementation 地址部署 factory（见 `nile_verification/deploy_contracts.py`）。

部署完成后，验证脚本继续通过环境变量读取本次部署输出：

```bash
export TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS="T..."
export TRON_VAULT_SLOT_FACTORY_ADDRESS="T..."
```

运行时代码不再读取全局 factory/implementation 环境变量；主网与 Nile 的地址按链维护在
`xcash/chains/constants.py` 的 `TRON_VAULT_SLOT_CONTRACT_ADDRESSES`。

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

只有上述 Nile 验收通过、回填对应链的 `TRON_VAULT_SLOT_CONTRACT_ADDRESSES` 后，才允许发布
对普通项目暴露 Tron 收款/充币。
