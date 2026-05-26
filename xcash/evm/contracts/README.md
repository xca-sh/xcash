# xcash EVM Deposit Slot Contracts

XcashDepositFactory 与 XcashDepositTemplate 是合约账单的链上归集入口。

## 设计

- `XcashDepositFactory` 持有不可变的 `depositTemplate` 地址。
- `deployDepositSlot(vault, salt)` 使用 OpenZeppelin Clones immutable args 通过 CREATE2 部署 slot。
- 每个 slot 的 immutable `vault` 决定原生币和 ERC20 最终归集地址。
- `XcashDepositTemplate` 的 `receive` / `fallback` 会把原生币转发到 vault。
- ERC20 余额由 `collectERC20(token)` 转入 vault。

## 构建

```bash
forge install OpenZeppelin/openzeppelin-contracts@v5.6.1 --no-git --shallow
make all
make test
```

## 地址预测公式

```text
deposit_slot = keccak256(0xff || factory || salt || keccak256(slot_init_code))[-20:]
```

其中 `slot_init_code` 由 OpenZeppelin `Clones` immutable args 规则构造。
Python 侧对应实现为 `xcash/evm/contracts_codec.py` 的
`build_xcash_deposit_slot_init_code(deposit_template, vault)` 和
`predict_xcash_deposit_slot_address(vault, salt)`；如需校验非默认部署地址，
也可显式传入 `factory` 和 `deposit_template`。

## Fixtures

`make fixtures` 会运行 `scripts/DumpXcashDepositFixtures.s.sol`，生成：

```text
../tests/fixtures/xcash_deposit_slot_fixtures.json
```

该 fixture 用 Foundry/OpenZeppelin 的实现校验 Python 侧 slot init_code 与
CREATE2 地址预测逻辑。

## Salt 约束

工厂无访问控制，任何人拿到相同 `salt + vault` 都能触发部署。业务层生成
salt 时必须包含服务端秘密或使用内部不可预测随机源，禁止直接使用公开订单号、
商户号、递增编号等可预测输入。
