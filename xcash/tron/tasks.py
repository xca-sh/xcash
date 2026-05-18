import structlog
from celery import shared_task
from tron.client import TronClientError
from tron.scanner import TronUsdtPaymentScanner

from chains.models import Chain
from chains.models import ChainType
from common.decorators import singleton_task

logger = structlog.get_logger()


@shared_task(ignore_result=True)
@singleton_task(timeout=48, use_params=True)
def scan_tron_chain(chain_pk: int) -> None:
    chain = Chain.objects.get(pk=chain_pk)
    if not chain.active:
        return
    if chain.type == ChainType.TRON and not chain.tron_api_key:
        logger.warning("Tron USDT 扫描跳过，缺少 API Key", chain=chain.code)
        return

    try:
        summary = TronUsdtPaymentScanner.scan_chain(chain=chain)
    except TronClientError:
        logger.warning("Tron USDT 扫描 RPC 失败", chain=chain.code)
        return

    logger.info(
        "Tron USDT 扫描完成",
        chain=chain.code,
        filter_addresses=summary.filter_addresses,
        blocks_scanned=summary.blocks_scanned,
        events_seen=summary.events_seen,
        created_transfers=summary.created_transfers,
    )


@shared_task(ignore_result=True)
@singleton_task(timeout=64)
def scan_active_tron_chains() -> None:
    for chain_pk in Chain.objects.filter(
        active=True,
        type=ChainType.TRON,
    ).exclude(tron_api_key="").values_list("pk", flat=True):
        scan_tron_chain.delay(chain_pk)
