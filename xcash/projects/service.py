from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

from chains.service import ChainService
from projects.models import DifferRecipientAddress
from projects.models import Project


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
    ) -> QuerySet[DifferRecipientAddress]:
        qs = DifferRecipientAddress.objects.filter(project=project)
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
    ) -> DifferRecipientAddress | None:
        """取指定链类型下最早创建的差额账单收款地址。"""
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
