"""链循环各步的时间预算：以协作方式限定单步耗时。

chain-runner 每轮依次执行扫描、终局轮询、派发三步（见 chains.runner），每步按「单元」
推进：扫描是 EVM 一个 getLogs chunk / Tron 一个块，轮询是一个在途任务，派发是一笔广播。
预算只在单元之间检查：已完成的单元照常落盘，剩余部分留给下一轮，绝不会像异步注入的
超时异常那样在 DB 写入或事务中途打断。第一个单元之前不检查，保证每步每轮至少推进一个
单元。各步各算各的预算：扫描落后链头时每轮都会用满 40s，若三步共用一个截止时刻，
轮询与派发会被长期饿死。

预算耗尽的含义按步区分：
- 扫描：按「本轮失败」处理。扫描器记录游标错误并抛 ScanRoundBudgetExceeded，
  /health/scanning 据此报 failing——持续撞预算意味着追不上链头，必须对外可见。
- 轮询与派发：只是「剩下的下轮再做」，用 deadline_passed() 判断后直接停手，不算错误。
"""

import time

SCAN_ROUND_BUDGET_SECONDS = 40
TX_POLL_BUDGET_SECONDS = 20
TX_DISPATCH_BUDGET_SECONDS = 20


# 命名沿用 Celery SoftTimeLimitExceeded 的习惯（它是本异常取代的对象），不加 Error 后缀。
class ScanRoundBudgetExceeded(Exception):  # noqa: N818
    """本轮扫描已耗尽时间预算。"""

    def __init__(self):
        # Chain.scan_last_error 直接存 str(exc)，带上类名便于运维从后台一眼认出失败类型。
        super().__init__(
            "ScanRoundBudgetExceeded: 本轮扫描超出时间预算，剩余区块留待下一轮"
        )


def step_deadline(*, budget_seconds: float) -> float:
    """返回从现在起计算的本步截止时刻（monotonic 时钟）。"""
    return time.monotonic() + budget_seconds


def deadline_passed(deadline: float | None) -> bool:
    """是否已过截止时刻；deadline 为 None 表示不限时。"""
    return deadline is not None and time.monotonic() >= deadline


def check_scan_round_deadline(deadline: float | None) -> None:
    """已过截止时刻时抛出 ScanRoundBudgetExceeded，供扫描器在单元之间调用。"""
    if deadline_passed(deadline):
        raise ScanRoundBudgetExceeded
