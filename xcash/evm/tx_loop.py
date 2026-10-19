"""EVM 主动交易在 chain-runner 链循环中的两步：终局轮询与派发。

当前派发、广播与终局轮询仍由 evm.tasks 中的 Celery 任务负责，这两步暂为空实现，
接管后再填入逻辑。
"""

from chains.models import Chain


def poll_evm_chain_round(chain: Chain, deadline: float) -> None:
    """轮询本链在途主动交易的链上终局。"""


def dispatch_evm_chain_round(chain: Chain, deadline: float) -> None:
    """派发本链待发的主动交易。"""
