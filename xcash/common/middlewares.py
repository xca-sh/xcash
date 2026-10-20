import logging
import os
import re
from dataclasses import dataclass

from django.conf import settings
from django.http import HttpRequest
from django.middleware.locale import LocaleMiddleware
from django.urls import Resolver404
from django.urls import resolve
from django.utils import timezone
from django.utils import translation
from django.utils.translation.trans_real import parse_accept_lang_header
from django_redis import get_redis_connection

logger = logging.getLogger(__name__)

from common.consts import APPID_HEADER
from common.consts import NONCE_HEADER
from common.consts import SIGNATURE_HEADER
from common.consts import TIMESTAMP_HEADER
from common.crypto import verify_hmac
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.utils.security import client_ip
from common.utils.security import is_ip_in_whitelist
from projects.models import Project


class AdminSessionTimeoutMiddleware:
    """根据 SystemSettings 动态设置后台 session 超时时间。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and self.is_admin_request_path(request):
            from core.runtime_settings import get_admin_session_timeout_seconds

            request.session.set_expiry(get_admin_session_timeout_seconds())
        return self.get_response(request)

    def is_admin_request_path(self, request: HttpRequest) -> bool:
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return False

        return match.namespace == "admin" or match.url_name in {
            "operational-inspection",
            "admin-path-redirect",
        }


class XcashLocaleMiddleware(LocaleMiddleware):
    """
    按「手动选择 → 浏览器语言 → 兜底」决定界面语言。

    与 Django 原生 LocaleMiddleware 的唯一区别在兜底：原生实现对「浏览器带了
    Accept-Language 但都不在 LANGUAGES 内」（如德语浏览器）也回退 LANGUAGE_CODE（中文），
    这里改为回退英文，与支付页一致。完全没带 Accept-Language 的请求（商户服务端调 API 的
    常见情形）仍回退 LANGUAGE_CODE，保持 API 错误文案的既有行为。
    项目不使用 i18n_patterns，因此不处理 URL 前缀语言。
    """

    UNSUPPORTED_BROWSER_FALLBACK = "en"

    def process_request(self, request):
        translation.activate(self.resolve_language(request))
        request.LANGUAGE_CODE = translation.get_language()

    def resolve_language(self, request: HttpRequest) -> str:
        # 1. 手动切换过（set_language 写入的 cookie）以手动选择为准
        cookie_language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if cookie_language:
            try:
                return translation.get_supported_language_variant(cookie_language)
            except LookupError:
                pass

        # 2. 没有 Accept-Language：不是浏览器访问，沿用默认语言
        accept_language = request.META.get("HTTP_ACCEPT_LANGUAGE", "")
        if not accept_language:
            return settings.LANGUAGE_CODE

        # 3. 按浏览器偏好顺序取第一个支持的语言（zh-CN → zh-hans、ru-RU → ru 由 Django 映射）
        for language, _quality in parse_accept_lang_header(accept_language):
            try:
                return translation.get_supported_language_variant(language)
            except LookupError:
                continue

        # 4. 浏览器语言都不支持
        return self.UNSUPPORTED_BROWSER_FALLBACK


@dataclass
class XcashRequestContext:
    project: Project | None = None
    timestamp: int = 0
    payload: str | None = None
    nonce: str = ""
    signature: str = ""
    client_ip: str | None = None


class XcashMiddleware:
    CONTEXT_ATTR = "_xcash_ctx"
    # 修复：NONCE_TTL_SECONDS 必须 >= TIMESTAMP_TOLERANCE_SECONDS
    # 否则 nonce 在时间窗口内过期后可被重放
    NONCE_TTL_SECONDS = 300
    TIMESTAMP_TOLERANCE_SECONDS = 300  # 5 分钟，兼顾网络延迟与安全

    def __init__(self, get_response):
        self.get_response = get_response
        self._nonce_connection = None

    def _get_context(self, request: HttpRequest) -> XcashRequestContext:
        ctx: XcashRequestContext | None = getattr(request, self.CONTEXT_ATTR, None)
        if ctx is None:
            ctx = XcashRequestContext(
                nonce=request.headers.get(NONCE_HEADER, ""),
                timestamp=self._parse_timestamp(request),
                signature=request.headers.get(SIGNATURE_HEADER, ""),
                payload=self._decode_payload(request),
                client_ip=self._client_ip(request),
                project=Project.retrieve(request.headers.get(APPID_HEADER, "")),
            )
            setattr(request, self.CONTEXT_ATTR, ctx)
        return ctx

    def project(self, request: HttpRequest) -> Project | None:
        return self._get_context(request).project

    def client_ip(self, request: HttpRequest) -> str:
        return self._get_context(request).client_ip

    def timestamp(self, request: HttpRequest) -> int:
        return self._get_context(request).timestamp

    def message(self, request: HttpRequest) -> str:
        ctx = self._get_context(request)
        return ctx.nonce + str(ctx.timestamp) + ctx.payload

    def signature(self, request: HttpRequest) -> str:
        return self._get_context(request).signature

    @staticmethod
    def _is_api_request(request: HttpRequest) -> bool:
        return request.path.startswith("/v1/")

    @staticmethod
    def _is_no_signature_request(request: HttpRequest) -> bool:
        post_patterns = [
            re.compile(r"^/v1/invoice/[^/]+/select-method$"),
        ]
        # 公开只读端点：买家支付页直接访问，无商户 appid / 签名。
        # /v1/metadata 是链/币基础字典，与 invoice 公开详情同属此类，必须豁免，
        # 否则会被当作商户 API 拦成 INVALID_APPID。
        get_patterns = [
            re.compile(r"^/v1/invoice/[^/]+$"),
            re.compile(r"^/v1/metadata$"),
        ]

        if request.method == "POST":
            return any(pattern.match(request.path) for pattern in post_patterns)
        if request.method == "GET":
            return any(pattern.match(request.path) for pattern in get_patterns)
        return False

    def _requires_project(self, request: HttpRequest) -> bool:
        return self._is_api_request(request) and not self._is_no_signature_request(
            request
        )

    @staticmethod
    def _parse_timestamp(request: HttpRequest) -> int:
        timestamp = request.headers.get(TIMESTAMP_HEADER, "0")
        if isinstance(timestamp, str) and timestamp.isdigit():
            return int(timestamp)
        return 0

    @staticmethod
    def _decode_payload(request: HttpRequest) -> str:
        return request.body.decode("utf-8")

    @staticmethod
    def _client_ip(request: HttpRequest) -> str | None:
        # 与限流层共用同一份可信代理判定，避免两套取 IP 口径产生绕过缝隙。
        return client_ip(request)

    def _nonce_cache(self):
        if self._nonce_connection is None:
            self._nonce_connection = get_redis_connection("default")
        return self._nonce_connection

    @staticmethod
    def _nonce_cache_key(project: Project, nonce: str) -> str:
        return f"xcash:nonce:{project.appid}:{nonce}"

    def __call__(self, request: HttpRequest):
        """商户 /v1 API 的统一准入，依次校验：项目 → IP 白名单 → HMAC 签名与 nonce。

        任一项不通过即返回对应错误码；顺序与原先三个独立中间件的叠放顺序一致。
        """
        if not self._requires_project(request):
            return self.get_response(request)

        rejection = (
            self.check_project(request)
            or self.check_ip_whitelist(request)
            or self.check_signature(request)
        )
        if rejection is not None:
            return rejection.to_response()
        return self.get_response(request)

    def check_project(self, request: HttpRequest) -> APIError | None:
        project = self.project(request)
        if not project:
            return APIError(ErrorCode.INVALID_APPID)

        # 停用的项目必须在此拒绝。active 此前只有 SaaS 的 activate/deactivate 会写、
        # 没有任何读取方：运维或 SaaS 停用商户后，对方仍能照常建账单、申请充币地址、
        # 占用 VaultSlot 并消耗归集 gas，后台却显示「已停用」。
        # 注意它与 SaaS 侧的 frozen 是两套独立状态，check_saas_permission 兜不住。
        if not project.active:
            return APIError(ErrorCode.ACCESS_DENY)

        if not settings.DEBUG:
            request_timestamp = self.timestamp(request)
            now_ts = int(timezone.now().timestamp())
            # 容限取 TIMESTAMP_TOLERANCE_SECONDS（300s），与 NONCE_TTL_SECONDS 保持一致，
            # 防止 nonce 在时间窗口内过期后被重放
            if (
                not request_timestamp
                or abs(request_timestamp - now_ts) > self.TIMESTAMP_TOLERANCE_SECONDS
            ):
                return APIError(ErrorCode.EXPIRED)

        ready, errors = project.is_ready
        if not ready:
            return APIError(
                ErrorCode.PROJECT_NOT_READY, detail="; ".join(str(e) for e in errors)
            )
        return None

    def check_ip_whitelist(self, request: HttpRequest) -> APIError | None:
        client_ip = self.client_ip(request)
        if not client_ip or not is_ip_in_whitelist(
            whitelist=self.project(request).ip_white_list,
            ip=client_ip,
        ):
            return APIError(ErrorCode.IP_FORBIDDEN)
        return None

    def check_signature(self, request: HttpRequest) -> APIError | None:
        project = self.project(request)
        if not self._is_valid_hmac(request, project):
            return APIError(ErrorCode.SIGNATURE_ERROR)

        nonce_status = self.register_nonce(request, project)
        if nonce_status is None:
            return APIError(ErrorCode.PARAMETER_ERROR)
        if not nonce_status:
            return APIError(ErrorCode.REPLAY_ATTACK)
        return None

    def _is_valid_hmac(self, request: HttpRequest, project: Project) -> bool:
        # 仅在 DEBUG 模式且服务端显式设置 ALLOW_DEV_BYPASS=1 环境变量时跳过签名校验；
        # 客户端无法控制服务端环境变量，不存在请求头绕过签名的攻击面。
        if settings.DEBUG and os.environ.get("ALLOW_DEV_BYPASS") == "1":
            logger.warning(
                "HMAC 验证已跳过（DEBUG + ALLOW_DEV_BYPASS=1），请勿在生产环境使用"
            )
            return True

        return verify_hmac(
            message=self.message(request),
            key=project.hmac_key,
            signature=self.signature(request),
        )

    def register_nonce(self, request: HttpRequest, project: Project) -> bool | None:
        ctx = self._get_context(request)
        if not ctx.nonce:
            return None

        cache = self._nonce_cache()
        key = self._nonce_cache_key(project, ctx.nonce)

        try:
            # nx=True：仅当 key 不存在时写入并返回 True
            # 若 key 已存在（nonce 已被使用）则返回 None/False → 触发 REPLAY_ATTACK
            # TTL 与时间戳容限一致（NONCE_TTL_SECONDS == TIMESTAMP_TOLERANCE_SECONDS）
            # 保证在有效时间窗口内 nonce 始终存在，防止窗口内重放
            stored = cache.set(
                key,
                1,
                ex=self.NONCE_TTL_SECONDS,
                nx=True,
            )
        except Exception:  # noqa
            # 修复：Redis 不可用时选择拒绝请求（fail-safe），防止通过触发异常绕过防重放
            logger.exception(
                "Nonce 存储失败，为防重放攻击拒绝请求，appid=%s", project.appid
            )
            return False

        return bool(stored)


class ExceptionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except APIError as e:
            return e.to_response()
