from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import TYPE_CHECKING
from typing import Any

import structlog
from aml.clients import MistTrackAmlResult
from aml.clients import MistTrackOpenApiClient
from aml.clients import QuicknodeMistTrackClient
from aml.misttrack_coin_map import OPENAPI_EVM_COIN
from aml.misttrack_coin_map import OPENAPI_TRON_COIN
from aml.misttrack_coin_map import QUICKNODE_EVM_CHAIN
from aml.models import Provider
from aml.models import RiskAssessment
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from chains.models import Chain
from chains.models import ChainType
from common.permission_check import get_saas_risk_marking_enabled
from core.runtime_settings import get_aml_screening_cache_seconds
from core.runtime_settings import get_aml_screening_enabled
from core.runtime_settings import get_aml_screening_force_refresh_threshold_usd
from core.runtime_settings import get_aml_screening_threshold_usd
from core.runtime_settings import get_misttrack_openapi_api_key
from core.runtime_settings import get_quicknode_misttrack_endpoint_url
from deposits.models import Deposit
from invoices.models import Invoice

if TYPE_CHECKING:
    from currencies.models import Crypto

logger = structlog.get_logger()


class UnsupportedProviderChainError(RuntimeError):
    """provider 当前未覆盖该 chain/coin 的映射，无法发起 AML 查询。"""


class AmlScreeningService:
    @classmethod
    def screen_invoice(cls, invoice_id: int) -> None:
        invoice = (
            Invoice.objects.select_related(
                "transfer", "transfer__chain", "transfer__crypto", "project"
            )
            .filter(pk=invoice_id)
            .first()
        )
        if invoice is None or invoice.transfer_id is None:
            return

        # 以下三种"业务上根本不该走 AML"的场景直接返回，不写 RiskAssessment：
        # - SaaS tier 未开权限：每个商户每笔都写记录会污染审计数据，权限本身在 saas tier 可查。
        # - AML 总开关关闭：会让所有 invoice/deposit 各写一条无查询记录，纯垃圾数据。
        # - 价值低于阈值：小额支付占绝大多数，要查"哪些单没 AML"看 invoice.risk_level IS NULL 即可。
        if not cls._is_aml_screening_allowed(invoice):
            return
        if not get_aml_screening_enabled():
            return
        if invoice.worth <= get_aml_screening_threshold_usd():
            return

        cls._screen_target(
            target=invoice,
            target_type=RiskAssessment.TargetType.INVOICE,
            worth=invoice.worth,
        )

    @classmethod
    def screen_deposit(cls, deposit_id: int) -> None:
        deposit = (
            Deposit.objects.select_related(
                "transfer",
                "transfer__chain",
                "transfer__crypto",
                "customer",
                "customer__project",
            )
            .filter(pk=deposit_id)
            .first()
        )
        if deposit is None:
            return

        # 同 screen_invoice，"业务上不该走 AML"的场景直接 return，不写 RiskAssessment。
        if not cls._is_aml_screening_allowed(deposit):
            return
        if not get_aml_screening_enabled():
            return
        if deposit.worth <= get_aml_screening_threshold_usd():
            return

        cls._screen_target(
            target=deposit,
            target_type=RiskAssessment.TargetType.DEPOSIT,
            worth=deposit.worth,
        )

    @classmethod
    def _is_aml_screening_allowed(cls, target: Invoice | Deposit) -> bool:
        """SaaS 模式下按 tier 的 enable_risk_marking 判定；自托管模式直接放行。

        语义：SaaS 权限缓存中的 enable_risk_marking 控制是否允许本项目产生外部 AML 查询成本。
        - 自托管（IS_SAAS=False）→ 放行，保持独立部署旧行为。
        - SaaS 模式 + 缓存命中 → 按 enable_risk_marking 判定。
        - SaaS 模式 + 冷缓存 → fail-closed，避免在权限不明时产生 MistTrack 成本。
        """
        if not settings.IS_SAAS:
            return True

        if isinstance(target, Invoice):
            appid = target.project.appid
        else:
            appid = target.customer.project.appid

        enabled = get_saas_risk_marking_enabled(appid=appid)
        if enabled is None:
            logger.info(
                "aml_screening.saas_perm_unavailable",
                appid=appid,
                target_type=target.__class__.__name__,
                target_id=target.pk,
            )
            return False

        return enabled

    @classmethod
    def write_cache(
        cls,
        *,
        source: str,
        chain: str = "",
        address: str,
        result: dict[str, Any],
        timeout: int,
    ) -> None:
        cache.set(
            cls._cache_key(source=source, chain=chain, address=address),
            result,
            timeout,
        )

    @classmethod
    def _screen_target(cls, *, target: Invoice | Deposit, target_type: str, worth):
        transfer = target.transfer
        provider = cls._select_provider()
        if provider is None:
            logger.info(
                "aml_screening.provider_not_configured",
                target_type=target_type,
                target_id=target.pk,
            )
            return

        address = transfer.from_address
        cached_result = None
        if worth <= get_aml_screening_force_refresh_threshold_usd():
            cached_result = cache.get(
                cls._cache_key(
                    source=provider["source"],
                    chain=transfer.chain.code,
                    address=address,
                )
            )

        if cached_result is not None:
            cls._record_success(target, target_type, provider["source"], cached_result)
            return

        try:
            result = cls._query_provider(
                provider=provider,
                chain=transfer.chain,
                crypto=transfer.crypto,
                address=address,
            )
        except UnsupportedProviderChainError as exc:
            logger.info(
                "aml_screening.unsupported_provider_chain",
                source=provider["source"],
                target_type=target_type,
                target_id=target.pk,
                chain=transfer.chain.code,
                crypto=transfer.crypto.symbol,
                error=str(exc),
            )
            return
        except Exception as exc:
            logger.warning(
                "aml_screening.provider_failed",
                source=provider["source"],
                target_type=target_type,
                target_id=target.pk,
                address=address,
                error=str(exc),
            )
            cls._record_failed(target, target_type, str(exc), source=provider["source"])
            return

        payload = cls._result_to_cache_payload(result)
        cls.write_cache(
            source=provider["source"],
            chain=transfer.chain.code,
            address=address,
            result=payload,
            timeout=get_aml_screening_cache_seconds(),
        )
        cls._record_success(target, target_type, provider["source"], payload)

    @classmethod
    def _record_success(
        cls,
        target: Invoice | Deposit,
        target_type: str,
        source: str,
        payload: dict[str, Any],
    ) -> None:
        now = timezone.now()
        risk_score = (
            Decimal(str(payload["risk_score"]))
            if payload.get("risk_score") is not None
            else None
        )
        defaults = {
            "source": source,
            "status": RiskAssessment.Status.SUCCESS,
            "target_type": target_type,
            "address": target.transfer.from_address,
            "tx_hash": target.transfer.hash,
            "risk_level": payload.get("risk_level"),
            "risk_score": risk_score,
            "raw_response": payload.get("raw_response") or {},
            "error_message": "",
            "checked_at": now,
        }
        cls._upsert_assessment(target, target_type, defaults)
        cls._sync_snapshot(target, payload.get("risk_level"), risk_score)

    @classmethod
    def _record_failed(
        cls,
        target: Invoice | Deposit,
        target_type: str,
        error_message: str,
        *,
        source: str = Provider.QUICKNODE_MISTTRACK,
    ) -> None:
        cls._upsert_assessment(
            target,
            target_type,
            {
                "source": source,
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

    @staticmethod
    def _cache_key(*, source: str, address: str, chain: str = "") -> str:
        if chain:
            return f"aml:{source}:{chain}:{address.strip().lower()}"
        return f"aml:{source}:{address.strip().lower()}"

    @staticmethod
    def _result_to_cache_payload(result: MistTrackAmlResult) -> dict[str, Any]:
        payload = asdict(result)
        if payload["risk_score"] is not None:
            payload["risk_score"] = str(payload["risk_score"])
        return payload

    @staticmethod
    def _select_provider() -> dict[str, str] | None:
        api_key = get_misttrack_openapi_api_key()
        if api_key:
            return {"source": Provider.MISTTRACK_OPENAPI, "api_key": api_key}

        endpoint_url = get_quicknode_misttrack_endpoint_url()
        if endpoint_url:
            return {
                "source": Provider.QUICKNODE_MISTTRACK,
                "endpoint_url": endpoint_url,
            }

        return None

    @classmethod
    def _query_provider(
        cls, *, provider: dict[str, str], chain: Chain, crypto: Crypto, address: str
    ) -> MistTrackAmlResult:
        if provider["source"] == Provider.MISTTRACK_OPENAPI:
            coin = cls._misttrack_openapi_coin(chain=chain, crypto=crypto)
            return MistTrackOpenApiClient(
                api_key=provider["api_key"]
            ).address_risk_score(coin=coin, address=address)

        quicknode_chain = cls._quicknode_misttrack_chain(chain)
        return QuicknodeMistTrackClient(
            endpoint_url=provider["endpoint_url"]
        ).address_risk_score(chain=quicknode_chain, address=address)

    @staticmethod
    def _quicknode_misttrack_chain(chain: Chain) -> str:
        if chain.type == ChainType.TRON:
            return "TRX"
        if chain.type == ChainType.EVM and chain.chain_id in QUICKNODE_EVM_CHAIN:
            return QUICKNODE_EVM_CHAIN[chain.chain_id]
        raise UnsupportedProviderChainError(
            f"unsupported QuickNode MistTrack chain: {chain.code}"
        )

    @staticmethod
    def _misttrack_openapi_coin(*, chain: Chain, crypto: Crypto) -> str:
        symbol = crypto.symbol.upper()
        if chain.type == ChainType.TRON and symbol in OPENAPI_TRON_COIN:
            return OPENAPI_TRON_COIN[symbol]
        if chain.type == ChainType.EVM:
            mapping = OPENAPI_EVM_COIN.get(chain.chain_id)
            if mapping and symbol in mapping:
                return mapping[symbol]
        raise UnsupportedProviderChainError(
            f"unsupported MistTrack OpenAPI coin: {crypto.symbol} on {chain.code}"
        )
