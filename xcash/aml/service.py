from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import structlog
from aml.clients import QuicknodeMistTrackClient
from aml.models import Provider
from aml.models import RiskAssessment
from django.db import IntegrityError
from django.db import transaction
from django.utils import timezone

from chains.constants import ChainCode
from chains.models import Transfer
from chains.models import TransferStatus
from chains.models import TransferType
from common.saas_callback import CallbackEvent
from common.saas_callback import SaasCallback
from common.saas_callback import send_saas_callback
from core.runtime_settings import get_quicknode_misttrack_endpoint_url
from deposits.models import Deposit
from invoices.models import Invoice
from invoices.models import InvoiceStatus

if TYPE_CHECKING:
    from projects.models import Project

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
    """付款地址 AML 筛查。

    时序：转账一匹配为账单/充值收款就异步查询（与区块确认并行），结果挂在 Transfer 上；
    收款确认时由确认流程把结果写入账单/充值快照与 webhook，并上报 SaaS 计费。
    查询若晚于确认才完成，结果补写到已确认的收款上（webhook 不重发）。
    """

    @staticmethod
    def should_screen(*, project: Project, worth: Decimal, transfer: Transfer) -> bool:
        """匹配时判定这笔收款是否需要 AML 查询。

        同时决定账单能否走快速确认：需要查询的账单必须走完整确认，给查询留出时间。
        以下情况不查询、不写记录：项目未开启、价值不超过项目阈值（小额收款占绝大多数）、
        链不在 MistTrack 覆盖范围（测试网等）、付款地址未知、未配置查询 endpoint。
        """
        return (
            project.aml_screening_enabled
            and worth > project.aml_screening_threshold_usd
            and transfer.chain.code in QUICKNODE_MISTTRACK_CHAIN
            and not transfer.has_unknown_source_address()
            and bool(get_quicknode_misttrack_endpoint_url())
        )

    @classmethod
    def screen_transfer(cls, transfer_id: int) -> None:
        """查询一笔已匹配转账的付款地址风险，结果写到该 Transfer 的评估记录上。"""
        transfer = (
            Transfer.objects.select_related("chain").filter(pk=transfer_id).first()
        )
        if transfer is None:
            # 确认前因重组被删除，无需再查
            return
        endpoint_url = get_quicknode_misttrack_endpoint_url()
        misttrack_chain = QUICKNODE_MISTTRACK_CHAIN.get(transfer.chain.code)
        if not endpoint_url or misttrack_chain is None:
            return

        # 每笔都实时查询、不复用历史结果：地址风险随其资金来源随时变化，攻击者可以先用
        # 干净资金付款让地址被判低风险，再转入黑币付款。查询量由项目阈值控制。
        address = transfer.from_address
        defaults = {
            "source": Provider.QUICKNODE_MISTTRACK,
            "address": address,
            "tx_hash": transfer.hash,
            "checked_at": timezone.now(),
        }
        try:
            result = QuicknodeMistTrackClient(
                endpoint_url=endpoint_url
            ).address_risk_score(chain=misttrack_chain, address=address)
        except Exception as exc:
            logger.warning(
                "aml_screening.provider_failed",
                transfer_id=transfer.pk,
                address=address,
                error=str(exc),
            )
            defaults.update(
                status=RiskAssessment.Status.FAILED,
                risk_level=None,
                risk_score=None,
                raw_response={},
                error_message=str(exc)[:1000],
            )
        else:
            defaults.update(
                status=RiskAssessment.Status.SUCCESS,
                risk_level=result.risk_level,
                risk_score=result.risk_score,
                raw_response=result.raw_response or {},
                error_message="",
            )

        try:
            RiskAssessment.objects.update_or_create(
                transfer=transfer, defaults=defaults
            )
        except IntegrityError:
            # 查询期间 Transfer 因重组被删除，外键已不存在
            logger.info("aml_screening.transfer_dropped", transfer_id=transfer.pk)
            return

        cls._apply_if_confirmed(transfer.pk)

    @classmethod
    @transaction.atomic
    def _apply_if_confirmed(cls, transfer_id: int) -> None:
        """查询晚于确认完成时，把结果补写到已确认的账单/充值上。

        加 Transfer 行锁与 Transfer.confirm() 互斥：评估记录已先于本事务提交，
        确认事务要么已经提交（这里能查到已确认的收款，补写一次），要么尚未读取评估
        （它会自己读到这条记录），两条路径不会同时漏掉。重复补写与重复上报都是幂等的。
        """
        transfer = Transfer.objects.select_for_update().filter(pk=transfer_id).first()
        if transfer is None or transfer.status != TransferStatus.CONFIRMED:
            return

        target: Invoice | Deposit | None = None
        if transfer.type == TransferType.Invoice:
            target = (
                Invoice.objects.select_related("project")
                .filter(transfer=transfer, status=InvoiceStatus.COMPLETED)
                .first()
            )
        elif transfer.type == TransferType.Deposit:
            target = (
                Deposit.objects.select_related("customer__project")
                .filter(transfer=transfer)
                .first()
            )
        if target is not None:
            cls.apply_to_business(target)

    @staticmethod
    def apply_to_business(target: Invoice | Deposit) -> None:
        """把收款 Transfer 上的 AML 结果写入账单/充值快照，成功的查询在此上报计费。

        收款确认流程在构建 webhook 之前调用（确认事务内，持有 Transfer 行锁），
        查询晚到时由 _apply_if_confirmed 补调。计费只发生在收款确认之后：转账若在确认
        前因重组被丢弃，查询费不向商户收取。幂等键按收款单号构造，两条路径重复上报时
        SaaS 只扣一次；失败的查询不收费。
        """
        assessment = RiskAssessment.objects.filter(
            transfer_id=target.transfer_id
        ).first()
        if assessment is None:
            return

        target.risk_level = assessment.risk_level
        target.risk_score = assessment.risk_score
        type(target).objects.filter(pk=target.pk).update(
            risk_level=assessment.risk_level,
            risk_score=assessment.risk_score,
            updated_at=timezone.now(),
        )

        if assessment.status != RiskAssessment.Status.SUCCESS:
            return
        project = (
            target.project if isinstance(target, Invoice) else target.customer.project
        )
        send_saas_callback(
            SaasCallback(
                event=CallbackEvent.AML_SCREENED,
                appid=project.appid,
                sys_no=f"aml-screening:{target.sys_no}",
            )
        )
