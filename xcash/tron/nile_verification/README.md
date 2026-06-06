# Tron VaultSlot Nile Verification

这些脚本用于正式开发完成后由人工在 Nile 验收 Tron VaultSlot 链上行为。所有私钥走环境变量，脚本只打印 txID 和 receipt，不打印私钥。

正式合约源码与 EVM 共用，在 `xcash/evm/contracts/src/`（`tron/contracts` 工程已通过
`src` 指向它）。先 `cd xcash/tron/contracts && forge build`，部署
`XcashVaultSlotTemplate` 和 `XcashVaultSlotFactory`（见 `deploy_contracts.py`），再运行本目录脚本。

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
- `TRON_VAULT_SLOT_FACTORY_ADDRESS` / `TRON_VAULT_SLOT_TEMPLATE_ADDRESS` 是部署脚本输出，不是部署前输入

- `DJANGO_SETTINGS_MODULE=config.settings.dev`
- `TRON_API_KEY`
- `TRON_NILE_OWNER_ADDRESS`
- `TRON_NILE_PRIVATE_KEY`
- `TRON_VAULT_SLOT_FACTORY_ADDRESS`
- `TRON_VAULT_SLOT_TEMPLATE_ADDRESS`
- `TRON_VAULT_SLOT_FEE_LIMIT`
- `TRON_VAULT_SLOT_DEPLOY_FEE_LIMIT`
- `TRON_VAULT_SLOT_TEST_VAULT`
- `TRON_VAULT_SLOT_SALT_HEX`
- `TRON_USDT_CONTRACT_ADDRESS`

## 命令

首次 Nile 验收时，factory/template 地址由部署脚本生成。先编译合约：

```bash
cd xcash/tron/contracts
forge build
cd ../../..
```

再部署 template 和 factory，并把脚本输出的两个地址回填到 `.env`：

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
`TRON_VAULT_SLOT_SALT_HEX`。`clone_collect_verify.py` 会按 factory/vault/salt 预测
slot，并用 `ensureDeployedAndCollect(vault,salt,token)` 执行首次部署 + 归集；若同时提供
`TRON_VAULT_SLOT_ADDRESS`，脚本会校验它与预测地址一致。A/B 脚本会自行生成盐并打印预测地址。
