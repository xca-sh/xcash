from __future__ import annotations

from tron.config import tron_vault_slot_runtime_ready

from chains.capabilities import ChainProductCapabilityService
from chains.constants import CHAIN_SPECS
from chains.models import ChainType
from chains.service import ChainService
from currencies.models import CryptoOnChain
from projects.models import InvoiceReceivingMode
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
    def contract_receivable_chain_codes(project: Project) -> set[str]:
        """VaultSlot 合约模式下项目可收款的链 code 集合。

        合约收款依赖对应链类型的项目不可变 vault 地址；Tron 只有 Nile 验证结论与
        factory/template/fee_limit 明确配置后才暴露，默认配置下始终只返回 EVM。
        """
        chain_codes = set()
        if project.evm_vault:
            chain_codes |= ChainService.codes_of_types({ChainType.EVM})
        if project.tron_vault and tron_vault_slot_runtime_ready():
            chain_codes |= set(
                CryptoOnChain.objects.filter(
                    chain__type=ChainType.TRON,
                    chain__active=True,
                    crypto__symbol="USDT",
                    crypto__active=True,
                    active=True,
                )
                .exclude(address="")
                .values_list("chain__code", flat=True)
            )

        # 主网/测试网门控：测试项目只收测试网链，非测试项目只收主网，隔离两类代币防混淆。
        return {
            code
            for code in chain_codes
            if CHAIN_SPECS[code].is_testnet == project.is_test
        }

    @staticmethod
    def invoice_receiving_mode_for_chain(*, project: Project, chain) -> str:
        """返回项目在该链类型下的账单收款模式，并校验链类型属于账单支持范围。"""
        # 显式按链类型取对应字段：新增链类型时此处会被强制改动，迫使为新链显式决策默认模式。
        mode_by_type = {
            ChainType.EVM: project.evm_invoice_receiving_mode,
            ChainType.TRON: project.tron_invoice_receiving_mode,
        }
        try:
            return mode_by_type[chain.type]
        except KeyError:
            raise ValueError(f"unsupported invoice chain_type={chain.type}") from None

    @staticmethod
    def invoice_receivable_methods(project: Project) -> dict[str, set[str]]:
        """返回项目当前账单收款模式下真实可用的 crypto -> chain 集合。"""
        from invoices.models import DifferRecipientAddress

        differ_chain_types = set(
            DifferRecipientAddress.objects.filter(
                project=project,
                active=True,
            ).values_list("chain_type", flat=True)
        )
        tokens = CryptoOnChain.objects.select_related("crypto", "chain").filter(
            crypto__active=True,
            chain__active=True,
            active=True,
        )

        methods: dict[str, set[str]] = {}
        for token in tokens:
            chain = token.chain
            crypto = token.crypto
            if CHAIN_SPECS[chain.code].is_testnet != project.is_test:
                continue
            if not ChainProductCapabilityService.supports_existing_invoice_method(
                chain=chain,
                crypto=crypto,
            ):
                continue

            mode = ProjectService.invoice_receiving_mode_for_chain(
                project=project,
                chain=chain,
            )
            if mode == InvoiceReceivingMode.VaultSlot:
                if not ProjectService._vault_slot_invoice_receiving_ready(
                    project=project,
                    chain=chain,
                ):
                    continue
            elif mode == InvoiceReceivingMode.Differ:
                if not ProjectService._differ_invoice_receiving_ready(
                    chain_type=chain.type,
                    crypto=crypto,
                    token_address=token.address,
                    differ_chain_types=differ_chain_types,
                ):
                    continue
            else:
                continue

            methods.setdefault(crypto.symbol, set()).add(chain.code)

        return methods

    @staticmethod
    def _vault_slot_invoice_receiving_ready(*, project: Project, chain) -> bool:
        if chain.type == ChainType.TRON:
            return bool(project.tron_vault) and tron_vault_slot_runtime_ready()
        if chain.type == ChainType.EVM:
            return bool(project.evm_vault)
        return False

    @staticmethod
    def _differ_invoice_receiving_ready(
        *,
        chain_type: str,
        crypto,
        token_address: str,
        differ_chain_types: set[str],
    ) -> bool:
        if chain_type not in differ_chain_types:
            return False
        if crypto.is_native:
            # 原生币差额收款仅在链能观测「EOA 收原生」时开放：Tron 逐块扫 TransferContract
            # 可观测 EOA 收原生；EVM 靠合约事件、原生打到 EOA 零事件，不可观测。
            return ChainProductCapabilityService.differ_supports_native(
                chain_type=chain_type
            )
        # 合约币（ERC20/TRC20）差额匹配按金额，仍要求该币在链上有合约地址。
        return bool(token_address)
