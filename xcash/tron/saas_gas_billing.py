from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from decimal import Decimal

import structlog
from celery import shared_task
from django.db import transaction
from tron.client import TronHttpClient

from chains.constants import ChainCode
from chains.models import Chain
from chains.models import TxTask
from chains.models import VaultSlot
from chains.models import VaultSlotCollectSchedule
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback

logger = structlog.get_logger()

GAS_FEE_CALLBACK_RETRY_BACKOFF_SECONDS = 60
TRON_ENERGY_MARKET_PRICE_ENERGY = Decimal("65000")
TRON_ENERGY_MARKET_PRICE_TRX = Decimal("3")


@dataclass(frozen=True, kw_only=True)
class TronTxDetail:
    gas_cost: str
    tx_hash: str
    chain: str
    fee_sun: int
    energy_usage_total: int
    net_usage: int
    native_price: str


def format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


def int_payload_value(payload: dict, key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def build_tx_detail(*, chain: Chain, tx_hash: str) -> TronTxDetail:
    payload = TronHttpClient(chain=chain).get_transaction_info_by_id(tx_hash)
    fee_sun = int_payload_value(payload, "fee")
    receipt = payload.get("receipt") or {}
    if not isinstance(receipt, dict):
        receipt = {}
    energy_usage_total = int_payload_value(receipt, "energy_usage_total")
    native_crypto = chain.native_coin
    native_decimals = native_crypto.get_decimals(chain)
    native_price = native_crypto.usd_amount(Decimal(1))
    burned_trx = Decimal(fee_sun).scaleb(-native_decimals)
    energy_cost_trx = (
        Decimal(energy_usage_total)
        * TRON_ENERGY_MARKET_PRICE_TRX
        / TRON_ENERGY_MARKET_PRICE_ENERGY
    )
    gas_cost = (burned_trx + energy_cost_trx) * native_price
    return TronTxDetail(
        gas_cost=format_decimal(gas_cost),
        tx_hash=tx_hash,
        chain=ChainCode(chain.code).label,
        fee_sun=fee_sun,
        energy_usage_total=energy_usage_total,
        net_usage=int_payload_value(receipt, "net_usage"),
        native_price=format_decimal(native_price),
    )


def notify_vault_slot_deploy_gas_fee(*, tx_task: TxTask) -> None:
    if not tx_task.tx_hash:
        return
    try:
        send_vault_slot_deploy_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tron_saas_gas_fee_callback_build_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task.pk,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        transaction.on_commit(
            lambda tx_task_id=tx_task.pk: retry_vault_slot_deploy_gas_fee.delay(
                tx_task_id
            )
        )
        return


def send_vault_slot_deploy_gas_fee(*, tx_task: TxTask) -> None:
    slot = (
        VaultSlot.objects.select_related("project", "chain").get(deploy_tx_task=tx_task)
    )
    tx_detail = build_tx_detail(chain=slot.chain, tx_hash=tx_task.tx_hash)
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_DEPLOY,
            appid=slot.project.appid,
            sys_no=f"tron-vault-slot-deploy:{tx_task.pk}",
            currency="USDT",
            tx_detail=asdict(tx_detail),
        )
    )


def notify_vault_slot_collect_gas_fee(*, tx_task: TxTask) -> None:
    """归集任务确认终局后回调 SaaS 计费。

    归集交易的资金接收方是系统外的商户 vault,不会被扫描器当作入账观测;
    因此 slot/project 经归集计划(VaultSlotCollectSchedule.tx_task)反查,
    而非依赖归集 Transfer。sys_no 以 tx_task.pk 收敛,保证一次
    归集任务恰好计费一次。
    """
    if not tx_task.tx_hash:
        return
    try:
        send_vault_slot_collect_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tron_saas_gas_fee_callback_build_failed",
            operation="vault_slot_collect",
            tx_task_id=tx_task.pk,
            tx_hash=tx_task.tx_hash,
            error=str(exc),
        )
        transaction.on_commit(
            lambda tx_task_id=tx_task.pk: retry_vault_slot_collect_gas_fee.delay(
                tx_task_id
            )
        )
        return


def send_vault_slot_collect_gas_fee(*, tx_task: TxTask) -> None:
    schedule = VaultSlotCollectSchedule.objects.select_related(
        "vault_slot__project",
        "chain",
    ).get(tx_task=tx_task)
    slot = schedule.vault_slot
    tx_detail = build_tx_detail(chain=schedule.chain, tx_hash=tx_task.tx_hash)
    send_saas_callback(
        SaasCallback(
            event=CallbackEvent.GAS_FEE_VAULT_SLOT_COLLECT,
            appid=slot.project.appid,
            sys_no=f"tron-vault-slot-collect:{tx_task.pk}",
            currency="USDT",
            tx_detail=asdict(tx_detail),
        )
    )


@shared_task(bind=True, ignore_result=True, max_retries=20)
def retry_vault_slot_deploy_gas_fee(self, tx_task_id: int) -> None:
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_deploy_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tron_saas_gas_fee_callback_retry_failed",
            operation="vault_slot_deploy",
            tx_task_id=tx_task_id,
            error=str(exc),
            retry=self.request.retries,  # noqa
        )
        raise self.retry(
            exc=exc,
            countdown=GAS_FEE_CALLBACK_RETRY_BACKOFF_SECONDS
            * (2**self.request.retries),  # noqa
        ) from exc


@shared_task(bind=True, ignore_result=True, max_retries=20)
def retry_vault_slot_collect_gas_fee(self, tx_task_id: int) -> None:
    tx_task = TxTask.objects.get(pk=tx_task_id)
    try:
        send_vault_slot_collect_gas_fee(tx_task=tx_task)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tron_saas_gas_fee_callback_retry_failed",
            operation="vault_slot_collect",
            tx_task_id=tx_task_id,
            error=str(exc),
            retry=self.request.retries,  # noqa
        )
        raise self.retry(
            exc=exc,
            countdown=GAS_FEE_CALLBACK_RETRY_BACKOFF_SECONDS
            * (2**self.request.retries),  # noqa
        ) from exc
