from rest_framework import serializers

from chains.models import VaultSlotBalance
from common.serializers import StrippedDecimalField


class SaasVaultSlotBalanceSerializer(serializers.ModelSerializer):
    vault_slot_address = serializers.CharField(source="vault_slot.address", read_only=True)
    usage = serializers.CharField(source="vault_slot.usage", read_only=True)
    customer_uid = serializers.CharField(source="vault_slot.customer.uid", read_only=True, allow_null=True)
    invoice_index = serializers.IntegerField(source="vault_slot.invoice_index", read_only=True, allow_null=True)
    chain = serializers.CharField(source="chain.code", read_only=True)
    crypto = serializers.CharField(source="crypto.symbol", read_only=True)
    value = StrippedDecimalField(read_only=True, max_digits=80, decimal_places=0)
    amount = StrippedDecimalField(read_only=True, max_digits=80, decimal_places=30)
    worth = StrippedDecimalField(read_only=True, max_digits=80, decimal_places=30)

    class Meta:
        model = VaultSlotBalance
        fields = [
            "id",
            "vault_slot_address",
            "usage",
            "customer_uid",
            "invoice_index",
            "chain",
            "crypto",
            "value",
            "amount",
            "worth",
            "synced_block_number",
            "synced_at",
            "last_tx_hash",
            "updated_at",
        ]
