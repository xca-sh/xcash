from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from projects.models import DifferRecipientAddress


class DifferRecipientAddressCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DifferRecipientAddress
        fields = ["name", "chain_type", "address"]
        # 禁用自动生成的唯一约束校验器，改用 validate 提供可读错误信息
        validators = []

    def validate(self, attrs):
        if DifferRecipientAddress.objects.filter(
            chain_type=attrs["chain_type"],
            address=attrs["address"],
        ).exists():
            raise serializers.ValidationError(
                {"address": _("该地址已被使用，同一链类型下地址不能重复。")}
            )
        return attrs


class DifferRecipientAddressDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = DifferRecipientAddress
        fields = ["id", "name", "chain_type", "address", "created_at"]
