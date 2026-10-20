"""新建主动交易时唤醒对应链的 chain-runner 线程（PG LISTEN/NOTIFY）。

交易由 web 请求、worker 任务、后台操作等不同进程创建，而发送它们的是 chain-runner
进程里的链线程，进程内唤醒无从谈起。NOTIFY 是 PG 原生的跨进程通知，并且天然具有
on_commit 语义：只在最外层事务提交时投递，回滚（含 savepoint 回滚）即丢弃；同一事务
内同一链的多次通知只投递一条；没有监听者时直接丢弃，不会积压。

通知只是「提前醒来」的提示：链线程的派发步骤永远按库里的任务状态工作，runner 重启
期间丢失的通知由它下一轮照常接走，因此不需要任何补偿逻辑。
"""

from django.db import connection

CHAIN_TX_CHANNEL = "xcash_chain_tx"


def notify_chain_tx_pending(chain_id: int) -> None:
    """通知 chain_id 所在链的 chain-runner 线程有新的待发交易。

    必须在创建任务的同一事务内调用，才能保证链线程醒来查库时一定看得到新任务。
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [CHAIN_TX_CHANNEL, str(chain_id)])
