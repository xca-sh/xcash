from aml.service import AmlScreeningService
from celery import shared_task


@shared_task(ignore_result=True)
def screen_transfer_aml(transfer_id: int) -> None:
    AmlScreeningService.screen_transfer(transfer_id)
