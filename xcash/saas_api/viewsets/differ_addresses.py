from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.viewsets import ModelViewSet
from saas_api.authentication import SaasTokenAuthentication
from saas_api.serializers.differ_addresses import DifferRecipientAddressSerializer

from common.error_codes import ErrorCode
from common.exceptions import APIError
from invoices.models import DifferRecipientAddress
from projects.models import Project


class SaasDifferRecipientAddressViewSet(ModelViewSet):
    """项目钱包直收地址池：商户在钱包直收模式下自管的收款 EOA 列表。

    作用域严格限定在 URL 的 project_appid，杜绝跨项目读写他人地址池。
    地址池小而固定，关闭分页让 UI 一次取全。可按 ?chain_type=evm|tron 过滤。
    """

    authentication_classes = [SaasTokenAuthentication]
    permission_classes = [IsAuthenticated]
    serializer_class = DifferRecipientAddressSerializer
    pagination_class = None
    # 显式方法白名单：禁用 PUT（避免整体替换语义），仅 list/create/patch/delete。
    # PATCH 只能改启用状态与排序；地址、链类型创建后不可修改，删除需无未完结账单。
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_project(self) -> Project:
        # 单请求内复用，避免 get_queryset / get_serializer_context / perform_create 反复查库。
        if not hasattr(self, "_project"):
            project = Project.retrieve(self.kwargs["project_appid"])
            if project is None:
                raise APIError(ErrorCode.PROJECT_NOT_FOUND)
            self._project = project
        return self._project

    def get_queryset(self):
        queryset = DifferRecipientAddress.objects.filter(
            project__appid=self.kwargs["project_appid"]
        ).order_by("chain_type", "sort_order", "pk")
        chain_type = self.request.query_params.get("chain_type")
        if chain_type:
            queryset = queryset.filter(chain_type=chain_type)
        return queryset

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # 序列化器的地址校验需要 project（唯一性与归属判定），从作用域注入。
        context["project"] = self.get_project()
        return context

    def perform_create(self, serializer):
        serializer.save(project=self.get_project())

    def perform_destroy(self, instance):
        # 仍有未完结账单引用该地址时模型拒绝删除，翻译成 400 而不是 500。
        try:
            instance.delete()
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages}) from exc
