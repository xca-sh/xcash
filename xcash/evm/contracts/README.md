# xcash EVM Vault Slot Contracts

XcashVaultSlotFactory 与 XcashVaultSlot 是合约账单与充币的链上归集入口。

## 设计

- `XcashVaultSlotFactory` 持有不可变的 `vaultSlotImplementation` 地址。
- `deployVaultSlot(vault, salt)` 使用 OpenZeppelin Clones immutable args 通过 CREATE2 部署 slot。
- 每个 slot 的 immutable `vault` 决定原生币和 ERC20 最终归集地址。
- `XcashVaultSlot` 的 `receive` 只抛出原生币入账事件，原生币留存在 slot。
- 原生币和 ERC20 余额都由 `collect(token)` 转入 vault；原生币使用 `collect(address(0))`。

## 构建

```bash
forge install OpenZeppelin/openzeppelin-contracts@v5.6.1 --no-git --shallow
make all
make test
```

## 测试 / 静态扫描

测试分两层：

- **单元测试**（`test/XcashVaultSlotTest.t.sol`、`XcashVaultSlotFactoryTest.t.sol`）：
  固定输入覆盖具体场景与 revert 分支，含 USDT 非标返回、false / 畸形返回等 token。
- **Fuzz 属性测试**（`test/XcashVaultSlotFuzz.t.sol`）：对金额、归集地址、salt
  全空间随机取值，验证核心不变性——receive 只记录并留存原生币、归集全额到位且
  slot 清零、资金只流向编码的 immutable vault、CREATE2 预测地址恒等于实际部署地址、
  不同 salt 不碰撞。

```bash
make test     # 单元 + fuzz 全部测试
make fuzz     # 只跑 fuzz，FOUNDRY_FUZZ_RUNS=10000 加大随机覆盖
```

### Slither 静态扫描

```bash
uv tool install slither-analyzer   # 一次性安装（或 pipx install slither-analyzer）
make slither                       # 扫 src/，medium 及以上发现会让命令失败
```

配置见 `slither.config.json`：`fail_on=medium` 让中 / 高危阻断、低 / 信息级仅提示；
`detectors_to_exclude` 定点关掉在本代码上**已复核为误报**的检测项，更强的
`reentrancy-eth` / `reentrancy-no-eth` 等保持开启：

| 排除项 | 原因 |
|---|---|
| `reentrancy-balance` | 归集后无状态写入，资金只能流向 immutable vault，重入偷不到币 |
| `incorrect-equality` | 归集后 `balanceOf(this)==0`、`amount==0` 是有意的清空 / 零值判断（归集全额扫零的不变量），非 timestamp/balance 精确匹配陷阱 |
| `too-many-digits` | 误读 `keccak256(type(X).runtimeCode)` 为大数字面量 |
| `low-level-calls`、`assembly` | 原生币归集与 immutable args 解码有意使用 |
| `naming-convention`、`solc-version` | 风格与版本噪声（0.8.35 为有意锁定） |

## 地址预测公式

```text
vault_slot = keccak256(0xff || factory || salt || keccak256(slot_init_code))[-20:]
```

其中 `slot_init_code` 由 OpenZeppelin `Clones` immutable args 规则构造。
Python 侧对应实现为 `xcash/evm/contracts_codec.py` 的
`build_xcash_vault_slot_init_code(vault_slot_implementation, vault)` 和
`predict_xcash_vault_slot_address(vault, salt)`；如需校验非默认部署地址，
也可显式传入 `factory` 和 `vault_slot_implementation`。

## Fixtures

`make fixtures` 会运行 `scripts/DumpXcashVaultSlotFixtures.s.sol`，生成：

```text
../tests/fixtures/xcash_vault_slot_fixtures.json
```

该 fixture 用 Foundry/OpenZeppelin 的实现校验 Python 侧 slot init_code 与
CREATE2 地址预测逻辑。

## Salt 约束

工厂无访问控制，任何人拿到相同 `salt + vault` 都能触发部署。这**不构成资金安全
风险**：slot 的归集目标 `vault` 写死在 clone 的 immutable args 里，无论谁部署、谁
调用 `collect`，资金都只能流向该 vault，攻击者抢先部署只是替商户垫了部署 gas。
因此 salt **不要求不可预测**，业务层可直接用确定性输入派生（见
`VaultSlot.build_salt`：按 `链类型 + 用途 + project + 业务身份` keccak），以换取
「同一业务身份在该链类型所有网络得到一致 slot 地址」的特性。

唯一的取舍是：确定性 salt 意味着 slot 地址在链下可被枚举推算，归集发生后 `vault`
也会暴露在链上，故第三方可推断某商户的收款地址集合。这是已知且可接受的隐私权衡，
不影响资金安全。
