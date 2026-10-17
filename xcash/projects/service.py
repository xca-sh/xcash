from __future__ import annotations

from chains.capabilities import ChainProductCapabilityService
from chains.constants import CHAIN_SPECS
from chains.models import ChainType
from chains.service import ChainService
from chains.vault_slots import is_chain_native_crypto
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
        """智能合约收款模式下项目可收款的链 code 集合。

        智能合约收款依赖对应链类型的项目不可变归集地址；Tron 的基础合约地址是
        发布前必须补齐的项目常量，不在业务入口重复做运行时门禁。
        """
        chain_codes = set()
        if project.evm_vault:
            chain_codes |= ChainService.codes_of_types({ChainType.EVM})
        if project.tron_vault:
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
        """返回项目在该链类型下实际生效的账单收款模式。"""
        return project.resolved_invoice_receiving_mode(chain.type)

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
                    chain=chain,
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
            return bool(project.tron_vault)
        if chain.type == ChainType.EVM:
            return bool(project.evm_vault)
        return False

    @staticmethod
    def _differ_invoice_receiving_ready(
        *,
        chain,
        crypto,
        token_address: str,
        differ_chain_types: set[str],
    ) -> bool:
        if chain.type not in differ_chain_types:
            return False
        # 按本链原生币判断：ETH 以 BEP20 上架 BSC 是合约币，Crypto.is_native 却为真，
        # 误判会把它当成 BSC 原生币排除出钱包直收。
        if is_chain_native_crypto(chain=chain, crypto=crypto):
            # 原生币钱包直收仅在链能观测「EOA 收原生」时开放：Tron 逐块扫 TransferContract
            # 可观测 EOA 收原生；EVM 靠合约事件、原生打到 EOA 零事件，不可观测。
            return ChainProductCapabilityService.differ_supports_native(
                chain_type=chain.type
            )
        # 合约币（ERC20/TRC20）钱包直收按金额匹配，仍要求该币在链上有合约地址。
        return bool(token_address)
