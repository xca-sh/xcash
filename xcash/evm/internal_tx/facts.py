from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from currencies.models import Crypto


@dataclass(frozen=True)
class MatchedTransferFact:
    """receipt 中提取的真实资产移动事实，由 matcher 返回。"""

    from_address: str
    to_address: str
    crypto: Crypto
    value: Decimal
    amount: Decimal
