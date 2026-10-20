"""chain-runner：按链常驻的链上工作循环（manage.py run_chains）。

一条链上的链上工作本质是「一个有状态的常驻循环」：扫描入账、跟踪在途主动交易的终局、
按序发出待发交易，周而复始。这里直接按这个形状实现，不再借 Beat tick + 队列 + 单例锁
+ 时限去模拟循环：

- 一个进程，主线程是 Supervisor，每条活跃链一个 ChainLoop 线程。链上工作是 IO 密集型，
  等 RPC 时释放 GIL；一链一进程每个约 100MB 私有内存，不划算。
- 一轮固定三步：扫描 → 终局轮询 → 派发。扫描先刷新链高，轮询据此判定确认数、收口
  终局并腾出 pipeline 名额，派发最后按序发出新交易。各步独立预算、独立容错：任一步
  失败都不能连带跳过后续步骤——终局轮询曾挂在扫描之后，扫描一抛异常它就整段被跳过，
  部署/归集永不终局，pipeline 填满后整条链的主动交易全部停摆。
- 同一条链只有一个线程在发交易：EVM nonce 按序进入 mempool、Tron 同一发送地址串行
  （能量预检的前提）都由单线程直接保证，不再需要发送地址锁和广播接力。
- 同一条链只允许一个循环：PG 会话级 advisory lock 按链加锁。锁持有在专用连接上，
  进程退出或连接断开即自动释放，没有 TTL，也不受缓存淘汰影响。多开实例时后到者抢不到
  锁就空转，前一个实例退出后 ≤1 个 tick 接管。锁不承担资金正确性：入账按
  (chain, hash, event_index) 行锁去重；多实例交接的 ≤1 个 tick 窗口里，EVM 同一 nonce
  至多一笔上链，Tron 最坏对幂等的部署/归集多发一笔、多烧一次能量。
- 新建交易靠 PG LISTEN/NOTIFY 唤醒（chains.wakeup），通知在锁连接上接收；唤醒只补跑
  派发步骤。通知只是提前醒来的提示，派发永远按库里的任务状态工作，丢失的通知由下一轮
  照常接走。
- 单步耗时由协作式预算约束（chains.scanning）；预算管不到的挂死（典型如 getaddrinfo 在
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
from psycopg import sql

from chains.constants import ChainType
from chains.models import Chain
from chains.registry import get_chain_family
from chains.scanning import SCAN_ROUND_BUDGET_SECONDS
from chains.scanning import TX_DISPATCH_BUDGET_SECONDS
from chains.scanning import TX_POLL_BUDGET_SECONDS
from chains.scanning import ScanRoundBudgetExceeded
from chains.scanning import step_deadline
from chains.wakeup import CHAIN_TX_CHANNEL
from config.worker_health import record_chain_runner_heartbeat

if TYPE_CHECKING:
    from django.db.models import QuerySet

    from chains.registry import ChainFamily

logger = structlog.get_logger()

# advisory lock 两参数形式的第一个键（int4），把本模块的锁与其他使用方隔开；第二个键是链 pk。
CHAIN_LOCK_NAMESPACE = 0x58434852  # "XCHR"
SUPERVISOR_TICK_SECONDS = 5
# 单步的硬上限，超过即判定线程挂死。预算只在单元之间检查，一步最长是「预算 + 一个单元」。
# 单次调用的最坏耗时：EVM 不重试，最多 8s；Tron 每次逻辑调用最多 3 次尝试、每次 8s 超时，
# 加退避后「超时两次、第三次成功」最坏约 24.9s（三次都超时则抛错，单元提前结束）。
# - 扫描：预算 40s 加一个单元（Tron 一块 2 次调用，约 50s），约 90s。
# - Tron 轮询与首播：约 7 次调用，20s + 7 × 24.9s ≈ 194s。
# - Tron 重播（带回执预查）：约 14 次调用，理论上限 20s + 14 × 24.9s ≈ 369s，超过本值；
#   但需要几乎每次调用都恰好「超时两次、第三次成功」，现实中不会出现。即便触发，也只是
#   一次 crash-only 重启：广播尝试与签名载荷都在发 RPC 之前落库，重启后先按已知 hash 查回执。
# 300s 覆盖全部现实路径，只有真正的挂死（典型如 getaddrinfo 在 C 层阻塞）才会触发。
CHAIN_STEP_HARD_LIMIT_SECONDS = 300


def scannable_chains() -> QuerySet[Chain]:
    """需要扫描的链；runner 调度与 /health/scanning 的停滞判定共用这一口径。

    排除未配 API Key 的 Tron 链：CheckConstraint chain_active_requires_runtime_config
    目前已保证 active 的 Tron 链必有 key，这一句是为将来放宽约束时两边不分叉。
    停用链没有循环，其待发与在途主动交易随之冻结，重新启用后续跑。
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
    """在一条专用连接上持有各链的会话级 advisory lock，并接收新建交易的唤醒通知。

    不能用 Django 的线程连接持锁：close_old_connections() 会按 CONN_MAX_AGE 回收它，
    连接一关锁就丢了。这里另建一条不登记在 connections 里的连接，只有 Supervisor 线程
    使用、全程 autocommit（Django 建连默认）。语句直接走底层 psycopg 连接：Django 包装层
    的健康检查可能把失效连接悄悄关掉重连，新会话不持有任何锁，锁丢失就被掩盖了。

    唤醒通知也在这条连接上 LISTEN：它与锁同生共死，连接断开时进程本就会退出重启，
    不需要单独的监听重连。

    前提是直连 PG：若将来在应用与 PG 之间加事务级连接池，这条连接必须绕过连接池，
    否则会话级锁与 LISTEN 都会落到别的客户端会话上。
    """

    def __init__(self):
        self.wrapper = connections.create_connection("default")
        self.wrapper.ensure_connection()
        self.raw_connection = self.wrapper.connection
        self.raw_connection.execute(
            sql.SQL("LISTEN {}").format(sql.Identifier(CHAIN_TX_CHANNEL))
        )

    def execute(self, query: str, params: tuple = ()):
        return self.raw_connection.execute(query, params).fetchone()[0]

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

    def wait_for_notifies(self, timeout: float) -> list[str]:
        """最多等 timeout 秒，一收到唤醒通知就返回其 payload；超时返回空列表。

        psycopg 的 notifies() 在迭代期间一直持有连接内部的非重入锁，生成器没走完就在
        本连接上执行其他语句会永久卡住，所以这里当场把生成器消费完再返回。不在生成器
        里时到达的通知由 psycopg 暂存，下次调用最先返回，不会丢。
        """
        notifies = self.raw_connection.notifies(timeout=timeout, stop_after=1)
        return [notify.payload for notify in notifies]

    def close(self) -> None:
        self.wrapper.close()


class ChainLoop:
    """单条链的常驻线程：扫描 → 终局轮询 → 派发，等扫描周期后再来一轮。"""

    def __init__(self, chain: Chain):
        self.chain_pk = chain.pk
        self.chain_code = chain.code
        self.scan_interval_seconds = chain.scan_interval_seconds
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        # 当前这一步开始的 monotonic 时刻，空闲时为 None；看门狗据此判定挂死。
        self.step_started_at: float | None = None
        self.thread = threading.Thread(
            target=self.run, name=f"chain-{chain.code}", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        # 让等待中的线程立即醒来退出。
        self.wake_event.set()

    def wake(self) -> None:
        self.wake_event.set()

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def join(self) -> None:
        self.thread.join()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                # 每轮前后做与 HTTP 请求前后相同的连接清理：按 CONN_MAX_AGE
                # 回收、丢弃失效连接，PG 重启后下一轮自动重连。
                close_old_connections()
                self.run_once()
                close_old_connections()
                # 等到下一轮；期间每次被唤醒只补跑一次派发步骤。唤醒不提前整轮：否则
                # 高峰期每建一笔交易都把扫描提前一次，扫描 RPC 量随下单量放大。
                next_round_at = time.monotonic() + self.scan_interval_seconds
                while self.wait_for_wake(until=next_round_at):
                    close_old_connections()
                    self.run_dispatch_only()
                    close_old_connections()
        finally:
            # 线程自己的 DB 连接只能在本线程关闭。
            connections.close_all()

    def wait_for_wake(self, *, until: float) -> bool:
        """等到 until（monotonic）或被唤醒；被唤醒且未停止时返回 True。

        Event 只有一位状态，等待期间的多次唤醒合并为一次；派发每次都按库里全部到期
        任务处理，合并不会漏单。
        """
        remaining = until - time.monotonic()
        if self.stop_event.is_set() or remaining <= 0:
            return False
        if not self.wake_event.wait(remaining):
            return False
        self.wake_event.clear()
        return not self.stop_event.is_set()

    def run_once(self) -> None:
        """执行一整轮：扫描 → 终局轮询 → 派发。"""
        chain = self.load_active_chain()
        if chain is None:
            return
        family = get_chain_family(chain.type)
        self.run_scan_step(chain, family)
        for name, step, budget_seconds in (
            ("poll", family.poll_round, TX_POLL_BUDGET_SECONDS),
            ("dispatch", family.dispatch_round, TX_DISPATCH_BUDGET_SECONDS),
        ):
            if self.stop_event.is_set():
                return
            self.run_step(chain, name, step, budget_seconds)

    def run_dispatch_only(self) -> None:
        """被新建交易唤醒时只跑派发步骤。"""
        chain = self.load_active_chain()
        if chain is None:
            return
        family = get_chain_family(chain.type)
        self.run_step(
            chain, "dispatch", family.dispatch_round, TX_DISPATCH_BUDGET_SECONDS
        )

    def load_active_chain(self) -> Chain | None:
        """每次重读链配置，RPC 等改动在下一步即生效。

        链刚被停用时什么都不做，Supervisor 会在下一个 tick 停掉本线程。
        """
        try:
            chain = Chain.objects.get(pk=self.chain_pk)
        except Exception:  # noqa: BLE001
            logger.exception("读取链配置失败", chain=self.chain_code)
            return None
        return chain if chain.active else None

    def run_scan_step(self, chain: Chain, family: ChainFamily) -> None:
        """扫描一轮后立即推进 last_scanned_at。

        与原 Celery 扫描任务的 finally 语义一致：无论成败都推进，按固定周期重试，失败
        是否持续由游标 last_error 体现给 /health/scanning。放在扫描步骤之后而不是整轮
        之后：它表示「扫描完成于」，不应被同链轮询与派发的耗时推迟。
        """
        self.run_step(chain, "scan", family.scan_round, SCAN_ROUND_BUDGET_SECONDS)
        try:
            chain.mark_scanned()
        except Exception:  # noqa: BLE001
            logger.exception("推进 last_scanned_at 失败", chain=self.chain_code)

    def run_step(
        self,
        chain: Chain,
        name: str,
        step: Callable[[Chain, float], None],
        budget_seconds: float,
    ) -> None:
        """执行一步。任何异常都只记录，绝不连带跳过后续步骤，也不让线程退出。"""
        self.step_started_at = time.monotonic()
        try:
            step(chain, step_deadline(budget_seconds=budget_seconds))
        except ScanRoundBudgetExceeded:
            # 已完成单元的进度与游标错误均已由扫描器落盘，剩余区块留给下一轮。
            logger.warning("链扫描超出轮次预算", chain=self.chain_code)
        except Exception:  # noqa: BLE001
            logger.exception("链循环步骤失败", chain=self.chain_code, step=name)
        finally:
            self.step_started_at = None


class ChainRunner:
    """Supervisor：按活跃链集合启停 ChainLoop，并负责锁保活、唤醒、看门狗与心跳。"""

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
                self.wait_for_wakeups(SUPERVISOR_TICK_SECONDS)
        finally:
            self.shutdown()

    def wait_for_wakeups(self, timeout: float) -> None:
        """等到下一个 tick；期间收到新建交易的通知就唤醒对应的链线程。

        停止信号要等本次 notifies 等待结束（≤ timeout）才被看到，停机最多慢一个 tick。
        """
        deadline = time.monotonic() + timeout
        while not self.stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                payloads = self.locks.wait_for_notifies(remaining)
            except Exception:  # noqa: BLE001
                # 锁连接出了问题：交给下一个 tick 的 ping 判定并退出进程。
                logger.exception("等待唤醒通知失败")
                return
            for payload in payloads:
                self.wake(payload)

    def wake(self, payload: str) -> None:
        """唤醒 payload 指定的链；不在本实例运行的链（别的实例持锁、已停用）直接忽略。"""
        try:
            chain_pk = int(payload)
        except ValueError:
            logger.warning("忽略无法解析的唤醒通知", payload=payload)
            return
        loop = self.loops.get(chain_pk)
        if loop is not None:
            loop.wake()

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
        """有链的单步超过硬上限即判定挂死并退出进程；返回是否已触发。"""
        now = self.clock()
        for loop in self.loops.values():
            started = loop.step_started_at
            if started is None or now - started <= CHAIN_STEP_HARD_LIMIT_SECONDS:
                continue
            logger.error(
                "链循环单步超过硬上限，判定线程挂死，退出进程等待重启",
                chain=loop.chain_code,
                elapsed_seconds=round(now - started),
                hard_limit_seconds=CHAIN_STEP_HARD_LIMIT_SECONDS,
            )
            # 打印全部线程栈，事后据此定位卡在哪个调用上。
            faulthandler.dump_traceback(all_threads=True)
            self.exit_process(1)
            return True
        return False

    def reconcile(self, chains: list[Chain]) -> None:
        """让运行中的循环与需要运行的链集合一致。

        停止是非阻塞的：只置停止标志，等线程自己跑完当前一步退出后，下一个 tick 再释放
        锁并移除。这样 Supervisor 不会被某条慢链卡住，看门狗也能覆盖正在停止的线程。
        """
        wanted = {chain.pk: chain for chain in chains}
        for chain_pk, loop in list(self.loops.items()):
            if chain_pk not in wanted:
                loop.stop()
            if not loop.is_alive():
                if not loop.stop_event.is_set():
                    logger.error(
                        "链循环线程意外退出，将重新拉起", chain=loop.chain_code
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
            logger.info("链循环已启动", chain=chain.code)

    def shutdown(self) -> None:
        """停机：等各链跑完当前一步再退出；关闭锁连接即释放全部锁。"""
        for loop in self.loops.values():
            loop.stop()
        for loop in self.loops.values():
            loop.join()
        self.loops.clear()
        if self.locks is not None:
            self.locks.close()
        logger.info("chain-runner 已停止")
