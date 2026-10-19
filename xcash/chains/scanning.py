"""扫描轮次预算：以协作方式限定单轮扫描的耗时。

扫描器按「单元」推进（EVM 一个 getLogs chunk、Tron 一个块），每完成一个单元就提交
进度。轮次预算只在单元之间检查：已完成的单元照常落盘，未扫部分留给下一轮，绝不会
像异步注入的超时异常那样在 DB 写入或事务中途打断。第一个单元之前不检查，保证每轮
至少推进一个单元。

预算耗尽按「本轮失败」处理：扫描器记录游标错误并上抛，/health/scanning 据此报
failing。持续撞预算意味着扫描速度追不上链头，必须对外可见。
"""

import time

SCAN_ROUND_BUDGET_SECONDS = 40


# 命名沿用 Celery SoftTimeLimitExceeded 的习惯（它是本异常取代的对象），不加 Error 后缀。
class ScanRoundBudgetExceeded(Exception):  # noqa: N818
    """本轮扫描已耗尽时间预算。"""

    def __init__(self):
        # 游标 last_error 直接存 str(exc)，带上类名便于运维从后台一眼认出失败类型。
        super().__init__(
            "ScanRoundBudgetExceeded: 本轮扫描超出时间预算，剩余区块留待下一轮"
        )


def scan_round_deadline(*, budget_seconds: float = SCAN_ROUND_BUDGET_SECONDS) -> float:
    """返回从现在起计算的本轮截止时刻（monotonic 时钟）。"""
    return time.monotonic() + budget_seconds


def check_scan_round_deadline(deadline: float | None) -> None:
    """已过截止时刻时抛出 ScanRoundBudgetExceeded；deadline 为 None 表示不限时。"""
    if deadline is not None and time.monotonic() >= deadline:
        raise ScanRoundBudgetExceeded
