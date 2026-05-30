from decimal import Decimal

from django.core.exceptions import ObjectDoesNotExist
from django.urls import reverse
from django_otp.plugins.otp_email.conf import settings
from rest_framework import serializers
from rest_framework.exceptions import ValidationError
from rest_framework.serializers import Serializer

from chains.serializers import TransferSerializer
from chains.service import ChainService
from common.consts import APPID_HEADER
from common.consts import MAX_INVOICE_DURATION
from common.consts import MIN_INVOICE_DURATION
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.serializers import StrippedDecimalField
from currencies.service import CryptoService
from currencies.service import FiatService
from projects.service import ProjectService

from .models import Invoice
from .models import InvoiceBillingMode
from .models import InvoiceProtocol
from .models import InvoiceStatus


class InvoiceSetCryptoChainSerializer(Serializer):
    crypto = serializers.CharField(required=True)
    chain = serializers.CharField(required=True)

    def validate_crypto(self, value):  # noqa
        if value and not CryptoService.exists(value):
            raise ValidationError(detail=ErrorCode.INVALID_CRYPTO.to_payload())
        return value

    def validate_chain(self, value):  # noqa
        if not value:
            return value
        try:
            ChainService.get_by_code(value)
        except ObjectDoesNotExist as exc:
            raise ValidationError(detail=ErrorCode.INVALID_CHAIN.to_payload()) from exc
        return value

    def validate(self, attrs):
        if not self._is_chain_crypto_supported(attrs):
            raise APIError(ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT)
        return attrs

    @staticmethod
    def _is_chain_crypto_supported(attrs) -> bool:
        if not attrs["chain"] or not attrs["crypto"]:
            return False
        try:
            chain = ChainService.get_by_code(attrs["chain"])
            crypto = CryptoService.get_by_symbol(attrs["crypto"])
        except ObjectDoesNotExist:
            return False
        return crypto.support_this_chain(chain)


class InvoiceCreateSerializer(Serializer):
    out_no = serializers.CharField(required=True, max_length=32)
    title = serializers.CharField(required=True, max_length=32)
    currency = serializers.CharField(required=True, max_length=8)
    amount = serializers.DecimalField(
        required=True,
        max_digits=32,
        decimal_places=8,
        min_value=Decimal("0.00000001"),
        max_value=Decimal(
            "1000000"
        ),  # 单笔上限 100 万，防止天文数字金额干扰汇率换算和差额分配
    )
    duration = serializers.IntegerField(
        required=False,
        default=10,
        min_value=MIN_INVOICE_DURATION,
        max_value=MAX_INVOICE_DURATION,
    )
    methods = serializers.JSONField(required=False, default=dict)
    notify_url = serializers.URLField(required=False)
    return_url = serializers.URLField(required=False)
    billing_mode = serializers.ChoiceField(
        choices=InvoiceBillingMode,
        default=InvoiceBillingMode.DIFFER,
        required=False,
    )

    def _get_project(self):
        # 缓存到实例，避免 validate_out_no / validate_methods / validate 三处重复查询。
        if not hasattr(self, "_project"):
            request = self.context["request"]
            self._project = ProjectService.get_by_appid(
                request.headers.get(APPID_HEADER)
            )
        return self._project

    def finalize_methods(self, *, project, billing_mode, requested):
        """按 billing_mode 生成/收敛最终 methods——账单可付组合的唯一真源。

        available_methods(project, billing_mode) 已是该模式下真正可付的 crypto→链集合
        （合约需 vault 且仅 EVM；差额需对应 chain_type 的收款地址）。
        - 商户未传 methods → 直接采用全部可用方式（系统按 billing_mode 动态生成）。
        - 商户传了 methods → 与可用集合逐项求交集校验，任何越界组合直接拒绝。
        """
        available = Invoice.available_methods(project, billing_mode)
        if not available:
            raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS)

        if not requested:
            return available

        if not isinstance(requested, dict):
            raise APIError(ErrorCode.PARAMETER_ERROR, detail="methods")

        sanitized: dict[str, list[str]] = {}
        for crypto_symbol, chain_codes in requested.items():
            if not isinstance(chain_codes, (list, tuple)):
                raise APIError(ErrorCode.PARAMETER_ERROR, detail=crypto_symbol)

            try:
                CryptoService.get_by_symbol(crypto_symbol)
            except ObjectDoesNotExist as exc:
                raise APIError(ErrorCode.INVALID_CRYPTO, detail=crypto_symbol) from exc

            available_chains = set(available.get(crypto_symbol, []))
            if not available_chains:
                raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS, detail=crypto_symbol)

            normalized_codes: list[str] = []
            for chain_code in chain_codes:
                if not isinstance(chain_code, str):
                    raise APIError(ErrorCode.PARAMETER_ERROR, detail=crypto_symbol)

                try:
                    ChainService.get_by_code(chain_code)
                except ObjectDoesNotExist as exc:
                    raise APIError(ErrorCode.INVALID_CHAIN, detail=chain_code) from exc
                if chain_code not in available_chains:
                    raise APIError(
                        ErrorCode.NO_RECIPIENT_ADDRESS,
                        detail=f"{crypto_symbol}:{chain_code}",
                    )
                normalized_codes.append(chain_code)

            if normalized_codes:
                sanitized[crypto_symbol] = normalized_codes

        if not sanitized:
            raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS)

        return sanitized

    def validate_currency(self, value):  # noqa
        if not (CryptoService.exists(value) or FiatService.exists(value)):
            raise APIError(ErrorCode.INVALID_INVOICE_CURRENCY)
        return value

    def validate_out_no(self, value):
        project = self._get_project()
        if Invoice.objects.filter(project=project, out_no=value).exists():
            raise APIError(ErrorCode.DUPLICATE_OUT_NO, detail=value)
        return value

    def validate(self, attrs):
        project = self._get_project()

        if not settings.DEBUG and (
            Invoice.objects.filter(
                project=project, status=InvoiceStatus.WAITING
            ).count()
            >= 100
        ):
            raise APIError(ErrorCode.TOO_MANY_WAITING)

        # billing_mode 决定可付组合：合约（EVM+vault）与差额（对应 chain_type 收款地址）
        # 各自的可用方式不同，最终 methods 由 finalize_methods 按模式动态生成/收敛。
        attrs["methods"] = self.finalize_methods(
            project=project,
            billing_mode=attrs.get("billing_mode", InvoiceBillingMode.DIFFER),
            requested=attrs.get("methods") or {},
        )

        # 计价货币为加密货币时，账单只为该币种收款，methods 收敛到单一币种。
        if CryptoService.exists(attrs["currency"]):
            currency = attrs["currency"]
            chains = attrs["methods"].get(currency, [])
            if not chains:
                raise APIError(ErrorCode.NO_AVAILABLE_METHOD)
            attrs["methods"] = {currency: chains}

        return attrs


class InvoicePublicSerializer(serializers.ModelSerializer):
    """公开 API（无需鉴权的 retrieve 端点）专用序列化器。

    仅暴露买家付款所需的最小字段集，不包含 appid、out_no 等商户内部信息。
    """

    crypto = serializers.CharField(
        source="crypto.symbol", read_only=True, allow_null=True
    )
    chain = serializers.CharField(source="chain.code", read_only=True, allow_null=True)
    amount = StrippedDecimalField(max_digits=32, decimal_places=8)
    pay_amount = StrippedDecimalField(max_digits=32, decimal_places=8)
    pay_url = serializers.SerializerMethodField()
    # 公开支付页用的 return_url：对 EPay V1 协议、且订单已完成时，注入带签名的
    # 同步跳转 query，让浏览器按 EPay V1 规范跳回商户站点完成对账；其他场景
    # 直接透传商户配置的原始 return_url（兼容 native 协议）。
    return_url = serializers.SerializerMethodField()
    payment = TransferSerializer(source="transfer", read_only=True)

    def get_pay_url(self, obj: Invoice) -> str:
        pay_path = reverse("payment-invoice", kwargs={"sys_no": obj.sys_no})
        request = self.context.get("request")
        if request is None:
            return pay_path
        django_request = getattr(request, "_request", request)
        return django_request.build_absolute_uri(pay_path)

    def get_return_url(self, obj: Invoice) -> str:
        if (
            obj.protocol == InvoiceProtocol.EPAY_V1
            and obj.status == InvoiceStatus.COMPLETED
        ):
            # lazy import 避免 serializers ↔ epay_service 顶层循环依赖。
            from .epay_service import EpaySubmitService

            signed = EpaySubmitService.build_return_url(obj)
            if signed:
                return signed
        return obj.return_url

    class Meta:
        model = Invoice
        fields = (
            "sys_no",
            "title",
            "currency",
            "amount",
            "methods",
            "chain",
            "crypto",
            "crypto_address",
            "pay_address",
            "pay_amount",
            "pay_url",
            "started_at",
            "created_at",
            "expires_at",
            "return_url",
            "payment",
            "status",
            "risk_level",
            "risk_score",
        )


class InvoiceDisplaySerializer(serializers.ModelSerializer):
    """商户侧（需要鉴权的 create 响应）序列化器，包含完整商户信息。"""

    appid = serializers.CharField(
        source="project.appid", read_only=True, allow_null=True
    )
    crypto = serializers.CharField(
        source="crypto.symbol", read_only=True, allow_null=True
    )
    chain = serializers.CharField(source="chain.code", read_only=True, allow_null=True)
    amount = StrippedDecimalField(max_digits=32, decimal_places=8)
    pay_amount = StrippedDecimalField(max_digits=32, decimal_places=8)

    pay_url = serializers.SerializerMethodField()
    payment = TransferSerializer(source="transfer", read_only=True)

    def get_pay_url(self, obj: Invoice) -> str:
        pay_path = reverse("payment-invoice", kwargs={"sys_no": obj.sys_no})
        request = self.context.get("request")
        if request is None:
            return pay_path
        django_request = getattr(request, "_request", request)
        return django_request.build_absolute_uri(pay_path)

    class Meta:
        model = Invoice
        fields = (
            "appid",
            "sys_no",
            "out_no",
            "title",
            "currency",
            "amount",
            "methods",
            "chain",
            "crypto",
            "crypto_address",
            "pay_address",
            "pay_amount",
            "pay_url",
            "started_at",
            "created_at",
            "expires_at",
            "notify_url",
            "return_url",
            "payment",
            "status",
            "risk_level",
            "risk_score",
        )
