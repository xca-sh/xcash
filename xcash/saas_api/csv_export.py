"""SaaS 商户中心列表（账单收款、用户充值）的 CSV 导出。

导出与列表共用同一套筛选（list_filters），只是不分页：
- 行数上限 EXPORT_MAX_ROWS，超出直接 400 让商户缩小时间范围，避免一次拉爆内存 / 连接；
- StreamingHttpResponse 逐行输出，开头写 UTF-8 BOM，Excel 直接打开不乱码；
- 文本列做 CSV 公式注入转义：商户单号、标题、客户 UID 等都可能由终端用户填写。
"""

import csv

from django.http import StreamingHttpResponse
from django.utils import timezone

from common.error_codes import ErrorCode
from common.exceptions import APIError

EXPORT_MAX_ROWS = 50_000
EXPORT_CHUNK_SIZE = 2000
# Excel 会把这些字符开头的单元格当公式执行（CSV 注入），文本列统一加 ' 前缀转义
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class _Echo:
    """csv.writer 需要一个 file-like；直接返回写入内容，交给 StreamingHttpResponse 逐行输出。"""

    def write(self, value):
        return value


def escape_formula(value) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def iso(value) -> str:
    return value.isoformat() if value else ""


def ensure_exportable(queryset) -> None:
    if queryset.count() > EXPORT_MAX_ROWS:
        raise APIError(
            ErrorCode.PARAMETER_ERROR,
            f"导出记录超过 {EXPORT_MAX_ROWS} 条，请缩小时间范围",
        )


def csv_response(queryset, *, columns, to_row, filename_prefix: str):
    """先校验行数上限，再按 queryset 顺序流式输出 CSV。"""
    ensure_exportable(queryset)
    writer = csv.writer(_Echo())

    def rows():
        yield "﻿"
        yield writer.writerow(columns)
        for obj in queryset.iterator(chunk_size=EXPORT_CHUNK_SIZE):
            yield writer.writerow(to_row(obj))

    filename = f"{filename_prefix}-{timezone.now():%Y%m%d}.csv"
    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
