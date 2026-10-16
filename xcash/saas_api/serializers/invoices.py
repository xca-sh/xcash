from rest_framework import serializers

from chains.serializers import TransferSerializer
from invoices.models import Invoice


class SaasInvoiceDetailSerializer(serializers.ModelSerializer):
    tx = TransferSerializer(source="transfer", read_only=True)
    # currency FK 的 PK 即法币 code，直接取 currency_id 输出字符串，省一次 join。
    currency = serializers.CharField(source="currency_id", read_only=True)
    crypto = serializers.SlugRelatedField(slug_field="symbol", read_only=True)
    chain = serializers.SlugRelatedField(slug_field="code", read_only=True)
    # 风险结果只存在收款转账的 AML 评估上；未绑定转账、未筛查时为 null。
    risk_level = serializers.CharField(
        source="transfer.aml_assessment.risk_level", read_only=True, allow_null=True
    )
    risk_score = serializers.DecimalField(
        source="transfer.aml_assessment.risk_score",
        max_digits=6,
        decimal_places=2,
        read_only=True,
        allow_null=True,
    )

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
