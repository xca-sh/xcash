from __future__ import annotations

import json
import math
from dataclasses import dataclass

from django.conf import settings
from tron.client import TronClientError
from tron.client import TronHttpClient


class TronResourceGuardError(TronClientError):
    """本地资源预检无法证明交易不会燃烧 TRX，因此拒绝广播。"""


@dataclass(frozen=True)
class TronResourceQuote:
    estimated_energy: int
    required_energy: int
    available_energy: int
    required_bandwidth: int | None = None
    available_bandwidth: int | None = None


def available_energy(resource: dict) -> int:
    return max(
        int_payload_value(resource, "EnergyLimit")
        - int_payload_value(resource, "EnergyUsed"),
        0,
    )


def available_bandwidth(resource: dict) -> int:
    free_bandwidth = int_payload_value(resource, "freeNetLimit") - int_payload_value(
        resource,
        "freeNetUsed",
    )
    staked_bandwidth = int_payload_value(resource, "NetLimit") - int_payload_value(
        resource,
        "NetUsed",
    )
    return max(free_bandwidth, 0) + max(staked_bandwidth, 0)


def int_payload_value(payload: dict, key: str) -> int:
    try:
        return int(payload.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def safety_margin_bps() -> int:
    return max(
        int(getattr(settings, "TRON_RESOURCE_SAFETY_MARGIN_BPS", 12_000)),
        10_000,
    )


def bandwidth_safety_bytes() -> int:
    return max(int(getattr(settings, "TRON_BANDWIDTH_SAFETY_BYTES", 512)), 0)


def with_safety_margin(value: int) -> int:
    return math.ceil(int(value) * safety_margin_bps() / 10_000)


def estimate_contract_call_energy(
    *,
    client: TronHttpClient,
    owner_address: str,
    contract_address: str,
    function_selector: str,
    parameter: str,
) -> int:
    payload = client.trigger_constant_contract(
        owner_address=owner_address,
        contract_address=contract_address,
        function_selector=function_selector,
        parameter=parameter,
    )
    result = payload.get("result") or {}
    if not isinstance(result, dict) or result.get("result") is not True:
        message = result.get("message") if isinstance(result, dict) else payload
        raise TronResourceGuardError(f"tron energy estimate failed: {message}")

    estimated = int_payload_value(payload, "energy_used")
    if estimated <= 0:
        estimated = int_payload_value(payload, "energy_required")
    if estimated <= 0:
        raise TronResourceGuardError("tron energy estimate missing energy_used")
    return estimated


def require_energy_for_contract_call(
    *,
    client: TronHttpClient,
    owner_address: str,
    contract_address: str,
    function_selector: str,
    parameter: str,
) -> TronResourceQuote:
    estimated = estimate_contract_call_energy(
        client=client,
        owner_address=owner_address,
        contract_address=contract_address,
        function_selector=function_selector,
        parameter=parameter,
    )
    required = with_safety_margin(estimated)
    resource = client.get_account_resource(address=owner_address)
    available = available_energy(resource)
    if available < required:
        raise TronResourceGuardError(
            "tron energy insufficient: "
            f"required={required} estimated={estimated} available={available}"
        )
    return TronResourceQuote(
        estimated_energy=estimated,
        required_energy=required,
        available_energy=available,
    )


def estimate_signed_transaction_bandwidth(transaction: dict) -> int:
    encoded = json.dumps(
        transaction,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(encoded)


def require_bandwidth_for_signed_transaction(
    *,
    client: TronHttpClient,
    owner_address: str,
    transaction: dict,
    quote: TronResourceQuote,
) -> TronResourceQuote:
    required = (
        estimate_signed_transaction_bandwidth(transaction) + bandwidth_safety_bytes()
    )
    resource = client.get_account_resource(address=owner_address)
    available = available_bandwidth(resource)
    if available < required:
        raise TronResourceGuardError(
            "tron bandwidth insufficient: "
            f"required={required} available={available}"
        )
    return TronResourceQuote(
        estimated_energy=quote.estimated_energy,
        required_energy=quote.required_energy,
        available_energy=quote.available_energy,
        required_bandwidth=required,
        available_bandwidth=available,
    )
