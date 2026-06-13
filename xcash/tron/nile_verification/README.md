# Tron VaultSlot Nile Verification

这些脚本用于正式开发完成后由人工在 Nile 验收 Tron VaultSlot 链上行为。所有私钥走环境变量，脚本只打印 txID 和 receipt，不打印私钥。

正式合约源码独立维护在 `xcash/tron/contracts/src/`。先用官方 Tron
`solc.tron 0.8.26+commit.733b4d28` 编译，部署 `XcashVaultSlot` 和
`XcashVaultSlotFactory`（见 `deploy_contracts.py`），再运行本目录脚本。不要使用
`--no-metadata`，Tronscan 表单验证需要 Solidity 默认 CBOR metadata。

## 环境变量

脚本会自动读取本目录的 `.env`；shell 环境变量优先级更高，可临时覆盖 `.env`。

首次部署前只需要填：

- `TRON_API_KEY`
- `TRON_NILE_OWNER_ADDRESS`
- `TRON_NILE_PRIVATE_KEY`
- `TRON_USDT_CONTRACT_ADDRESS`

其余规则：

- `TRON_NILE_CHAIN_CODE` 默认 `tron-nile`
- `TRON_NILE_RPC_URL` 默认 `https://nile.trongrid.io`
- `TRON_VAULT_SLOT_TEST_VAULT` 为空时默认使用 `TRON_NILE_OWNER_ADDRESS`
- `TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT` 为空时默认 `1500000000`
- `TRON_VAULT_SLOT_FEE_LIMIT` 为空时验收脚本默认 `300000000`
- `TRON_VAULT_SLOT_FACTORY_ADDRESS` / `TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS` 是部署脚本输出，不是部署前输入

- `DJANGO_SETTINGS_MODULE=config.settings.dev`
- `TRON_API_KEY`
- `TRON_NILE_OWNER_ADDRESS`
- `TRON_NILE_PRIVATE_KEY`
- `TRON_VAULT_SLOT_FACTORY_ADDRESS`
- `TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS`
- `TRON_VAULT_SLOT_FEE_LIMIT`
- `TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT`
- `TRON_VAULT_SLOT_TEST_VAULT`
- `TRON_VAULT_SLOT_SALT_HEX`
- `TRON_USDT_CONTRACT_ADDRESS`

## 命令

首次 Nile 验收时，factory/implementation 地址由部署脚本生成。先编译合约：

```bash
TRON_SOLC=/private/tmp/tron-solc-0.8.26
cd xcash/tron/contracts
forge build --use "$TRON_SOLC"
cd ../../..
```

再部署 implementation 和 factory，并把脚本输出的两个地址回填到 `.env`：

```bash
.venv/bin/python xcash/tron/nile_verification/deploy_contracts.py
```

地址回填后继续跑链上行为验收：

```bash
.venv/bin/python xcash/tron/nile_verification/dry_run_predictions.py --count 5
.venv/bin/python xcash/tron/nile_verification/nile_deploy_compare.py --broadcast --wait
.venv/bin/python xcash/tron/nile_verification/clone_collect_verify.py --broadcast --wait
.venv/bin/python xcash/tron/nile_verification/activation_create2_ab.py --case a --broadcast --wait
.venv/bin/python xcash/tron/nile_verification/activation_create2_ab.py --case b --broadcast --wait
```

`nile_deploy_compare.py` 和 `clone_collect_verify.py` 需要额外提供
`TRON_VAULT_SLOT_SALT_HEX`。`clone_collect_verify.py` 按生产「部署→归集」两段式路径
验收：slot 未部署时先发 `deployVaultSlot(vault,salt)`，确认后对 slot 直调
`collect(token)` 清扫 TRC20；原生 TRX 的 `collect(address(0))` 由 A/B 激活脚本覆盖。
slot 已部署则跳过部署直接归集——同一 salt 重复执行
即验收「已部署槽位重复归集」。若同时提供 `TRON_VAULT_SLOT_ADDRESS`，脚本会校验它
与预测地址一致。A/B 脚本会自行生成盐并打印预测地址。
