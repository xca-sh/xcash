import ipaddress

from rest_framework import serializers

from projects.models import Project

# 业务校验上下界，集中声明便于审计与调整。
HMAC_KEY_MIN_LENGTH = 16
# 模型层 ShortUUIDField(length=32) 硬性限制 max_length=32，
# 这里给出一个不超过模型上限的安全值；DRF 会合并 model 的 max_length 校验。
HMAC_KEY_MAX_LENGTH = 32
IP_WHITE_LIST_MAX_ENTRIES = 100


class ProjectCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Project
        fields = ["name", "webhook"]
        extra_kwargs = {"webhook": {"required": False}}


class ProjectUpdateSerializer(serializers.ModelSerializer):
    """商户可编辑的项目字段白名单，附带业务校验。

    与 ProjectDetailSerializer（只读展示）分离，严禁让 PATCH 回退到 Detail。
    """

    class Meta:
        model = Project
        fields = [
            "webhook",
            "webhook_open",
            "hmac_key",
            "ip_white_list",
            "fast_confirm_threshold",
            "pre_notify",
        ]
        extra_kwargs = {field: {"required": False} for field in fields}

    def validate_webhook(self, value: str) -> str:
        # URLField 已校验 URL 格式；此处额外要求必须 http/https（避免 ftp/javascript 等）。
        if value and not value.startswith(("http://", "https://")):
            raise serializers.ValidationError("webhook 必须以 http:// 或 https:// 开头")
        return value

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


class ProjectVaultSetSerializer(serializers.Serializer):
    """商户首次设置收款归集地址（Vault）。

    vault 是一次性写入、不可修改的归集地址。immutability（已设置则拒绝）
    在视图层先行拦截；这里不做链上、多签或部署状态校验。
    """

    vault = serializers.CharField()


class ProjectDetailSerializer(serializers.ModelSerializer):
    vault_address = serializers.SerializerMethodField()
    is_ready = serializers.SerializerMethodField()
    ready_errors = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = [
            "appid",
            "name",
            "webhook",
            "webhook_open",
            "failed_count",
            "ip_white_list",
            "hmac_key",
            "fast_confirm_threshold",
            "pre_notify",
            "vault_address",
            "is_ready",
            "ready_errors",
            "active",
            "created_at",
        ]

    def get_vault_address(self, obj):
        # 商户的资金最终汇入其收款归集地址（VaultSlot 合约写死的转发目标），
        # 故对外暴露 Project.vault 才是语义正确的“金库地址”；未配置时返回 None。
        return obj.vault or None

    def get_is_ready(self, obj):
        ready, _ = obj.is_ready
        return ready

    def get_ready_errors(self, obj):
        _, errors = obj.is_ready
        return [str(e) for e in errors]
