from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

import httpx
from django.conf import settings

if TYPE_CHECKING:
    from chains.models import Address
    from chains.models import Chain
    from chains.models import Wallet


@dataclass(frozen=True)
class EvmSignedPayload:
    tx_hash: str
    raw_transaction: str


@dataclass(frozen=True)
class SignerAdminSummary:
    health: dict
    wallets: dict
    requests_last_hour: dict
    recent_anomalies: list[dict]


class SignerServiceError(RuntimeError):
    """统一描述 signer 服务调用失败，避免上层直接暴露 httpx/JSON 细节。"""


def _format_signer_http_error(*, path: str, exc: Exception) -> str:
    """保留 signer 返回的错误原因，但不记录请求体或交易载荷。"""
    message = f"远端 signer 请求失败: {path}"
    if not isinstance(exc, httpx.HTTPStatusError):
        return message

    response = exc.response
    parts = [f"HTTP {response.status_code}"]
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = payload.get("code")
        detail = payload.get("detail")
        if code:
            parts.append(f"code={code}")
        if detail:
            parts.append(f"detail={detail}")
    return f"{message} ({', '.join(parts)})"


def build_signer_signature_payload(
    *,
    method: str,
    path: str,
    request_id: str,
    request_body: bytes,
) -> bytes:
    """主应用与 signer 必须使用同一套签名材料，避免协议边界漂移。"""

    return b"\n".join(
        [
            method.upper().encode("utf-8"),
            path.encode("utf-8"),
            request_id.encode("utf-8"),
            request_body,
        ]
    )


def _normalize_hex(value: str) -> str:
    normalized = value if value.startswith("0x") else f"0x{value}"
    return normalized.lower()


class SignerBackend:
    def create_wallet(self, *, wallet_id: int) -> int:
        raise NotImplementedError

    def fetch_admin_summary(self) -> SignerAdminSummary:
        raise NotImplementedError

    def derive_address(
        self,
        *,
        wallet: Wallet,
        chain_type: str,
        bip44_account: int,
        address_index: int,
    ) -> str:
        raise NotImplementedError

    def sign_evm_transaction(
        self,
        *,
        address: Address,
        chain: Chain,
        tx_dict: dict,
    ) -> EvmSignedPayload:
        raise NotImplementedError

class RemoteSignerBackend(SignerBackend):
    def __init__(self, *, base_url: str, timeout: float, shared_secret: str):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.shared_secret = shared_secret

    def _build_headers(
        self,
        *,
        method: str,
        path: str,
        request_content: bytes,
        request_id: str,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Signer-Request-Id": request_id,
        }
        if self.shared_secret:
            # 主应用与 signer 的最小互信先用 HMAC 建立，后续可以再升级为 mTLS。
            headers["X-Signer-Signature"] = hmac.new(
                self.shared_secret.encode("utf-8"),
                build_signer_signature_payload(
                    method=method,
                    path=path,
                    request_id=request_id,
                    request_body=request_content,
                ),
                hashlib.sha256,
            ).hexdigest()
        return headers

    def _post_json(self, *, path: str, request_body: dict) -> dict:
        if not self.base_url:
            raise SignerServiceError("SIGNER_BASE_URL 未配置，无法使用远端 signer")

        if "request_id" not in request_body:
            request_body["request_id"] = str(uuid4())
        request_id = str(request_body["request_id"])
        request_content = json.dumps(
            request_body,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        headers = self._build_headers(
            method="POST",
            path=path,
            request_content=request_content,
            request_id=request_id,
        )
        try:
            response = httpx.post(
                f"{self.base_url}{path}",
                content=request_content,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignerServiceError(
                _format_signer_http_error(path=path, exc=exc)
            ) from exc

    def _get_json(self, *, path: str) -> dict:
        if not self.base_url:
            raise SignerServiceError("SIGNER_BASE_URL 未配置，无法使用远端 signer")

        request_id = str(uuid4())
        request_content = b""
        headers = self._build_headers(
            method="GET",
            path=path,
            request_content=request_content,
            request_id=request_id,
        )
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SignerServiceError(
                _format_signer_http_error(path=path, exc=exc)
            ) from exc

    def create_wallet(self, *, wallet_id: int) -> int:
        payload = self._post_json(
            path="/v1/wallets/create",
            request_body={
                "wallet_id": wallet_id,
            },
        )
        response_wallet_id = payload.get("wallet_id")
        if not response_wallet_id:
            raise SignerServiceError("远端 signer 返回缺少 wallet_id")
        response_wallet_id = int(response_wallet_id)
        if response_wallet_id != wallet_id:
            raise SignerServiceError("远端 signer 返回 wallet_id 不匹配")
        return response_wallet_id

    def derive_address(
        self,
        *,
        wallet: Wallet,
        chain_type: str,
        bip44_account: int,
        address_index: int,
    ) -> str:
        payload = self._post_json(
            path="/v1/wallets/derive-address",
            request_body={
                "wallet_id": wallet.pk,
                "chain_type": chain_type,
                "bip44_account": bip44_account,
                "address_index": address_index,
            },
        )
        address = payload.get("address")
        if not address:
            raise SignerServiceError("远端 signer 返回缺少 address")
        return str(address)

    def sign_evm_transaction(
        self,
        *,
        address: Address,
        chain: Chain,
        tx_dict: dict,
    ) -> EvmSignedPayload:
        payload = self._post_json(
            path="/v1/sign/evm",
            request_body={
                "wallet_id": address.wallet_id,
                "chain_type": address.chain_type,
                "bip44_account": address.bip44_account,
                "address_index": address.address_index,
                "tx_dict": tx_dict,
            },
        )
        try:
            tx_hash = payload.get("tx_hash")
            raw_transaction = payload.get("raw_transaction")
        except AttributeError as exc:
            raise SignerServiceError("远端 signer 返回格式无效") from exc
        if not tx_hash or not raw_transaction:
            raise SignerServiceError("远端 signer 返回缺少 tx_hash 或 raw_transaction")
        return EvmSignedPayload(
            tx_hash=_normalize_hex(str(tx_hash)),
            raw_transaction=_normalize_hex(str(raw_transaction)),
        )

    def fetch_admin_summary(self) -> SignerAdminSummary:
        payload = self._get_json(path="/internal/admin-summary")
        try:
            health = payload["health"]
            wallets = payload["wallets"]
            requests_last_hour = payload["requests_last_hour"]
            recent_anomalies = payload["recent_anomalies"]
        except (TypeError, KeyError) as exc:
            raise SignerServiceError("远端 signer 返回缺少管理摘要字段") from exc
        # dashboard 只消费归一化后的摘要结构，避免上层继续解析松散 JSON。
        return SignerAdminSummary(
            health=dict(health),
            wallets=dict(wallets),
            requests_last_hour=dict(requests_last_hour),
            recent_anomalies=list(recent_anomalies),
        )


def get_signer_backend() -> SignerBackend:
    backend = settings.SIGNER_BACKEND.lower()
    if backend == "remote":
        shared_secret = settings.SIGNER_SHARED_SECRET
        if not shared_secret and not settings.DEBUG:
            raise RuntimeError(
                "生产环境必须配置 SIGNER_SHARED_SECRET，"
                "否则主应用与 signer 之间的请求将无 HMAC 鉴权保护"
            )
        return RemoteSignerBackend(
            base_url=settings.SIGNER_BASE_URL,
            timeout=settings.SIGNER_TIMEOUT,
            shared_secret=shared_secret,
        )
    raise RuntimeError("主应用仅支持 remote signer，请检查 SIGNER_BACKEND 配置")
