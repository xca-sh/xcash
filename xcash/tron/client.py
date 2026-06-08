from __future__ import annotations

import json
import time

import httpx
import structlog
from django.conf import settings

from chains.constants import TRON_MAINNET_BASE_URL

logger = structlog.get_logger()

# HTTP 失败时的退避时长（秒），数组长度即"最多重试次数"。
# 初次失败 → 等 0.2s 重试 → 等 0.8s 重试 → 仍失败上抛，整体上限约 1s 的等待开销。
_TRON_HTTP_RETRY_BACKOFF_SECONDS = (0.2, 0.8)


class TronClientError(RuntimeError):
    """Tron HTTP 客户端异常。"""


class TronHttpClient:
    # 主网地址作为兜底；真实 Chain 走 chain.tron_base_url（按 is_testnet 选 Nile/主网）。
    BASE_URL = TRON_MAINNET_BASE_URL

    def __init__(self, *, chain):
        self.chain = chain
        self.base_url = getattr(chain, "tron_base_url", "") or self.BASE_URL
        self.timeout = settings.TRON_RPC_TIMEOUT

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.chain.tron_api_key:
            headers["TRON-PRO-API-KEY"] = self.chain.tron_api_key
        return headers

    @staticmethod
    def _is_retriable_http_error(exc: Exception) -> bool:
        """4xx 客户端错误（除 408 / 429）属永久性错误，不重试；其余 HTTP 错误视为瞬时。

        409 / 422 等也属客户端责任范畴，重试只会重复触发；429 受限要等节点退避后再发，
        和 5xx / 网络层超时归为可重试。
        """
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return not (400 <= status < 500 and status not in (408, 429))
        return isinstance(exc, httpx.HTTPError)

    def _request_with_retry(
        self,
        *,
        method: str,
        url: str,
        request_label: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> httpx.Response:
        """统一封装 Tron HTTP 调用的指数退避重试 + raise_for_status。

        非瞬时错误（4xx 客户端错误）立即上抛，瞬时错误（5xx / 429 / 网络层超时 / 连接）
        按 _TRON_HTTP_RETRY_BACKOFF_SECONDS 退避重试，最多尝试 3 次。
        """
        last_exc: Exception | None = None
        max_attempts = len(_TRON_HTTP_RETRY_BACKOFF_SECONDS) + 1
        chain_code = getattr(self.chain, "chain", "unknown")
        for attempt in range(max_attempts):
            try:
                # 走 httpx.get/post 而非 httpx.request 分发：测试用 @patch("tron.client.httpx.get/post")
                # 拦截调用，统一入口能稳定保持现有测试 mock 表面。
                if method == "GET":
                    response = httpx.get(
                        url,
                        headers=self._headers(),
                        timeout=self.timeout,
                        params=params,
                    )
                elif method == "POST":
                    response = httpx.post(
                        url,
                        headers=self._headers(),
                        timeout=self.timeout,
                        params=params,
                        json=json_body,
                    )
                else:
                    raise ValueError(f"unsupported HTTP method: {method}")  # noqa: TRY301
            except Exception as exc:  # noqa: BLE001
                if not self._is_retriable_http_error(exc):
                    raise TronClientError(f"{request_label} from {chain_code}") from exc
                last_exc = exc
                if attempt == max_attempts - 1:
                    break
                backoff_seconds = _TRON_HTTP_RETRY_BACKOFF_SECONDS[attempt]
                logger.warning(
                    "Tron HTTP 调用失败，准备重试",
                    chain=chain_code,
                    request=request_label,
                    attempt=attempt + 1,
                    backoff_seconds=backoff_seconds,
                    error=str(exc),
                )
                time.sleep(backoff_seconds)
            else:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if not self._is_retriable_http_error(exc):
                        raise TronClientError(
                            f"{request_label} from {chain_code}"
                        ) from exc
                    last_exc = exc
                    if attempt == max_attempts - 1:
                        break
                    backoff_seconds = _TRON_HTTP_RETRY_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "Tron HTTP 调用失败，准备重试",
                        chain=chain_code,
                        request=request_label,
                        attempt=attempt + 1,
                        backoff_seconds=backoff_seconds,
                        error=str(exc),
                    )
                    time.sleep(backoff_seconds)
                else:
                    return response

        raise TronClientError(f"{request_label} from {chain_code}") from last_exc

    def get_latest_solid_block_number(self) -> int:
        response = self._request_with_retry(
            method="GET",
            url=f"{self.base_url}/walletsolidity/getnowblock",
            request_label="failed to fetch latest solid block",
        )
        payload = response.json()
        try:
            block_number = int(
                ((payload.get("block_header") or {}).get("raw_data") or {}).get(
                    "number",
                    0,
                )
            )
        except (TypeError, ValueError) as exc:
            raise TronClientError(
                f"invalid latest solid block from {self.chain.code}"
            ) from exc
        if block_number <= 0:
            raise TronClientError(f"invalid latest solid block from {self.chain.code}")
        return block_number

    def get_solid_block_id(self, *, block_number: int) -> str:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/walletsolidity/getblockbynum",
            request_label="failed to fetch solid block",
            json_body={"num": block_number},
        )
        payload = response.json()
        block_id = str(payload.get("blockID") or "").strip().lower()
        if len(block_id) != 64:
            raise TronClientError(f"invalid solid block id from {self.chain.code}")
        return block_id

    def get_solid_block(self, *, block_number: int) -> dict:
        """拉取已固化（BFT 不可逆）整块，含 transactions，用于原生 TRX TransferContract 扫描。"""
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/walletsolidity/getblockbynum",
            request_label="failed to fetch solid block",
            json_body={"num": block_number},
        )
        return response.json()

    def get_transaction_info_by_id(self, tx_hash: str) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/walletsolidity/gettransactioninfobyid",
            request_label="failed to fetch transaction info",
            json_body={"value": tx_hash},
        )
        return response.json()

    def get_account(self, *, address: str) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/getaccount",
            request_label="failed to fetch account",
            json_body={"address": address, "visible": True},
        )
        return response.json()

    def get_account_resource(self, *, address: str) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/getaccountresource",
            request_label="failed to fetch account resource",
            json_body={"address": address, "visible": True},
        )
        return response.json()

    def get_contract(self, *, address: str) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/getcontract",
            request_label="failed to fetch contract",
            json_body={"value": address, "visible": True},
        )
        return response.json()

    def trigger_constant_contract(
        self,
        *,
        owner_address: str,
        contract_address: str,
        function_selector: str,
        parameter: str,
    ) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/triggerconstantcontract",
            request_label="failed to trigger constant contract",
            json_body={
                "owner_address": owner_address,
                "contract_address": contract_address,
                "function_selector": function_selector,
                "parameter": parameter,
                "visible": True,
            },
        )
        return response.json()

    def trigger_smart_contract(
        self,
        *,
        owner_address: str,
        contract_address: str,
        function_selector: str,
        parameter: str,
        fee_limit: int,
    ) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/triggersmartcontract",
            request_label="failed to trigger smart contract",
            json_body={
                "owner_address": owner_address,
                "contract_address": contract_address,
                "function_selector": function_selector,
                "parameter": parameter,
                "fee_limit": int(fee_limit),
                "visible": True,
            },
        )
        return response.json()

    def broadcast_transaction(self, *, transaction: dict) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/broadcasttransaction",
            request_label="failed to broadcast transaction",
            json_body=transaction,
        )
        return response.json()

    def create_trx_transfer(
        self,
        *,
        owner_address: str,
        to_address: str,
        amount_sun: int,
    ) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/createtransaction",
            request_label="failed to create TRX transfer",
            json_body={
                "owner_address": owner_address,
                "to_address": to_address,
                "amount": int(amount_sun),
                "visible": True,
            },
        )
        return response.json()

    def deploy_contract(
        self,
        *,
        owner_address: str,
        name: str,
        abi: list,
        bytecode: str,
        fee_limit: int,
        parameter: str = "",
    ) -> dict:
        response = self._request_with_retry(
            method="POST",
            url=f"{self.base_url}/wallet/deploycontract",
            request_label="failed to deploy contract",
            json_body={
                "owner_address": owner_address,
                "name": name,
                "abi": json.dumps(abi, separators=(",", ":")),
                "bytecode": bytecode.removeprefix("0x"),
                "parameter": parameter,
                "fee_limit": int(fee_limit),
                "call_value": "0",
                "consume_user_resource_percent": 100,
                "origin_energy_limit": int(fee_limit),
                "visible": True,
            },
        )
        return response.json()

    def list_confirmed_trc20_history(
        self,
        *,
        address: str,
        contract_address: str,
        fingerprint: str | None = None,
        limit: int = 200,
    ) -> dict:
        params = {
            "limit": limit,
            "only_confirmed": "true",
            "contract_address": contract_address,
        }
        if fingerprint:
            params["fingerprint"] = fingerprint

        response = self._request_with_retry(
            method="GET",
            url=f"{self.base_url}/v1/accounts/{address}/transactions/trc20",
            request_label="failed to fetch confirmed TRC20 history",
            params=params,
        )
        return response.json()

    def list_confirmed_contract_events(
        self,
        *,
        contract_address: str,
        event_name: str,
        block_number: int,
        fingerprint: str | None = None,
        limit: int = 200,
    ) -> dict:
        params = {
            "event_name": event_name,
            "block_number": block_number,
            "limit": limit,
            "only_confirmed": "true",
        }
        if fingerprint:
            params["fingerprint"] = fingerprint

        response = self._request_with_retry(
            method="GET",
            url=f"{self.base_url}/v1/contracts/{contract_address}/events",
            request_label="failed to fetch confirmed contract events",
            params=params,
        )
        return response.json()
