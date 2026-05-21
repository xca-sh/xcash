from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

from chains.service import ChainService
from projects.models import Project
from projects.models import RecipientAddress
from projects.models import RecipientAddressUsage


class ProjectService:
    """集中封装 Project 相关的常用读取逻辑。"""

    @staticmethod
    def get_by_appid(appid: str) -> Project:
        return Project.retrieve(appid)

    @staticmethod
    def get_by_id(project_id: int) -> Project:
        return Project.objects.get(pk=project_id)

    @staticmethod
    def invoice_recipients(
        project: Project,
        *,
        chain_type: str | None = None,
    ) -> QuerySet[RecipientAddress]:
        # 账单支付只允许读取显式标记为 invoice 的收款地址，
        # 避免把仅用于归集的地址暴露给付款用户。
        qs = RecipientAddress.objects.filter(
            project=project,
            usage=RecipientAddressUsage.INVOICE,
        )
        if chain_type:
            qs = qs.filter(chain_type=chain_type)
        return qs

    @staticmethod
    def invoice_recipient_addresses(
        project: Project,
        *,
        chain_type: str | None = None,
    ) -> set[str]:
        return set(
            ProjectService.invoice_recipients(
                project,
                chain_type=chain_type,
            ).values_list("address", flat=True)
        )

    @staticmethod
    def primary_invoice_recipient(
        *,
        project: Project,
        chain_type: str,
    ) -> RecipientAddress | None:
        """取指定链类型下最早创建的账单收款地址。

        合约账单首次派生 collector 时用它锁定 recipient;后续应复用
        InvoicePaySlot 历史记录,避免商户调整收款地址导致 collector 地址漂移。
        """
        return (
            ProjectService.invoice_recipients(project, chain_type=chain_type)
            .order_by("created_at", "id")
            .first()
        )

    @staticmethod
    def has_invoice_recipient(project: Project) -> bool:
        return ProjectService.invoice_recipients(project).exists()

    @staticmethod
    def receivable_chain_codes(project: Project) -> set[str]:
        chain_types = set(
            ProjectService.invoice_recipients(project).values_list(
                "chain_type",
                flat=True,
            )
        )
        return ChainService.codes_of_types(chain_types)
