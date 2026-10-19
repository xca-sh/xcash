"""Tron 主动交易在 chain-runner 链循环中的两步：终局轮询与派发。

当前派发、广播与回执确认仍由 tron.tasks 中的 Celery 任务负责，这两步暂为空实现，
接管后再填入逻辑。
"""

from chains.models import Chain


def poll_tron_chain_round(chain: Chain, deadline: float) -> None:
    """按回执收口本链在途的主动交易。"""


def dispatch_tron_chain_round(chain: Chain, deadline: float) -> None:
    """派发本链待发的主动交易，并重播已过期的在途交易。"""
