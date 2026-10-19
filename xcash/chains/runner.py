"""chain-runner：按链常驻的扫描循环（manage.py run_chains）。

链扫描本质是「每条链一个有状态的常驻循环」：读游标 → 扫一轮 → 等扫描周期 → 再扫。
这里直接按这个形状实现，不再借 Beat tick + 队列 + 单例锁 + 时限去模拟循环：

- 一个进程，主线程是 Supervisor，每条活跃链一个 ChainLoop 线程。扫描是 IO 密集型，
  等 RPC 时释放 GIL；一链一进程每个约 100MB 私有内存，不划算。
- 同一条链只允许一个循环：PG 会话级 advisory lock 按链加锁。锁持有在专用连接上，
  进程退出或连接断开即自动释放，没有 TTL，也不受缓存淘汰影响。多开实例时后到者抢不到
  锁就空转，前一个实例退出后 ≤1 个 tick 接管。锁只防重复扫描浪费，不承担正确性：入账
  唯一入口按 (chain, hash, event_index) 行锁去重，短暂双扫不会重复入账。
- 单轮耗时由协作式预算约束（chains.scanning）；预算管不到的挂死（典型如 getaddrinfo 在
  C 层阻塞）由看门狗兜底：线程无法从外部杀掉，判定挂死后整个进程退出，交给容器
  restart 策略重启（crash-only），各链进度均已按单元提交。
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog
from django.db import close_old_connections
from django.db import connections

from chains.constants import ChainType
from chains.models import Chain
from chains.registry import get_chain_family
from chains.scanning import ScanRoundBudgetExceeded
from chains.scanning import scan_round_deadline
from config.worker_health import record_chain_runner_heartbeat

if TYPE_CHECKING:
    from django.db.models import QuerySet

logger = structlog.get_logger()

# advisory lock 两参数形式的第一个键（int4），把本模块的锁与其他使用方隔开；第二个键是链 pk。
CHAIN_LOCK_NAMESPACE = 0x58434852  # "XCHR"
SUPERVISOR_TICK_SECONDS = 5
# 单轮扫描的硬上限。预算 40s 只在单元之间检查，再加一个单元的正常最坏耗时（单次调用
# 8s 超时 × 3 次尝试 + 退避 ≈ 25s，EVM 二分或 Tron 每块两次调用再翻倍）仍远低于此值；
# 超过即判定线程挂死。
SCAN_ROUND_HARD_LIMIT_SECONDS = 180


def scannable_chains() -> QuerySet[Chain]:
    """需要扫描的链；runner 调度与 /health/scanning 的停滞判定共用这一口径。

    排除未配 API Key 的 Tron 链：CheckConstraint chain_active_requires_runtime_config
    目前已保证 active 的 Tron 链必有 key，这一句是为将来放宽约束时两边不分叉。
    """
    return Chain.objects.filter(
        active=True,
        type__in=(ChainType.EVM, ChainType.TRON),
    ).exclude(type=ChainType.TRON, tron_api_key="")


def terminate_process(code: int) -> None:
    """立即结束进程：线程无法被外部终止，只能整体退出。先刷出日志再 os._exit。"""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


class ChainLocks:
    """在一条专用连接上持有各链的会话级 advisory lock。

    不能用 Django 的线程连接持锁：close_old_connections() 会按 CONN_MAX_AGE 回收它，
    连接一关锁就丢了。这里另建一条不登记在 connections 里的连接，只有 Supervisor 线程
    使用、全程 autocommit（Django 建连默认）。语句直接走底层 psycopg 连接：Django 包装层
    的健康检查可能把失效连接悄悄关掉重连，新会话不持有任何锁，锁丢失就被掩盖了。

    前提是直连 PG：若将来在应用与 PG 之间加事务级连接池，这条连接必须绕过连接池，
    否则会话级锁会落到别的客户端会话上。
    """

    def __init__(self):
        self.wrapper = connections.create_connection("default")
        self.wrapper.ensure_connection()
        self.raw_connection = self.wrapper.connection

    def execute(self, sql: str, params: tuple = ()):
        return self.raw_connection.execute(sql, params).fetchone()[0]

    def try_lock(self, chain_pk: int) -> bool:
        return self.execute(
            "SELECT pg_try_advisory_lock(%s, %s)", (CHAIN_LOCK_NAMESPACE, chain_pk)
        )

    def unlock(self, chain_pk: int) -> None:
        self.execute(
            "SELECT pg_advisory_unlock(%s, %s)", (CHAIN_LOCK_NAMESPACE, chain_pk)
        )

    def ping(self) -> None:
        """连接失效即抛错：锁已随连接一起丢失。"""
        self.execute("SELECT 1")

    def close(self) -> None:
        self.wrapper.close()


class ChainLoop:
    """单条链的常驻扫描线程：扫一轮 → 推进 last_scanned_at → 等扫描周期 → 再扫。"""

    def __init__(self, chain: Chain):
        self.chain_pk = chain.pk
        self.chain_code = chain.code
        self.scan_interval_seconds = chain.scan_interval_seconds
        self.stop_event = threading.Event()
        # 本轮开始的 monotonic 时刻，空闲时为 None；看门狗据此判定挂死。
        self.round_started_at: float | None = None
        self.thread = threading.Thread(
            target=self.run, name=f"chain-{chain.code}", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def join(self) -> None:
        self.thread.join()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                # 每轮前后做 Celery Django fixup 在任务前后做的连接清理：按 CONN_MAX_AGE
                # 回收、丢弃失效连接，PG 重启后下一轮自动重连。
                close_old_connections()
                self.run_once()
                close_old_connections()
                self.stop_event.wait(self.scan_interval_seconds)
        finally:
            # 线程自己的 DB 连接只能在本线程关闭。
            connections.close_all()

    def run_once(self) -> None:
        """执行一轮扫描。任何异常都只记录，循环绝不因单轮失败而退出。

        与原 Celery 扫描任务的 finally 语义一致：无论本轮成败都推进 last_scanned_at，
        按固定周期重试；失败是否持续由游标 last_error 体现给 /health/scanning。
        """
        self.round_started_at = time.monotonic()
        chain = None
        try:
            chain = Chain.objects.get(pk=self.chain_pk)
            if not chain.active:
                # 链刚被停用，Supervisor 会在下一个 tick 停掉本线程。
                return
            get_chain_family(chain.type).scan_round(chain, scan_round_deadline())
        except ScanRoundBudgetExceeded:
            # 已完成单元的进度与游标错误均已由扫描器落盘，剩余区块留给下一轮。
            logger.warning("链扫描超出轮次预算", chain=self.chain_code)
        except Exception:  # noqa: BLE001
            logger.exception("链扫描失败", chain=self.chain_code)
        finally:
            if chain is not None and chain.active:
                try:
                    chain.mark_scanned()
                except Exception:  # noqa: BLE001
                    logger.exception("推进 last_scanned_at 失败", chain=self.chain_code)
            self.round_started_at = None


class ChainRunner:
    """Supervisor：按活跃链集合启停 ChainLoop，并负责锁保活、看门狗与心跳。"""

    def __init__(
        self,
        *,
        locks_factory: Callable[[], ChainLocks] = ChainLocks,
        loop_factory: Callable[[Chain], ChainLoop] = ChainLoop,
        exit_process: Callable[[int], None] = terminate_process,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.locks_factory = locks_factory
        self.loop_factory = loop_factory
        self.exit_process = exit_process
        self.clock = clock
        self.stop_event = threading.Event()
        self.locks: ChainLocks | None = None
        self.loops: dict[int, ChainLoop] = {}

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        self.locks = self.locks_factory()
        logger.info("chain-runner 已启动")
        try:
            while not self.stop_event.is_set():
                close_old_connections()
                self.tick()
                close_old_connections()
                self.stop_event.wait(SUPERVISOR_TICK_SECONDS)
        finally:
            self.shutdown()

    def tick(self) -> None:
        try:
            self.locks.ping()
        except Exception:  # noqa: BLE001
            # 锁随连接一起丢了，别的实例可能已接管。不在进程内重连重建，整体重启最简单。
            logger.exception("advisory lock 连接失效，退出进程等待重启后重新抢锁")
            self.exit_process(1)
            return
        if self.check_watchdog():
            return
        try:
            self.reconcile(list(scannable_chains().only("pk", "code")))
            record_chain_runner_heartbeat()
        except Exception:  # noqa: BLE001
            # DB/缓存暂时不可用：已在跑的链不受影响，下个 tick 重试；心跳缺失会让
            # /health/workers 如实报出。
            logger.exception("chain-runner 巡检失败")

    def check_watchdog(self) -> bool:
        """有链的单轮超过硬上限即判定挂死并退出进程；返回是否已触发。"""
        now = self.clock()
        for loop in self.loops.values():
            started = loop.round_started_at
            if started is None or now - started <= SCAN_ROUND_HARD_LIMIT_SECONDS:
                continue
            logger.error(
                "链扫描单轮超过硬上限，判定线程挂死，退出进程等待重启",
                chain=loop.chain_code,
                elapsed_seconds=round(now - started),
                hard_limit_seconds=SCAN_ROUND_HARD_LIMIT_SECONDS,
            )
            # 打印全部线程栈，事后据此定位卡在哪个调用上。
            faulthandler.dump_traceback(all_threads=True)
            self.exit_process(1)
            return True
        return False

    def reconcile(self, chains: list[Chain]) -> None:
        """让运行中的循环与需要扫描的链集合一致。

        停止是非阻塞的：只置停止标志，等线程自己跑完当前一轮退出后，下一个 tick 再释放
        锁并移除。这样 Supervisor 不会被某条慢链卡住，看门狗也能覆盖正在停止的线程。
        """
        wanted = {chain.pk: chain for chain in chains}
        for chain_pk, loop in list(self.loops.items()):
            if chain_pk not in wanted:
                loop.stop()
            if not loop.is_alive():
                if not loop.stop_event.is_set():
                    logger.error(
                        "链扫描线程意外退出，将重新拉起", chain=loop.chain_code
                    )
                self.locks.unlock(chain_pk)
                del self.loops[chain_pk]
        for chain_pk, chain in wanted.items():
            if chain_pk in self.loops or not self.locks.try_lock(chain_pk):
                # 已在跑，或该链由别的实例持有。
                continue
            loop = self.loop_factory(chain)
            self.loops[chain_pk] = loop
            loop.start()
            logger.info("链扫描循环已启动", chain=chain.code)

    def shutdown(self) -> None:
        """停机：等各链跑完当前一轮再退出；关闭锁连接即释放全部锁。"""
        for loop in self.loops.values():
            loop.stop()
        for loop in self.loops.values():
            loop.join()
        self.loops.clear()
        if self.locks is not None:
            self.locks.close()
        logger.info("chain-runner 已停止")
