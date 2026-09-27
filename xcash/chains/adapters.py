from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from chains.models import Chain
from chains.registry import get_chain_family
from chains.types import AddressStr
from currencies.models import Crypto


class TxCheckStatus(StrEnum):
    """链上交易结果查询的内存枚举。

    这里只描述“当前查到的交易结果”，不落库，也不参与业务状态机建模。
    SUCCEEDED 表示适配器已查到成功交易结果；需要区块确认窗口的业务必须在调用侧
    另行校验 block_number / block_hash 与 chain.confirm_block_count 后再进入确认态。
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MISSING = "missing"


@dataclass(frozen=True, eq=False)
class TxCheckResult:
    """链上交易状态及 receipt 位置元信息。

    兼容旧代码中直接把 tx_result 返回值与 TxCheckStatus 比较的写法，同时让
    确认任务能在 reorg 后发现 blockNumber / blockHash 变化并刷新确认起点。
    """

    status: TxCheckStatus
    block_number: int | None = None
    block_hash: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TxCheckStatus):
            return self.status == other
        if isinstance(other, str):
            return self.status == other
        if isinstance(other, TxCheckResult):
            return (
                self.status == other.status
                and self.block_number == other.block_number
                and self.block_hash == other.block_hash
            )
        return NotImplemented

    def __hash__(self) -> int:
        # 与 TxCheckStatus 的兼容相等比较保持一致；不同 receipt 元信息出现哈希碰撞可接受。
        return hash(self.status)


class AdapterInterface(ABC):
    """链适配器接口：负责地址验证、余额查询、交易结果查询。

    交易签名与广播逻辑已从 Adapter 层移除，统一由各链专属的 XxxTxTask 模型负责：
    - EVM：evm.EvmTxTask.schedule(intent)
    """

    @abstractmethod
    def validate_address(self, address: AddressStr) -> bool:
        pass

    @abstractmethod
    def is_address(self, chain: Chain, address: AddressStr) -> bool:
        pass

    @abstractmethod
    def is_contract(self, chain: Chain, address: AddressStr) -> bool:
        pass

    @abstractmethod
    def get_balance(self, address: AddressStr, chain: Chain, crypto: Crypto) -> int:
        pass

    @abstractmethod
    def tx_result(
        self, chain, tx_hash: str
    ) -> TxCheckStatus | TxCheckResult | Exception:
        pass


class AdapterFactory:
    # 各链族在 AppConfig.ready() 里把适配器登记到 chains.registry，这里只按链类型查表，
    # chains 因此不再反向 import evm / tron。

    @staticmethod
    def get_adapter(chain_type: str) -> AdapterInterface:
        try:
            family = get_chain_family(chain_type)
        except ValueError:
            raise ValueError(f"Unsupported chain adapter: {chain_type}") from None
        return family.adapter_class()
