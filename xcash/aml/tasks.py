from aml.service import AmlScreeningService
from procrastinate.contrib.django import app

from common.jobs import defer_once

SCREEN_TRANSFER_AML_TASK = "aml.screen_transfer_aml"


def enqueue_screen_transfer_aml(transfer_id: int) -> None:
    """在撮合事务内入队 AML 查询：与转账匹配同事务提交，提交后崩溃也不会漏查漏计费。"""
    defer_once(
        screen_transfer_aml,
        queueing_lock=f"{SCREEN_TRANSFER_AML_TASK}:{transfer_id}",
        transfer_id=transfer_id,
    )


@app.task(name=SCREEN_TRANSFER_AML_TASK)
def screen_transfer_aml(*, transfer_id: int) -> None:
    AmlScreeningService.screen_transfer(transfer_id)
