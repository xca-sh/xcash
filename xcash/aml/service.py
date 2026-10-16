from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from aml.clients import MistTrackAmlResult
from aml.clients import QuicknodeMistTrackClient
from aml.models import Provider
from aml.models import RiskAssessment
from django.db import transaction
from django.utils import timezone

from chains.constants import ChainCode
from core.runtime_settings import get_quicknode_misttrack_endpoint_url
from deposits.models import Deposit
from invoices.models import Invoice

logger = structlog.get_logger()


# xcash 链 code → mt_addressRiskScore 的 blockchain 参数取值。
# 取值表见 https://docs.misttrack.io/openapi/quicknode-add-on-docs ，只列 xcash 支持且
# MistTrack 覆盖的主网；Linea / Scroll 与全部测试网不在 MistTrack 覆盖范围内。
QUICKNODE_MISTTRACK_CHAIN: dict[str, str] = {
    ChainCode.Ethereum: "ETH",
    ChainCode.BSC: "BNB",
    ChainCode.Polygon: "Polygon",
    ChainCode.ArbitrumOne: "ARBITRUM",
    ChainCode.Optimism: "Optimism",
    ChainCode.Base: "Base",
    ChainCode.Avalanche: "AVAX",
    ChainCode.Tron: "TRON",
}


class AmlScreeningService:
    @classmethod
    def screen_invoice(cls, invoice_id: int) -> None:
        invoice = (
            Invoice.objects.select_related("transfer", "transfer__chain", "project")
            .filter(pk=invoice_id)
            .first()
        )
        if invoice is None or invoice.transfer_id is None:
            return

        # 以下两种"业务上根本不该走 AML"的场景直接返回，不写 RiskAssessment：
        # - 项目未开启 AML：会让该项目所有 invoice/deposit 各写一条无查询记录，纯垃圾数据。
        # - 价值不超过项目阈值：小额支付占绝大多数，要查"哪些单没 AML"看 invoice.risk_level IS NULL 即可。
        project = invoice.project
        if not project.aml_screening_enabled:
            return
        if invoice.worth <= project.aml_screening_threshold_usd:
            return

        cls._screen_target(
            target=invoice,
            target_type=RiskAssessment.TargetType.INVOICE,
        )

    @classmethod
    def screen_deposit(cls, deposit_id: int) -> None:
        deposit = (
            Deposit.objects.select_related(
                "transfer",
                "transfer__chain",
                "customer",
                "customer__project",
            )
            .filter(pk=deposit_id)
            .first()
        )
        if deposit is None:
            return

        # 同 screen_invoice，"业务上不该走 AML"的场景直接 return，不写 RiskAssessment。
        project = deposit.customer.project
        if not project.aml_screening_enabled:
            return
        if deposit.worth <= project.aml_screening_threshold_usd:
            return

        cls._screen_target(
            target=deposit,
            target_type=RiskAssessment.TargetType.DEPOSIT,
        )

    @classmethod
    def _screen_target(cls, *, target: Invoice | Deposit, target_type: str):
        transfer = target.transfer
        if transfer.has_unknown_source_address():
            logger.info(
                "aml_screening.source_address_unknown",
                target_type=target_type,
                target_id=target.pk,
                transfer_id=transfer.pk,
            )
            return

        endpoint_url = get_quicknode_misttrack_endpoint_url()
        if not endpoint_url:
            logger.info(
                "aml_screening.provider_not_configured",
                target_type=target_type,
                target_id=target.pk,
            )
            return

        # 每笔都实时查询、不复用历史结果：地址风险随其资金来源随时变化，攻击者可以先用
        # 干净资金付款让地址被判低风险，再在缓存期内转入黑币付款。查询量由项目阈值控制。
        address = transfer.from_address
        misttrack_chain = QUICKNODE_MISTTRACK_CHAIN.get(transfer.chain.code)
        if misttrack_chain is None:
            # MistTrack 只覆盖主网，测试网与未覆盖的链不查询、不写记录。
            logger.info(
                "aml_screening.unsupported_chain",
                target_type=target_type,
                target_id=target.pk,
                chain=transfer.chain.code,
            )
            return

        try:
            result = QuicknodeMistTrackClient(
                endpoint_url=endpoint_url
            ).address_risk_score(chain=misttrack_chain, address=address)
        except Exception as exc:
            logger.warning(
                "aml_screening.provider_failed",
                target_type=target_type,
                target_id=target.pk,
                address=address,
                error=str(exc),
            )
            cls._record_failed(target, target_type, str(exc))
            return

        cls._record_success(target, target_type, result)

    @classmethod
    def _record_success(
        cls,
        target: Invoice | Deposit,
        target_type: str,
        result: MistTrackAmlResult,
    ) -> None:
        defaults = {
            "source": Provider.QUICKNODE_MISTTRACK,
            "status": RiskAssessment.Status.SUCCESS,
            "target_type": target_type,
            "address": target.transfer.from_address,
            "tx_hash": target.transfer.hash,
            "risk_level": result.risk_level,
            "risk_score": result.risk_score,
            "raw_response": result.raw_response or {},
            "error_message": "",
            "checked_at": timezone.now(),
        }
        cls._upsert_assessment(target, target_type, defaults)
        cls._sync_snapshot(target, result.risk_level, result.risk_score)

    @classmethod
    def _record_failed(
        cls,
        target: Invoice | Deposit,
        target_type: str,
        error_message: str,
    ) -> None:
        cls._upsert_assessment(
            target,
            target_type,
            {
                "source": Provider.QUICKNODE_MISTTRACK,
                "status": RiskAssessment.Status.FAILED,
                "target_type": target_type,
                "address": target.transfer.from_address,
                "tx_hash": target.transfer.hash,
                "risk_level": None,
                "risk_score": None,
                "raw_response": {},
                "error_message": error_message[:1000],
                "checked_at": timezone.now(),
            },
        )
        cls._sync_snapshot(target, None, None)

    @staticmethod
    def _sync_snapshot(
        target: Invoice | Deposit, risk_level: str | None, risk_score: Decimal | None
    ) -> None:
        target.__class__.objects.filter(pk=target.pk).update(
            risk_level=risk_level,
            risk_score=risk_score,
            updated_at=timezone.now(),
        )

    @staticmethod
    @transaction.atomic
    def _upsert_assessment(
        target: Invoice | Deposit, target_type: str, defaults: dict[str, Any]
    ) -> None:
        lookup: dict[str, Any]
        if target_type == RiskAssessment.TargetType.INVOICE:
            lookup = {"invoice": target}
            defaults["deposit"] = None
        else:
            lookup = {"deposit": target}
            defaults["invoice"] = None
        RiskAssessment.objects.update_or_create(defaults=defaults, **lookup)
