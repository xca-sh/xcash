from __future__ import annotations

import random
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from aml.models import RiskLevel


@dataclass(frozen=True)
class MistTrackAmlResult:
    risk_level: str
    risk_score: Decimal | None
    raw_response: dict[str, Any]


_DEFAULT_TIMEOUT = 5.0
_MAX_ATTEMPTS = 3
_BASE_BACKOFF = 0.5


def _scrub_url(url: str) -> str:
    # QuickNode endpoint 形如 https://<subdomain>.quiknode.pro/<token>/，token 即 secret。
    # 仅保留 scheme+host，去掉 path/query 避免泄露 token。
    parsed = httpx.URL(url)
    return f"{parsed.scheme}://{parsed.host}"


def _request_with_retry(
    method: str,
    url: str,
    *,
    request_kwargs: dict[str, Any],
    error_label: str,
) -> httpx.Response:
    """统一的带指数退避重试的 HTTP 调用。
    重试策略：仅对网络错误、429 与 5xx 重试，其余 4xx 直接抛出。最多 3 次，退避 0.5s/1s/2s + jitter。
    """
    last_exc: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = httpx.request(method, url, **request_kwargs)
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt == _MAX_ATTEMPTS - 1:
                break
        else:
            status = response.status_code
            if status < 500:
                if status == 429:
                    # 429 限流与 5xx 一样按指数退避重试，不走 4xx 直接失败路径
                    last_exc = RuntimeError(f"{error_label} HTTP {status}")
                    if attempt == _MAX_ATTEMPTS - 1:
                        break
                elif status >= 400:
                    body = response.text[:200]
                    raise RuntimeError(f"{error_label} HTTP {status}: {body}")
                else:
                    return response
            else:
                last_exc = RuntimeError(f"{error_label} HTTP {status}")
                if attempt == _MAX_ATTEMPTS - 1:
                    break

        backoff = _BASE_BACKOFF * (2**attempt) + random.uniform(0, 0.25)  # noqa: S311
        time.sleep(backoff)

    raise RuntimeError(f"{error_label} request failed: {last_exc}")


class QuicknodeMistTrackClient:
    """QuickNode Marketplace 的 MistTrack Address Risk Scores add-on。

    协议见 https://docs.misttrack.io/openapi/quicknode-add-on-docs ：
    JSON-RPC 方法 mt_addressRiskScore，params 为位置参数 [blockchain, address]，
    result 含 score / risk_level / detail_list / hacking_event。
    """

    def __init__(self, *, endpoint_url: str):
        self.endpoint_url = endpoint_url

    def address_risk_score(self, *, chain: str, address: str) -> MistTrackAmlResult:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "mt_addressRiskScore",
            "params": [chain, address],
        }
        response = _request_with_retry(
            "POST",
            self.endpoint_url,
            request_kwargs={"json": payload, "timeout": _DEFAULT_TIMEOUT},
            # 不在 error_label 中泄露 endpoint，外层日志按 host 摘要
            error_label=f"QuickNode MistTrack({_scrub_url(self.endpoint_url)})",
        )

        data = response.json()
        if not isinstance(data, dict):
            raise TypeError("QuickNode MistTrack returned non-object response")
        error = data.get("error")
        if error:
            if isinstance(error, dict):
                message = error.get("message") or str(error)
            else:
                message = str(error)
            raise RuntimeError(message)

        result = data.get("result")
        if not isinstance(result, dict):
            raise TypeError("QuickNode MistTrack response missing result")

        risk_level = result.get("risk_level")
        if risk_level not in RiskLevel.values:
            raise RuntimeError(f"unknown MistTrack risk level: {risk_level}")

        score = result.get("score")
        return MistTrackAmlResult(
            risk_level=str(risk_level),
            risk_score=Decimal(str(score)) if score is not None else None,
            raw_response=result,
        )
