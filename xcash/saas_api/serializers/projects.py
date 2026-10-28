import copy
import ipaddress

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import serializers

from chains.models import ChainType
from projects.models import Project
from webhooks.service import delivery_url_scheme_allowed

# 业务校验上下界，集中声明便于审计与调整。
HMAC_KEY_MIN_LENGTH = 16
# 模型层 ShortUUIDField(length=32) 硬性限制 max_length=32，
# 这里给出一个不超过模型上限的安全值；DRF 会合并 model 的 max_length 校验。
HMAC_KEY_MAX_LENGTH = 32
IP_WHITE_LIST_MAX_ENTRIES = 100


def validate_webhook_url(value: str) -> str:
    # URLField 已校验格式；这里只放行可投递的地址：https，内网放行模式下另放行 http。
    if not value:
        return value
    is_http_url = value.startswith(("http://", "https://"))
    if not is_http_url or not delivery_url_scheme_allowed(value):
        raise serializers.ValidationError("webhook 必须使用 https 地址")
    return value


def validate_contract_receiving_change(project: Project, changes: dict) -> None:
    """在 project 现值上叠加本次改动，按智能合约收款的配置不变量校验最终状态。

    不变量定义在 Project.validate_contract_receiving，admin 与 SaaS API 共用同一口径；
    这里在副本上叠加改动，校验失败不会污染调用方持有的实例。
    """
    candidate = copy.copy(project)
    for field, value in changes.items():
        setattr(candidate, field, value)
    try:
        candidate.validate_contract_receiving()
    except DjangoValidationError as exc:
        raise serializers.ValidationError(exc.message_dict) from exc


class ProjectCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["name", "webhook"]
        extra_kwargs = {"webhook": {"required": False}}

    def validate_webhook(self, value: str) -> str:
        return validate_webhook_url(value)

    def create(self, validated_data):
        if settings.DEBUG:
            validated_data.setdefault("is_test", True)
        return super().create(validated_data)


class ProjectUpdateSerializer(serializers.ModelSerializer):
    """商户可编辑的项目字段白名单，附带业务校验。

    与 ProjectDetailSerializer（只读展示）分离，严禁让 PATCH 回退到 Detail。
    """

    class Meta:
        model = Project
        fields = [
            "webhook",
            "hmac_key",
            "ip_white_list",
            "fast_confirm_threshold",
            # 账单收款模式按链配置。商户可自助切换；切换只影响后续新账单的地址分配，
            # 不动已存在账单。choices 校验由 ModelSerializer 从模型字段自动派生。
            "evm_invoice_receiving_mode",
            "tron_invoice_receiving_mode",
            # 智能合约收款开关（SaaS 钱包页的简洁/完整模式）。关闭时不能有归集地址，
            # 账单模式选智能合约须先开启并设置该链归集地址，见 validate()。
            "contract_receiving_enabled",
            # AML 筛查按项目配置，商户自助开关。阈值非负由模型 MinValueValidator 派生校验。
            "aml_screening_enabled",
            "aml_screening_threshold_usd",
        ]
        extra_kwargs = {field: {"required": False} for field in fields}

    def validate_webhook(self, value: str) -> str:
        return validate_webhook_url(value)

    def validate_hmac_key(self, value: str) -> str:
        if len(value) < HMAC_KEY_MIN_LENGTH or len(value) > HMAC_KEY_MAX_LENGTH:
            raise serializers.ValidationError(
                f"hmac_key 长度需在 {HMAC_KEY_MIN_LENGTH}~{HMAC_KEY_MAX_LENGTH} 之间"
            )
        return value

    def validate_ip_white_list(self, value: str) -> str:
        """校验格式：`*`、空串、或逗号分隔的 IP/CIDR 列表。"""
        stripped = value.strip()
        if stripped in {"", "*"}:
            return stripped
        entries = [e.strip() for e in stripped.split(",") if e.strip()]
        if len(entries) > IP_WHITE_LIST_MAX_ENTRIES:
            raise serializers.ValidationError(
                f"IP 白名单最多 {IP_WHITE_LIST_MAX_ENTRIES} 条"
            )
        for entry in entries:
            try:
                # ip_network 同时接受纯 IP 和 CIDR 表示。
                ipaddress.ip_network(entry, strict=False)
            except ValueError:
                raise serializers.ValidationError(
                    f"IP 白名单格式不合法: {entry}"
                ) from None
        return stripped

    def validate_fast_confirm_threshold(self, value):
        if value < 0:
            raise serializers.ValidationError("fast_confirm_threshold 不能为负数")
        return value

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is not None:
            validate_contract_receiving_change(self.instance, attrs)
        return attrs

    def update(self, instance, validated_data):
        # validate() 读的是未加锁的实例：关闭开关的同时另一请求可能刚设置了归集地址。
        # 锁定行后按最新数据复查（与 vault 接口的行锁互斥），并在锁定行上写入，
        # 避免用过期实例整行保存。
        with transaction.atomic():
            locked = Project.objects.select_for_update().get(pk=instance.pk)
            validate_contract_receiving_change(locked, validated_data)
            return super().update(locked, validated_data)


class ProjectVaultSetSerializer(serializers.Serializer):
    """商户首次设置指定链类型的收款归集地址（Vault）。

    每个链类型的 vault 都是一次性写入、不可修改的归集地址。immutability（已设置则拒绝）
    在视图层先行拦截；这里只做链类型与地址格式校验，不做链上、多签或部署状态校验。
    """

    chain_type = serializers.ChoiceField(choices=ChainType.choices)
    vault = serializers.CharField()

    def validate(self, attrs):
        try:
            Project.validate_vault_address_for_chain_type(
                chain_type=attrs["chain_type"],
                address=attrs["vault"],
            )
            project = self.context.get("project")
            if project is not None:
                project.validate_vault_address_is_globally_unique(
                    address=attrs["vault"],
                )
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"vault": exc.message}) from exc
        return attrs


class ProjectDetailSerializer(serializers.ModelSerializer):
    evm_vault_address = serializers.SerializerMethodField()
    tron_vault_address = serializers.SerializerMethodField()
    is_ready = serializers.SerializerMethodField()
    ready_errors = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "appid",
            "name",
            "webhook",
            "ip_white_list",
            "hmac_key",
            "fast_confirm_threshold",
            "evm_invoice_receiving_mode",
            "tron_invoice_receiving_mode",
            "contract_receiving_enabled",
            "aml_screening_enabled",
            "aml_screening_threshold_usd",
            "evm_vault_address",
            "tron_vault_address",
            "is_ready",
            "ready_errors",
            "active",
            "created_at",
        ]

    def get_evm_vault_address(self, obj):
        return obj.evm_vault or None

    def get_tron_vault_address(self, obj):
        return obj.tron_vault or None

    def get_is_ready(self, obj):
        ready, _ = obj.is_ready
        return ready

    def get_ready_errors(self, obj):
        _, errors = obj.is_ready
        return [str(e) for e in errors]
