from rest_framework import serializers

from chains.serializers import TransferSerializer
from deposits.models import Deposit


class SaasDepositDetailSerializer(serializers.ModelSerializer):
    tx = TransferSerializer(source="transfer", read_only=True)
    uid = serializers.CharField(source="customer.uid", read_only=True)
    status = serializers.CharField(source="transfer.status", read_only=True)
    crypto = serializers.SlugRelatedField(
        source="transfer.crypto", slug_field="symbol", read_only=True
    )
    chain = serializers.SlugRelatedField(
        source="transfer.chain", slug_field="code", read_only=True
    )
    # 风险结果只存在收款转账的 AML 评估上；未筛查时为 null。
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
        model = Deposit
        fields = [
            "sys_no",
            "uid",
            "crypto",
            "chain",
            "worth",
            "status",
            "risk_level",
            "risk_score",
            "tx",
            "created_at",
            "updated_at",
        ]
