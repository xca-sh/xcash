import ipaddress
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import timedelta
from urllib.parse import urlsplit

import environ
import httpx
import structlog
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.db.models import Q
from django.utils import timezone

from common import http_client
from common.consts import APPID_HEADER
from common.consts import NONCE_HEADER
from common.consts import SIGNATURE_HEADER
from common.consts import TIMESTAMP_HEADER
from common.crypto import calc_hmac
from common.decorators import singleton_task
from core.runtime_settings import get_webhook_delivery_max_backoff_seconds
from core.runtime_settings import get_webhook_delivery_max_retries
from core.runtime_settings import get_webhook_event_timeout
from webhooks.models import DeliveryAttempt
from webhooks.models import WebhookEvent

logger = structlog.get_logger()

EVENT_ATTEMPT_TIMEOUT = 10
DELIVERY_CLAIM_TIMEOUT = EVENT_ATTEMPT_TIMEOUT + 5
# DNS 解析预算。要显著小于 soft_time_limit(8s)，留出建连、收包与落库的时间。
DNS_RESOLVE_TIMEOUT = 3
# 商户侧配置缺失（未开通知/未填地址）时的挂起时长。这类状态是可恢复的，
# 不能一次就判终局，但也不该高频空转，因此退避到分钟级等待配置补齐。
CONFIG_MISSING_RETRY_DELAY = timedelta(minutes=10)
# 语义上等于「稍后再试」的 4xx：请求超时、限流、要求先决条件。
RETRYABLE_STATUS = frozenset({408, 425, 429})
# 超龄事件的终结阈值，取事件超时窗口的倍数：要显著大于「重试上限 × 最大退避」，
# 否则会误杀仍在正常退避重试途中的事件。
STALLED_EVENT_TIMEOUT_FACTOR = 8
# 商户回执通常只有 "ok"/"success" 等几字节。设置 64KB 上限既能兼容偶发的 HTML
# 错误页等场景，又能挡掉恶意商户回包放大 celery worker 内存。
MAX_RESPONSE_BYTES = 64 * 1024

# 出口代理配置（可选）：设置后 webhook 请求通过代理转发，隐藏服务器真实 IP
# XCASH_EGRESS_PROXY      — 代理转发地址（不设则直连商户 webhook URL）
# XCASH_EGRESS_PROXY_KEY  — 代理鉴权密钥
_egress_proxy_url: str | None = environ.Env().str("XCASH_EGRESS_PROXY", default=None)
_egress_proxy_key: str = environ.Env().str("XCASH_EGRESS_PROXY_KEY", default="")


class DeliveryTargetResolutionError(Exception):
    """DNS 暂时拿不到解析结果（超时、解析器故障、记录传播中）。

    必须与「判定为不安全」区分开：解析失败是瞬时状态，商户域名偶发解析慢一次
    就永久终结事件会丢掉支付成功通知；只有解析成功且结果里存在非公网地址，
    才是确定性的不安全目标，允许终局。
    """


def resolve_addresses(hostname: str, port: int) -> list[str]:
    """带超时的 DNS 解析；拿不到任何结果时抛 DeliveryTargetResolutionError。

    socket.getaddrinfo 本身没有超时参数，遇到权威 NS 黑洞（域名过期、NS 被丢包）
    时会在 C 层阻塞远超任务的 soft_time_limit，把 worker 卡满一整批。这里用线程
    包一层实现可控超时。

    不能写成 `with ThreadPoolExecutor(...)`：with 退出时隐式 shutdown(wait=True)
    会 join 仍阻塞在 getaddrinfo 里的工作线程，超时形同虚设。必须 wait=False 立即
    返回，超时的解析线程泄漏到 getaddrinfo 自身返回为止（libc 解析器有自己的
    重试上限，最终一定会结束）。
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(
            socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM
        )
        try:
            infos = future.result(timeout=DNS_RESOLVE_TIMEOUT)
        except (FuturesTimeoutError, OSError) as exc:
            raise DeliveryTargetResolutionError(
                f"DNS resolution failed for {hostname}: {exc}"
            ) from exc
    finally:
        pool.shutdown(wait=False)
    addresses = [item[4][0] for item in infos]
    if not addresses:
        raise DeliveryTargetResolutionError(f"empty DNS answer for {hostname}")
    return addresses


def safe_delivery_target(url: str) -> str | None:
    """校验投递目标，并返回一个可直连的、已通过校验的 IP。

    返回 None 表示目标确定不安全（非 https、localhost、解析出非公网地址）。
    返回 "" 表示无需固定 IP（内网放行模式）。DNS 暂时解析不出结果时抛
    DeliveryTargetResolutionError，由调用方按可重试语义处理，不得当作不安全终局。

    必须把校验用的 IP 交给调用方直连：如果只回 bool、让 httpx 再按主机名解析一次，
    两次 DNS 之间就存在 rebinding 窗口——商户把域名的权威 NS 配成交替返回公网 IP
    与 169.254.169.254，校验轮拿到公网 IP 放行、连接轮拿到元数据地址，响应还会被
    存进 DeliveryAttempt.response_body 经 SaaS 接口读回去。
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None

    # 仅放过 hostname 解析；scheme / 私有网段 / localhost 校验整体跳过，
    # 用于本地压测等"主动信任内网目标"的开发场景。生产保持默认 False。
    if getattr(settings, "WEBHOOK_ALLOW_INTERNAL_TARGETS", False):
        return "" if parsed.hostname else None

    if parsed.scheme != "https" or not parsed.hostname:
        return None

    hostname = parsed.hostname.strip().lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return None

    try:
        literals = [ipaddress.ip_address(hostname)]
    except ValueError:
        resolved = resolve_addresses(hostname, parsed.port or 443)
        try:
            literals = [ipaddress.ip_address(item) for item in resolved]
        except ValueError:
            return None

    if not literals or not all(ip.is_global for ip in literals):
        return None
    # 全部解析结果均为公网地址，取第一个作为本次投递的固定连接目标。
    return str(literals[0])


def next_backoff(try_number: int) -> int:
    # Webhook 重试节奏允许通过系统参数中心调节，但仍保持指数退避，避免失败时瞬时洪泛商户端。
    return min(2 ** (try_number + 1), get_webhook_delivery_max_backoff_seconds())


def _claim_event_for_delivery(event_pk) -> bool:
    """认领事件并原子自增尝试计数。

    计数必须在这里加，而不是等投递结束后按 DeliveryAttempt 行数推算：任务从认领
    到写 attempt 之间随时可能被 hard time_limit 杀掉（DNS 卡住、worker 重启、
    OOM），那时事件仍是 PENDING、attempts 仍为空，投递锁一过期就被重新调度，
    重试次数永远停在 1，max_retries 形同虚设——事件变成永不终结的黑洞，还因
    created_at 最老而长期霸占调度批次的头部，饿死其他商户的重试。
    """
    now = timezone.now()
    claimed = (
        WebhookEvent.objects.filter(
            pk=event_pk,
            status=WebhookEvent.Status.PENDING,
        )
        .filter(
            Q(schedule_locked_until__isnull=True) | Q(schedule_locked_until__lte=now)
        )
        .filter(
            Q(delivery_locked_until__isnull=True) | Q(delivery_locked_until__lte=now)
        )
        .update(
            delivery_locked_until=now + timedelta(seconds=DELIVERY_CLAIM_TIMEOUT),
            attempt_count=F("attempt_count") + 1,
        )
    )
    return claimed == 1


def suspend_or_fail(
    event_pk,
    *,
    try_number: int,
    reason: str,
    delay: timedelta,
) -> None:
    """把事件挂起等待外部条件恢复；已用尽重试预算则判终局。

    用于「商户配置暂时缺失」这类可恢复原因：既不能一次就 FAILED（配置几秒后就
    可能补齐，而丢掉的是支付成功通知），也不能无限挂着（否则事件永远堆在待投递
    队列里）。重试预算仍由 attempt_count 统一约束。
    """
    if try_number >= get_webhook_delivery_max_retries():
        WebhookEvent.objects.filter(pk=event_pk).update(
            status=WebhookEvent.Status.FAILED,
            last_error=reason,
            schedule_locked_until=None,
            delivery_locked_until=None,
        )
        return

    WebhookEvent.objects.filter(pk=event_pk).update(
        schedule_locked_until=timezone.now() + delay,
        last_error=reason,
        delivery_locked_until=None,
    )


def _build_delivery_headers(project, event, body_str: str, timestamp: str) -> dict:
    """组装 Webhook 请求头，包含 HMAC 签名信息。"""
    nonce = event.nonce
    return {
        "Content-Type": "application/json",
        APPID_HEADER: project.appid,
        NONCE_HEADER: nonce,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: calc_hmac(
            message=f"{nonce}{timestamp}{body_str}",
            key=project.hmac_key,
        ),
    }


def pin_request_to_ip(url: str, pinned_ip: str) -> tuple[str, dict]:
    """把请求目标固定到已校验的 IP，返回 (改写后的 URL, 额外 httpx extensions)。

    直连 IP 可以彻底关掉「校验一次 DNS、连接又解析一次」的 rebinding 窗口。
    TLS 侧通过 sni_hostname 仍以原主机名做 SNI 与证书校验，因此安全性不降级。
    """
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname or hostname == pinned_ip:
        return url, {}

    # IPv6 字面量在 URL 里必须带方括号。
    literal = ipaddress.ip_address(pinned_ip)
    host_part = f"[{pinned_ip}]" if literal.version == 6 else pinned_ip
    netloc = f"{host_part}:{parsed.port}" if parsed.port else host_part
    pinned_url = parsed._replace(netloc=netloc).geturl()
    return pinned_url, {"sni_hostname": hostname}


def _execute_http_delivery(
    *,
    request_url: str,
    method: str = "POST",
    headers: dict,
    body_str: str = "",
    params: dict | None = None,
    expected_response_body: str = "ok",
    pinned_ip: str = "",
) -> tuple[bool, int | None, dict | None, str, str, int]:
    """
    向目标地址发送 Webhook 请求，返回
    (ok, status_code, resp_headers, resp_text, err_text, duration_ms)。
    不抛异常，所有错误均通过返回值传递。
    """
    ok = False
    status_code = None
    resp_headers = None
    resp_text = ""
    err_text = ""

    extensions: dict = {}
    if pinned_ip:
        parsed = urlsplit(request_url)
        request_url, extensions = pin_request_to_ip(request_url, pinned_ip)
        # 连接目标换成 IP 后，仍要带上原 Host 头，否则虚拟主机路由不到正确站点。
        if parsed.netloc:
            headers = {**headers, "Host": parsed.netloc}

    start = time.perf_counter()
    try:
        # 不能用 common.http_client 的共享连接池：连接固定到已校验 IP，池按 IP 复用连接，
        # 同 IP 的两个商户会复用对方的 TLS 连接、绕过证书校验。只复用 SSLContext，省掉
        # 每次投递现场加载 CA 包的开销。
        with httpx.Client(timeout=5, verify=http_client.ssl_context()) as client:
            with client.stream(
                method,
                request_url,
                headers=headers,
                params=params,
                content=body_str if method != "GET" else None,
                extensions=extensions,
            ) as resp:
                status_code = resp.status_code
                resp_headers = dict(resp.headers)
                # 流式读取并在累计达到 MAX_RESPONSE_BYTES 时截断，避免恶意/异常
                # 商户回执（如 100MB HTML 错误页）撑爆 worker 内存。
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    if len(buf) + len(chunk) > MAX_RESPONSE_BYTES:
                        buf.extend(chunk[: MAX_RESPONSE_BYTES - len(buf)])
                        break
                    buf.extend(chunk)
                # utf-8-sig 去掉开头的 BOM：str.strip() 不会去 BOM，带 BOM 的 PHP 文件
                # 输出的 "success" 前面带 BOM，会被误判为响应体不符。
                resp_text = bytes(buf).decode("utf-8-sig", errors="replace")
            # 商户的 PHP/Java 框架 echo "success" 时常带回车 / BOM / 前后空白，
            # 严格相等会把这些合法响应判为失败、触发重试；strip 后精确匹配
            # 兼顾兼容性与匹配严格度（仍区分大小写、不允许中间夹杂内容）。
            ok = status_code == 200 and resp_text.strip() == expected_response_body
    except httpx.RequestError as e:
        err_text = f"{e.__class__.__name__}: {e}"
    except Exception as e:
        err_text = f"UnexpectedError: {type(e).__name__}"
    duration_ms = int((time.perf_counter() - start) * 1000)

    return ok, status_code, resp_headers, resp_text, err_text, duration_ms


@shared_task(ignore_result=True)
@singleton_task(timeout=60, use_params=False)
def reap_stalled_events(batch_size=512):
    """把超龄仍未送达的事件强制终结。

    attempt_count 已经堵住了已知的"零 attempt 无限重试"路径，但任何新的中断点
    （投递前新增的逻辑抛异常、认领后进程被杀且计数因故未落库）都可能让事件长期
    滞留 PENDING。这类事件 created_at 最老，会持续占据调度批次头部把其他商户的
    重试挤出去，因此需要一道与具体失败原因无关的兜底。

    终结判据取事件超时窗口的若干倍，远大于正常重试链路的总耗时，不会误伤在途重试。
    """
    deadline = (
        timezone.now() - get_webhook_event_timeout() * STALLED_EVENT_TIMEOUT_FACTOR
    )
    stalled_pks = list(
        WebhookEvent.objects.filter(
            status=WebhookEvent.Status.PENDING,
            created_at__lte=deadline,
        )
        .order_by("created_at")
        .values_list("pk", flat=True)[:batch_size]
    )
    if not stalled_pks:
        return

    reaped = WebhookEvent.objects.filter(pk__in=stalled_pks).update(
        status=WebhookEvent.Status.FAILED,
        last_error="Delivery stalled beyond timeout; reaped by janitor.",
        schedule_locked_until=None,
        delivery_locked_until=None,
    )
    logger.warning(
        "webhook_stalled_events_reaped",
        count=reaped,
        deadline=deadline.isoformat(),
    )


@shared_task
@singleton_task(timeout=15, use_params=False)
def schedule_events(batch_size=128):
    qs = (
        WebhookEvent.objects.filter(status=WebhookEvent.Status.PENDING)
        .filter(
            Q(schedule_locked_until__isnull=True)
            | Q(schedule_locked_until__lte=timezone.now())
        )
        .filter(
            Q(delivery_locked_until__isnull=True)
            | Q(delivery_locked_until__lte=timezone.now())
        )
        .order_by("created_at")[:batch_size]
    )

    for ev in qs:
        deliver_event.delay(ev.pk)


@shared_task(
    acks_late=True,
    max_retries=0,
    soft_time_limit=8,  # httpx timeout=5s，额外留 3s 给 DB 写入，避免 SoftTimeLimitExceeded 打断事务
    time_limit=EVENT_ATTEMPT_TIMEOUT,
)
@singleton_task(timeout=EVENT_ATTEMPT_TIMEOUT + 2, use_params=True)
def deliver_event(event_pk):
    if not _claim_event_for_delivery(event_pk):
        return

    event = WebhookEvent.objects.select_related("project").get(pk=event_pk)

    project = event.project
    target_url = event.delivery_url or project.webhook
    # 认领时已原子自增，这里读到的就是本次的尝试序号。
    try_number = event.attempt_count

    # 商户通知开关关闭 / 投递地址未配置：这是可恢复的配置态，不能判终局。
    # 商户在后台改回调地址（清空保存再填新值）只需几秒，若一次就打成 FAILED，
    # 期间确认的所有账单与充值通知都会永久丢失，只能靠人工逐条重投。
    if not project.webhook_open or not target_url:
        reason = (
            "Endpoint not open."
            if not project.webhook_open
            else "Webhook URL not configured."
        )
        suspend_or_fail(
            event_pk,
            try_number=try_number,
            reason=reason,
            delay=CONFIG_MISSING_RETRY_DELAY,
        )
        return

    try:
        pinned_ip = safe_delivery_target(target_url)
    except DeliveryTargetResolutionError as exc:
        # DNS 瞬时失败（解析器抖动、NS 短暂不可达）与「目标不安全」是两回事：
        # 商户域名偶发解析慢一次就终局会永久丢掉支付成功通知。按正常退避挂起，
        # 重试预算仍由 attempt_count 统一约束，持续解析不出来最终照样 FAILED。
        suspend_or_fail(
            event_pk,
            try_number=try_number,
            reason=str(exc),
            delay=timedelta(seconds=next_backoff(try_number)),
        )
        return
    if pinned_ip is None:
        WebhookEvent.objects.filter(pk=event_pk).update(
            status=WebhookEvent.Status.FAILED,
            last_error="Unsafe webhook URL.",
            delivery_locked_until=None,
        )
        return

    body_str = json.dumps(event.payload)
    timestamp = str(int(timezone.now().timestamp()))

    if event.delivery_method == WebhookEvent.DeliveryMethod.GET_QUERY:
        # GET 请求由商户端用自有签名（如 EPay MD5）校验 query string，不附带 HMAC 头
        headers = {}
        http_method = "GET"
        query_params = event.payload
        # GET 实际不发送 body，attempt 记录留空避免误导
        body_str_for_attempt = ""
    else:
        headers = _build_delivery_headers(project, event, body_str, timestamp)
        http_method = "POST"
        query_params = None
        body_str_for_attempt = body_str

    # 出口代理模式：所有 delivery_method 一律走代理。商户配置的 notify_url 由 xcash worker
    # 直连会暴露真实 IP，且无法防御内网/元数据端点的 SSRF 攻击。代理地址未配置时退回直连。
    if _egress_proxy_url:
        request_url = _egress_proxy_url
        headers["CF-Worker-Destination"] = target_url
        headers["CF-Worker-Key"] = _egress_proxy_key
        # 代理地址由运维配置、非商户可控，无需固定 IP。
        connect_ip = ""
    else:
        request_url = target_url
        connect_ip = pinned_ip

    ok, status_code, resp_headers, resp_text, err_text, duration_ms = (
        _execute_http_delivery(
            request_url=request_url,
            method=http_method,
            headers=headers,
            body_str=body_str,
            params=query_params,
            expected_response_body=event.expected_response_body,
            pinned_ip=connect_ip,
        )
    )

    # 记录本次 attempt + 更新事件状态（事务保护）
    # 去掉代理鉴权头，避免写入 attempt 日志泄漏密钥
    headers.pop("CF-Worker-Key", None)
    headers.pop("CF-Worker-Destination", None)
    with transaction.atomic():
        DeliveryAttempt.objects.create(
            event=event,
            try_number=try_number,
            request_headers=headers,
            request_body=body_str_for_attempt,
            response_status=status_code,
            response_headers=resp_headers,
            response_body=resp_text[:1024],
            duration_ms=duration_ms,
            ok=ok,
            error=err_text[:1024],
        )

        if ok:
            # 投递成功只完成当前事件。Project.webhook_open 是商户/管理员开关，
            # 不由单次投递结果自动改写。
            WebhookEvent.objects.filter(pk=event_pk).update(
                status=WebhookEvent.Status.SUCCEEDED,
                last_error="",
                delivered_at=timezone.now(),
                delivery_locked_until=None,
            )
            return

        # 网络错误、5xx 与「稍后再试」语义的 4xx 可重试；其余 2xx(非200)/3xx/4xx
        # 属于商户端明确拒绝，重试无意义。428/429/408 必须算可重试：商户端限流器
        # 或滚动发布期间返回 429 几分钟，一次就判终局会让这期间所有支付成功通知
        # 永久失败，商户不给用户上账，直接产生资金纠纷。
        # 易支付（GET 查询串投递）约定商户回 "success" 才算收到，否则平台继续重发，
        # 插件按此语义做了幂等，因此 200 但响应体不符也可重试。原生 JSON 通知不跟随：
        # 这类商户以前只会收到一次，改成重发可能让未做幂等的商户重复上账。
        epay_body_mismatch = (
            status_code == 200
            and event.delivery_method == WebhookEvent.DeliveryMethod.GET_QUERY
        )
        retryable = (
            status_code is None
            or status_code >= 500
            or status_code in RETRYABLE_STATUS
            or epay_body_mismatch
        ) and try_number < get_webhook_delivery_max_retries()
        error_msg = err_text or f"status={status_code}"
        if retryable:
            WebhookEvent.objects.filter(pk=event_pk).update(
                schedule_locked_until=timezone.now()
                + timedelta(seconds=next_backoff(try_number)),
                last_error=error_msg,
                delivery_locked_until=None,
            )
        else:
            WebhookEvent.objects.filter(pk=event_pk).update(
                status=WebhookEvent.Status.FAILED,
                last_error=error_msg,
                schedule_locked_until=None,
                delivery_locked_until=None,
            )
