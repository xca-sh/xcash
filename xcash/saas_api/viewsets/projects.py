from django.db import transaction as db_transaction
from rest_framework import status as drf_status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.projects import ProjectCreateSerializer
from saas_api.serializers.projects import ProjectDetailSerializer
from saas_api.serializers.projects import ProjectUpdateSerializer
from saas_api.serializers.projects import ProjectVaultSetSerializer

from invoices.models import EpayMerchant
from projects.models import Project


class ProjectViewSet(ModelViewSet):
    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    # Project 模型本身未在 Meta 里声明 ordering，启用全局分页后必须显式排序，
    # 否则 DRF 分页器会警告翻页结果可能重复/缺失。按创建时间倒序是列表页直觉顺序。
    queryset = Project.objects.all().order_by("-created_at", "-pk")
    lookup_field = "appid"
    # 安全白名单：仅允许读取、创建和局部更新；显式禁用 PUT/DELETE 避免绕过字段白名单
    # （PUT 会回退到 ProjectDetailSerializer，能改 appid/name/active；DELETE 会直接删项目）。
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return ProjectCreateSerializer
        if self.action == "partial_update":
            return ProjectUpdateSerializer
        if self.action == "update":
            # PUT 已被 http_method_names 禁用；若 future 有人重新打开，
            # 这里直接 raise 防止 fallthrough 到 ProjectDetailSerializer
            # 导致 name/appid/active 等字段被写入。
            raise NotImplementedError("PUT not supported; use PATCH")
        return ProjectDetailSerializer

    def perform_create(self, serializer):
        serializer.save()
        # 系统级 lazy create：项目落库后立即分配 EpayMerchant，
        # 保证每个项目从注册一刻起就具备 EPay 收款能力，无需用户在 UI 手动启用。
        EpayMerchant.ensure_for_project(serializer.instance)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        detail = ProjectDetailSerializer(serializer.instance)
        return Response(detail.data, status=drf_status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def vault(self, request, appid=None):
        """商户首次设置指定链类型的收款归集地址（Vault），一经设置不可修改。

        POST /projects/{appid}/vault  body: {"chain_type": "evm|tron", "vault": "..."}
        - 已设置 → 409，明确告知不可修改；
        - 未设置 → 写入后返回项目详情。
        """
        project = self.get_object()
        serializer = ProjectVaultSetSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        chain_type = serializer.validated_data["chain_type"]
        vault_field = Project.vault_field_for_chain_type(chain_type)
        # 先做无锁短路：已设置直接拒绝，避免常见的"重复点击"进入事务。
        if getattr(project, vault_field):
            return Response(
                {"vault": "收款归集地址一旦设置不可修改。"},
                status=drf_status.HTTP_409_CONFLICT,
            )
        new_vault = serializer.validated_data["vault"]

        # 加行锁后复查再写：vault 不可变且关乎资金去向，必须杜绝并发下两个请求都通过空值检查。
        with db_transaction.atomic():
            locked = Project.objects.select_for_update().get(pk=project.pk)
            if getattr(locked, vault_field):
                return Response(
                    {"vault": "收款归集地址一旦设置不可修改。"},
                    status=drf_status.HTTP_409_CONFLICT,
                )
            setattr(locked, vault_field, new_vault)
            locked.save(update_fields=[vault_field])
        return Response(ProjectDetailSerializer(locked).data)

    @action(detail=True, methods=["post"])
    def activate(self, request, appid=None):
        project = self.get_object()
        project.active = True
        project.save(update_fields=["active"])
        return Response(ProjectDetailSerializer(project).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, appid=None):
        project = self.get_object()
        project.active = False
        project.save(update_fields=["active"])
        return Response(ProjectDetailSerializer(project).data)

    @action(detail=True, methods=["get"], url_path="receivable-methods")
    def receivable_methods(self, request, appid=None):
        """当前收款配置下真正生效的 crypto→链 列表，供 UI 展示哪些币种可收。

        结果由「每条链的收款模式 × 能力矩阵 × 前置条件（vault/地址池）」共同推导，
        商户切换模式或增减差额地址后会随之变化，是配置是否生效的权威反馈。
        """
        from invoices.models import Invoice

        project = self.get_object()
        return Response(Invoice.available_methods(project))
