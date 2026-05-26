from rest_framework import serializers

from chains.serializers import TransferSerializer
from withdrawals.models import HotWalletFunding
from withdrawals.models import WithdrawalReviewLog


class HotWalletFundingSerializer(serializers.ModelSerializer):
    tx = TransferSerializer(source="transfer", read_only=True)

    class Meta:
        model = HotWalletFunding
        fields = [
            "id",
            "tx",
        ]


class WithdrawalReviewLogSerializer(serializers.ModelSerializer):
    withdrawal_sys_no = serializers.CharField(source="withdrawal.sys_no", read_only=True)
    actor = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = WithdrawalReviewLog
        fields = [
            "id",
            "withdrawal_sys_no",
            "actor",
            "action",
            "from_status",
            "to_status",
            "note",
            "created_at",
        ]
