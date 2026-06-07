from rest_framework import serializers

from chains.models import Chain
from currencies.models import Crypto
from currencies.models import CryptoOnChain


class CryptoOnChainSerializer(serializers.ModelSerializer):
    chain = serializers.SlugRelatedField(slug_field="chain", read_only=True)

    class Meta:
        model = CryptoOnChain
        fields = ["chain", "address", "decimals"]


class SaasCryptoSerializer(serializers.ModelSerializer):
    crypto_on_chains = CryptoOnChainSerializer(many=True, read_only=True)

    class Meta:
        model = Crypto
        fields = [
            "name",
            "symbol",
            "is_native",
            "prices",
            "active",
            "crypto_on_chains",
        ]


class SaasChainSerializer(serializers.ModelSerializer):
    native_coin = serializers.SerializerMethodField()

    class Meta:
        model = Chain
        fields = [
            "name",
            "chain",
            "type",
            "native_coin",
            "confirm_block_count",
            "active",
        ]

    def get_native_coin(self, obj) -> str:
        return obj.spec.native_coin_symbol
