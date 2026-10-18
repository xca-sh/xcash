"""SaaS 商户中心列表页（账单收款、用户充值）共用的查询条件。

商户中心的列表只做「按关键字定位一笔」+「按维度缩小范围」两件事：
- keyword：一个输入框同时匹配编号类（精确/包含）与链上标识（地址、哈希，忽略大小写精确匹配）；
- chain / crypto：按公链代码、币种代码筛选；
- created_after / created_before：创建时间半开区间 [after, before)，由前端按商户本地时区
  换算好整日边界后传 ISO8601，避免服务端猜时区。

非法参数一律 400，不静默忽略，防止商户误以为筛选已生效。
"""

from datetime import UTC

from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from common.error_codes import ErrorCode
from common.exceptions import APIError

# 关键字过长没有业务意义（最长的是 66 位 EVM 哈希），截断防止拼出超长 LIKE。
KEYWORD_MAX_LENGTH = 128


def keyword_param(params) -> str:
    return params.get("q", "").strip()[:KEYWORD_MAX_LENGTH]


def parse_datetime_param(params, name: str):
    value = params.get(name, "").strip()
    if not value:
        return None
    dt = parse_datetime(value)
    if dt is None:
        raise APIError(ErrorCode.PARAMETER_ERROR, f"{name} 不是合法的 ISO8601 时间")
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, UTC)
    return dt


def filter_created_range(queryset, params, field: str = "created_at"):
    after = parse_datetime_param(params, "created_after")
    before = parse_datetime_param(params, "created_before")
    if after and before and after >= before:
        raise APIError(
            ErrorCode.PARAMETER_ERROR, "created_after 必须早于 created_before"
        )
    if after:
        queryset = queryset.filter(**{f"{field}__gte": after})
    if before:
        queryset = queryset.filter(**{f"{field}__lt": before})
    return queryset


def filter_chain_crypto(queryset, params, *, chain_field: str, crypto_field: str):
    chain = params.get("chain", "").strip()
    crypto = params.get("crypto", "").strip()
    if chain:
        queryset = queryset.filter(**{f"{chain_field}__code": chain})
    if crypto:
        queryset = queryset.filter(**{f"{crypto_field}__symbol__iexact": crypto})
    return queryset


def invoice_keyword_condition(keyword: str) -> Q:
    """账单关键字：系统编号 / 商户单号 / 标题 包含匹配，收款地址 / 交易哈希 精确匹配。"""
    return (
        Q(sys_no__icontains=keyword)
        | Q(out_no__icontains=keyword)
        | Q(title__icontains=keyword)
        | Q(pay_address__iexact=keyword)
        | Q(transfer__hash__iexact=keyword)
    )


def filter_invoices(queryset, params):
    """账单列表与状态计数共用的筛选（不含状态），保证页签数字与筛选结果一致。"""
    keyword = keyword_param(params)
    if keyword:
        queryset = queryset.filter(invoice_keyword_condition(keyword))
    queryset = filter_chain_crypto(
        queryset, params, chain_field="chain", crypto_field="crypto"
    )
    return filter_created_range(queryset, params)


def deposit_keyword_condition(keyword: str) -> Q:
    """充值关键字：系统编号 / 客户 UID 包含匹配，付款地址 / 交易哈希 精确匹配。"""
    return (
        Q(sys_no__icontains=keyword)
        | Q(customer__uid__icontains=keyword)
        | Q(transfer__from_address__iexact=keyword)
        | Q(transfer__hash__iexact=keyword)
    )
