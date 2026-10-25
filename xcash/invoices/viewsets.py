from collections.abc import Mapping
from datetime import timedelta

import structlog
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.utils import timezone
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from chains.service import ChainService
from common.consts import APPID_HEADER
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.permission_check import check_saas_permission
from common.permissions import RejectAll
from common.throttles import InvoiceRetrieveThrottle
from common.throttles import InvoiceSelectMethodThrottle
from currencies.models import PriceUnavailableError
from currencies.service import CryptoService
from projects.models import Project

from .exceptions import InvoiceStatusError
from .models import Invoice
from .models import InvoiceProtocol
from .models import InvoiceStatus
from .serializers import InvoiceCreateSerializer
from .serializers import InvoiceDisplaySerializer
from .serializers import InvoicePublicSerializer
from .serializers import InvoiceSetCryptoChainSerializer

# 下面两个 TTL 常量是向后兼容 re-export。noqa 行内只放 pragma：black 会把超长行拆开，
# 带说明文字的 noqa 会被挪到 `)` 后失效，ruff --fix 进而把这个 re-export 当未使用删掉。
from .service import INVOICE_PUBLIC_NON_WAITING_CACHE_TTL  # noqa: F401
from .service import INVOICE_PUBLIC_WAITING_CACHE_TTL  # noqa: F401
from .service import InvoiceService
from .service import invoice_public_cache_key
from .service import invoice_public_cache_ttl

logger = structlog.get_logger()


def plain_cache_data(value):
    if isinstance(value, Mapping):
        return {key: plain_cache_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain_cache_data(item) for item in value]
    return value


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = Invoice.objects.all()
    lookup_field = "sys_no"
    # 公开端点不需要身份认证；类级别移除 SessionAuthentication 同时消除 CSRF 强制检查，
    # 避免同源前端页面因携带 session cookie 而触发 CSRF 403。
    # 非公开 action 已由 RejectAll 拦截，无需依赖 authentication_classes。
    authentication_classes = []

    def get_serializer_class(self):
        if self.action == "create":
            return InvoiceCreateSerializer
        elif self.action == "select_method":
            return InvoiceSetCryptoChainSerializer
        elif self.action == "retrieve":
            # 公开端点使用精简序列化器，不暴露 appid/out_no 等商户内部信息。
            return InvoicePublicSerializer
        # 修复：RejectAll 覆盖了其他动作，但 DRF 在生成 schema / OPTIONS 时仍会访问 serializer；
        # 这里返回展示序列化器，避免无意义的 500。
        return InvoiceDisplaySerializer

    def get_permissions(self):
        if self.action in ("create", "select_method", "retrieve"):
            permission_classes = [AllowAny]
        else:
            permission_classes = [RejectAll]
        return [permission() for permission in permission_classes]

    def get_throttles(self):
        # retrieve 是前端公开轮询端点，select_method 则会消耗支付槽位配额。
        # 两者必须拆分限流桶，避免状态轮询意外耗尽切换支付方式额度。
        if self.action == "retrieve":
            return [InvoiceRetrieveThrottle()]
        if self.action == "select_method":
            return [InvoiceSelectMethodThrottle()]
        return super().get_throttles()

    def retrieve(self, request, *args, **kwargs):
        sys_no = kwargs[self.lookup_url_kwarg or self.lookup_field]
        cache_key = invoice_public_cache_key(sys_no)
        cached_data = cache.get(cache_key)
        if cached_data is not None:
            return Response(cached_data)

        invoice: Invoice = self.get_object()
        data = plain_cache_data(self.get_serializer(invoice).data)
        cache.set(
            cache_key,
            data,
            timeout=invoice_public_cache_ttl(invoice.status),
        )
        return Response(data)

    def create(self, request, *args, **kwargs):
        # SaaS 模式：Invoice 收款这里只校验账号状态。
        check_saas_permission(
            appid=request.headers.get(APPID_HEADER),  # noqa
            action="invoice",
        )

        serializer = self.get_serializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            raise APIError(ErrorCode.PARAMETER_ERROR, detail=serializer.errors)
        validated_data = serializer.validated_data

        project = Project.retrieve(appid=request.headers.get(APPID_HEADER))  # noqa

        existing = Invoice.objects.filter(
            project=project, out_no=validated_data["out_no"]
        ).first()
        if existing is not None:
            return self.idempotent_existing_invoice_response(
                request, existing, validated_data
            )

        # 待支付容量与 methods 收敛只约束真正的新建，必须放在已有账单判定之后：
        # 否则项目待支付满额、或商户事后关闭了原支付方式时，原请求的合法重试
        # 会被拒，拿不回已建好的账单（methods 本就不参与幂等比较）。
        InvoiceService.ensure_waiting_capacity(project)
        methods = InvoiceService.finalize_methods(
            project=project,
            requested=validated_data.get("methods") or {},
        )

        try:
            # 内层 atomic 建立保存点，与 epay 建单路径一致。
            # 全局 ATOMIC_REQUESTS=True 让整个请求跑在一个事务里：不套保存点的话，
            # INSERT 撞唯一约束会把连接置为 needs_rollback，下面兜底分支的重查会抛
            # TransactionManagementError，商户并发重试同一 out_no 时拿到的是 500
            # 而不是设计好的幂等响应。
            with db_transaction.atomic():
                invoice = Invoice.objects.create(
                    project=project,
                    out_no=validated_data["out_no"],
                    title=validated_data["title"],
                    currency_id=validated_data["currency"],
                    amount=validated_data["amount"],
                    methods=methods,
                    notify_url=validated_data.get("notify_url", ""),
                    return_url=validated_data.get("return_url", ""),
                    expires_at=timezone.now()
                    + timedelta(minutes=validated_data["duration"]),
                )
        except IntegrityError as exc:
            # 与并发创建撞唯一约束：重查后按同一幂等口径处理，避免商户
            # 并发重试时一个 201 一个 4xx 的不稳定语义。
            existing = Invoice.objects.filter(
                project=project, out_no=validated_data["out_no"]
            ).first()
            if existing is None:
                raise APIError(
                    ErrorCode.DUPLICATE_OUT_NO, detail=validated_data["out_no"]
                ) from exc
            return self.idempotent_existing_invoice_response(
                request, existing, validated_data
            )
        # 账单初始化副作用显式走 service，替代隐式 post_save signal。
        InvoiceService.initialize_invoice(invoice)

        return Response(
            InvoiceDisplaySerializer(
                invoice,
                context={
                    "request": request,
                },
            ).data,
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def invoice_matches_create_request(invoice: Invoice, validated_data: dict) -> bool:
        """条件幂等的判据：关键商务参数与已有账单全等才视为同一请求的重试。

        duration/expires_at 与 methods 不参与比较：前者随请求时间天然漂移，
        后者是项目可用方法收敛的结果，项目配置变更会让合法重试失配。
        协议必须是 NATIVE——EPay 建的账单共用 out_no 命名空间，不能跨协议幂等返回。
        """
        return (
            invoice.protocol == InvoiceProtocol.NATIVE
            and invoice.title == validated_data["title"]
            and invoice.currency_id == validated_data["currency"]
            and invoice.amount == validated_data["amount"]
            and invoice.notify_url == validated_data.get("notify_url", "")
            and invoice.return_url == validated_data.get("return_url", "")
        )

    def idempotent_existing_invoice_response(
        self, request, existing: Invoice, validated_data: dict
    ) -> Response:
        """out_no 已存在时的条件幂等：参数一致返回已有账单，不一致报重复单号。

        无条件返回会让「同 out_no 但金额/币种/回调已变」的请求静默拿到旧账单，
        掩盖商户侧 bug；报错则保留了真正冲突的可见性。
        """
        if not self.invoice_matches_create_request(existing, validated_data):
            raise APIError(ErrorCode.DUPLICATE_OUT_NO, detail=existing.out_no)
        return Response(
            InvoiceDisplaySerializer(existing, context={"request": request}).data,
            status=status.HTTP_200_OK,
        )

    @action(methods=["post"], detail=True, url_path="select-method")
    def select_method(self, request, *args, **kwargs):
        serializer = self.get_serializer(
            data=request.data, context={"request": request}
        )
        if not serializer.is_valid():
            raise APIError(ErrorCode.PARAMETER_ERROR, detail=serializer.errors)
        validated_data = serializer.validated_data

        invoice: Invoice = self.get_object()

        if invoice.status != InvoiceStatus.WAITING or invoice.transfer_id is not None:
            raise APIError(ErrorCode.INVALID_INVOICE_STATUS)

        if invoice.expires_at < timezone.now():
            raise APIError(ErrorCode.INVOICE_EXPIRED)

        if validated_data["crypto"] not in invoice.methods:
            raise APIError(ErrorCode.INVALID_CRYPTO)

        if validated_data["chain"] not in invoice.methods[validated_data["crypto"]]:
            raise APIError(ErrorCode.INVALID_CHAIN)

        try:
            crypto = CryptoService.get_by_symbol(validated_data["crypto"])
            chain = ChainService.get_by_code(validated_data["chain"])
        except ObjectDoesNotExist as exc:
            raise APIError(ErrorCode.PARAMETER_ERROR) from exc

        try:
            invoice.select_method(crypto, chain)
        except InvoiceStatusError as exc:
            # 锁内状态复查失败：预检之后账单已绑定链上付款/完成/过期，
            # 拒绝切换，避免覆盖已收款账单的支付指引。
            logger.warning("select_method rejected by status recheck", detail=str(exc))
            raise APIError(ErrorCode.INVALID_INVOICE_STATUS) from exc
        except Invoice.InvoiceAllocationError as exc:
            # 不透传异常内部信息给 API 调用方，避免泄露 project/crypto/chain 等内部标识。
            logger.warning("select_method allocation failed", detail=str(exc))
            raise APIError(ErrorCode.NO_RECIPIENT_ADDRESS) from exc
        except (PriceUnavailableError, KeyError, ValueError) as exc:
            # 价格源缺该币/该法币的报价（PriceUnavailableError）或精度溢出（ValueError）
            # 导致法币换算失败，属于系统配置问题而非调用方参数错误，记录 error 级别日志。
            # PriceUnavailableError 必须显式列出：它直接继承 Exception，不是 KeyError
            # 的子类，漏掉会让买家在切换支付方式时收到 500 而不是可识别的错误码。
            logger.exception(
                "select_method price conversion failed",
                invoice=invoice.sys_no,
                crypto=validated_data["crypto"],
                exc=str(exc),
            )
            raise APIError(
                ErrorCode.PARAMETER_ERROR, detail="price unavailable"
            ) from exc

        cache.delete(invoice_public_cache_key(invoice.sys_no))

        return Response(
            InvoicePublicSerializer(
                invoice,
                context={
                    "request": request,
                },
            ).data,
            status=status.HTTP_200_OK,
        )
