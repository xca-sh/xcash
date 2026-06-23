"""静态读取 xcash/chains/constants.py 中登记的 EVM chain id。"""
# ruff: noqa: INP001

from __future__ import annotations

import ast
from pathlib import Path

CONSTANTS_PATH = Path(__file__).resolve().parents[3] / "chains" / "constants.py"


def chain_spec_value(call: ast.Call, field_name: str, position: int) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == field_name:
            return keyword.value
    if len(call.args) > position:
        return call.args[position]
    return None


def is_evm_chain_spec(call: ast.Call) -> bool:
    if not isinstance(call.func, ast.Name) or call.func.id != "ChainSpec":
        return False
    chain_type = chain_spec_value(call, "type", 0)
    if chain_type is None:
        return False
    return (
        isinstance(chain_type, ast.Attribute)
        and isinstance(chain_type.value, ast.Name)
        and chain_type.value.id == "ChainType"
        and chain_type.attr == "EVM"
    )


def chain_id_from_spec(call: ast.Call) -> int:
    chain_id = chain_spec_value(call, "chain_id", 1)
    if not isinstance(chain_id, ast.Constant) or not isinstance(chain_id.value, int):
        raise TypeError("EVM ChainSpec 的 chain_id 必须是整数常量")
    return chain_id.value


def read_evm_chain_ids() -> list[int]:
    tree = ast.parse(CONSTANTS_PATH.read_text(), filename=str(CONSTANTS_PATH))
    for node in tree.body:
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CHAIN_SPECS"
            for target in node.targets
        ):
            value = node.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "CHAIN_SPECS"
        ):
            value = node.value
        if value is None:
            continue
        if not isinstance(value, ast.Dict):
            raise TypeError("CHAIN_SPECS 必须是 dict 字面量")
        return [
            chain_id_from_spec(item)
            for item in value.values
            if isinstance(item, ast.Call) and is_evm_chain_spec(item)
        ]
    raise RuntimeError("未找到 CHAIN_SPECS")


def main() -> int:
    print(" ".join(str(chain_id) for chain_id in read_evm_chain_ids()))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
