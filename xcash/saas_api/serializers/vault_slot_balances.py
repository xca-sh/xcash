from rest_framework import serializers

from chains.constants import ChainType
from chains.models import VaultSlotBalance
from chains.vault_slots import get_backend
from common.serializers import StrippedDecimalField


class SaasVaultSlotBalanceSerializer(serializers.ModelSerializer):
    vault_slot_address = serializers.CharField(
        source="vault_slot.address", read_only=True
    )
    usage = serializers.CharField(source="vault_slot.usage", read_only=True)
    customer_uid = serializers.CharField(
        source="vault_slot.customer.uid", read_only=True, allow_null=True
    )
    invoice_index = serializers.IntegerField(
        source="vault_slot.invoice_index", read_only=True, allow_null=True
    )
    chain = serializers.CharField(source="chain.code", read_only=True)
    # 以下字段供商户在 SaaS 用自己的钱包「部署 + 归集」：都是链上公开数据，
    # 部署入参（factory / vault / salt）与归集入参（token_address）直接下发，前端不再推导。
    chain_type = serializers.CharField(source="chain.type", read_only=True)
    chain_id = serializers.IntegerField(
        source="chain.chain_id", read_only=True, allow_null=True
    )
    is_deployed = serializers.BooleanField(
        source="vault_slot.is_deployed", read_only=True
    )
    salt = serializers.SerializerMethodField()
    vault = serializers.SerializerMethodField()
    factory = serializers.SerializerMethodField()
    token_address = serializers.SerializerMethodField()
    crypto = serializers.CharField(source="crypto.symbol", read_only=True)
    value = StrippedDecimalField(read_only=True, max_digits=32, decimal_places=0)
    amount = StrippedDecimalField(read_only=True, max_digits=32, decimal_places=8)
    worth = StrippedDecimalField(read_only=True, max_digits=32, decimal_places=4)

    class Meta:
        model = VaultSlotBalance
        fields = [
            "id",
            "vault_slot_address",
            "usage",
            "customer_uid",
            "invoice_index",
            "chain",
            "chain_type",
            "chain_id",
            "is_deployed",
            "salt",
            "vault",
            "factory",
            "token_address",
            "crypto",
            "value",
            "amount",
            "worth",
            "synced_block_number",
            "synced_at",
            "last_tx_hash",
            "updated_at",
        ]

    def get_salt(self, obj) -> str:
        return "0x" + bytes(obj.vault_slot.salt).hex()

    def get_vault(self, obj) -> str:
        # VaultSlot 地址由 CREATE2(factory, salt, vault) 确定，vault 取项目在该链族的归集地址。
        project = obj.vault_slot.project
        if obj.chain.type == ChainType.TRON:
            return project.tron_vault or ""
        return project.evm_vault or ""

    def get_factory(self, obj) -> str | None:
        # 未登记基础合约的链返回 None，前端据此隐藏归集入口。
        try:
            return obj.chain.vault_slot_contract_addresses().factory
        except (RuntimeError, ValueError):
            return None

    def get_token_address(self, obj) -> str:
        """collect(token) 的入参：本链原生币为零地址，代币为其合约地址。"""
        return get_backend(obj.chain).collect_token_address(
            crypto=obj.crypto, chain=obj.chain
        )
