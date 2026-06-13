# Tron VaultSlot Deployment Order

源码独立维护在 `xcash/tron/contracts/src/`，本工程只编译 Tron/TVM 专用合约。

1. 使用官方 Tron `solc.tron 0.8.26+commit.733b4d28` 编译 Tron 专用源码到 `out/`：

   ```bash
   TRON_SOLC=/private/tmp/tron-solc-0.8.26
   cd xcash/tron/contracts && forge build --use "$TRON_SOLC"
   ```

   不要加 `--no-metadata`，否则 Tronscan 表单无法复现部署字节码。

2. Deploy `XcashVaultSlot`.
3. Deploy `XcashVaultSlotFactory(implementation_address)`.
4. Export deployed addresses for the verification scripts:

```bash
export TRON_VAULT_SLOT_IMPLEMENTATION_ADDRESS="T..."
export TRON_VAULT_SLOT_FACTORY_ADDRESS="T..."
```

5. Run the Nile verification scripts from `xcash/tron/nile_verification/`.
6. Copy the verified addresses into `TRON_VAULT_SLOT_CONTRACT_ADDRESSES` in
   `xcash/chains/constants.py` for the corresponding Tron network.

Do not use EVM-style on-chain deterministic-address prediction on Tron.
The application-side predictor uses the TVM `0x41` CREATE2 address preimage.
