"""Tron 链族向 chains 引擎登记的实现，由 TronConfig.ready() 调用 register()。"""

from tron import vault_slots
from tron.adapter import TronAdapter
from tron.scanner import scan_tron_chain_round
from tron.tx_loop import dispatch_tron_chain_round
from tron.tx_loop import poll_tron_chain_round

from chains.constants import ChainType
from chains.registry import ChainFamily
from chains.registry import register_chain_family


def register() -> None:
    register_chain_family(
        ChainFamily(
            chain_type=ChainType.TRON,
            adapter_class=TronAdapter,
            vault_slot_backend=vault_slots,
            scan_round=scan_tron_chain_round,
            poll_round=poll_tron_chain_round,
            dispatch_round=dispatch_tron_chain_round,
        )
    )
