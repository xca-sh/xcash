"""EVM 链族向 chains 引擎登记的实现，由 EvmConfig.ready() 调用 register()。"""

from chains.constants import ChainType
from chains.registry import ChainFamily
from chains.registry import register_chain_family
from evm import vault_slots
from evm.adapter import EvmAdapter


def register() -> None:
    register_chain_family(
        ChainFamily(
            chain_type=ChainType.EVM,
            adapter_class=EvmAdapter,
            vault_slot_backend=vault_slots,
        )
    )
