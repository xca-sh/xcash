from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django.db.models import QuerySet

from chains.models import ChainType
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
    def differ_receivable_chain_codes(project: Project) -> set[str]:
        """差额（DIFFER）模式下项目可收款的链 code 集合。

        差额收款依赖 DifferRecipientAddress：get_pay_differ 在该 chain_type 的收款地址集合上
        轮换 (地址, 金额) 组合。按已配置地址的 chain_type 展开为该类型下全部 active 链，
        EVM、Tron 均可走差额模式。
        """
        differ_types = set(
            ProjectService.invoice_recipients(project).values_list(
                "chain_type",
                flat=True,
            )
        )
        return ChainService.codes_of_types(differ_types)

    @staticmethod
    def contract_receivable_chain_codes(project: Project) -> set[str]:
        """合约（CONTRACT）模式下项目可收款的链 code 集合。

        合约收款依赖项目不可变 vault 地址，且仅 EVM 链支持（VaultSlot 智能合约）。设置 vault
        后即可在全部 active EVM 链通过 VaultSlot 收款，不依赖 DifferRecipientAddress；未设
        vault 则无法走合约模式，返回空集。
        """
        if not project.vault:
            return set()
        return ChainService.codes_of_types({ChainType.EVM})
