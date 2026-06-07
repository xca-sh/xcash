from rest_framework import serializers

from chains.serializers import TransferSerializer
from invoices.models import Invoice


class SaasInvoiceDetailSerializer(serializers.ModelSerializer):
    tx = TransferSerializer(source="transfer", read_only=True)
    crypto = serializers.SlugRelatedField(slug_field="symbol", read_only=True)
    chain = serializers.SlugRelatedField(slug_field="code", read_only=True)

    class Meta:
        model = Invoice
        fields = [
            "sys_no",
            "out_no",
            "title",
            "currency",
            "amount",
            "methods",
            "crypto",
            "chain",
            "pay_amount",
            "pay_address",
            "worth",
            "status",
            "protocol",
            "risk_level",
            "risk_score",
            "tx",
            "started_at",
            "expires_at",
            "notify_url",
            "return_url",
            "created_at",
            "updated_at",
        ]
