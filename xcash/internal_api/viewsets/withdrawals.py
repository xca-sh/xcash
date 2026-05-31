from django.contrib.auth import get_user_model
from django.db import transaction as db_transaction
from internal_api.authentication import InternalTokenAuthentication
from internal_api.serializers.withdrawals import InternalWithdrawalCreateSerializer
from internal_api.serializers.withdrawals import InternalWithdrawalDetailSerializer
from internal_api.serializers.withdrawals import WithdrawalRejectSerializer
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.viewsets import ModelViewSet

from chains.capabilities import ChainProductCapabilityService
from chains.models import Chain
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.permissions import RejectAll
from currencies.models import Crypto
from projects.models import Project
from withdrawals.models import Withdrawal
from withdrawals.models import WithdrawalReviewStatus
from withdrawals.service import WithdrawalService


class InternalWithdrawalViewSet(ModelViewSet):
    authentication_classes = [InternalTokenAuthentication]
    permission_classes = [IsAuthenticated]
    lookup_field = "sys_no"

    def get_queryset(self):
        return (
            Withdrawal.objects.filter(
                project__appid=self.kwargs["project_appid"]
            )
            .select_related("crypto", "chain", "transfer", "reviewed_by")
            .order_by("-created_at", "-pk")
        )

    def get_serializer_class(self):
        if self.action == "create":
            return InternalWithdrawalCreateSerializer
        if self.action == "reject":
            return WithdrawalRejectSerializer
        return InternalWithdrawalDetailSerializer

    def get_permissions(self):
        if self.action in ("create", "list", "retrieve", "approve", "reject"):
            return [IsAuthenticated()]
        return [RejectAll()]

    @db_transaction.atomic
    def create(self, request, *args, **kwargs):
        """创建提币，复用现有 WithdrawalService 的策略校验和提交逻辑。"""
        project = Project.retrieve(self.kwargs["project_appid"])
        if project is None:
            raise APIError(ErrorCode.PROJECT_NOT_FOUND)

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 校验 out_no 唯一
        if Withdrawal.objects.filter(project=project, out_no=data["out_no"]).exists():
            raise APIError(ErrorCode.DUPLICATE_OUT_NO)

        try:
            chain = Chain.objects.get(code=data["chain"], active=True)
        except Chain.DoesNotExist:
            raise APIError(ErrorCode.INVALID_CHAIN) from None

        try:
            crypto = Crypto.objects.get(symbol=data["crypto"], active=True)
        except Crypto.DoesNotExist:
            raise APIError(ErrorCode.INVALID_CRYPTO) from None

        if not crypto.chains.filter(pk=chain.pk).exists():
            raise APIError(ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT)
        if not ChainProductCapabilityService.supports_withdrawal(
            chain=chain,
            crypto=crypto,
        ):
            raise APIError(ErrorCode.INVALID_CHAIN)

        # 用 usd_amount 路径算美元价值：缺价时降级为 0（无价的自定义代币按 0 计，法币限额
        # 自然不约束它），与 WithdrawalService 核心路径一致，避免在此因 to_fiat 缺价报错。
        worth = WithdrawalService.estimate_withdrawal_worth(
            crypto=crypto, amount=data["amount"]
        )

        # 锁 project 做策略校验
        project = Project.objects.select_for_update().get(pk=project.pk)
        WithdrawalService.assert_project_policy(
            project=project,
            chain=chain,
            crypto=crypto,
            to=data["to"],
            amount=data["amount"],
        )
        require_review = WithdrawalService.should_require_review(
            project=project, worth=worth
        )

        withdrawal = Withdrawal.objects.create(
            project=project,
            out_no=data["out_no"],
            to=data["to"],
            crypto=crypto,
            chain=chain,
            amount=data["amount"],
            worth=worth,
            review_status=(
                WithdrawalReviewStatus.REVIEWING if require_review
                else WithdrawalReviewStatus.APPROVED
            ),
        )

        if not require_review:
            WithdrawalService.submit_withdrawal(withdrawal=withdrawal)

        return Response(
            InternalWithdrawalDetailSerializer(withdrawal).data,
            status=201,
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, project_appid=None, sys_no=None):
        """放行审核中的提币，复用 WithdrawalService.approve_withdrawal。"""
        withdrawal = self.get_object()
        if withdrawal.review_status != WithdrawalReviewStatus.REVIEWING:
            raise APIError(ErrorCode.WITHDRAWAL_NOT_REVIEWABLE)

        reviewer = self._get_internal_reviewer()
        withdrawal = WithdrawalService.approve_withdrawal(
            withdrawal_id=withdrawal.pk,
            reviewer=reviewer,
            note="Approved via SaaS internal API",
        )
        return Response(InternalWithdrawalDetailSerializer(withdrawal).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, project_appid=None, sys_no=None):
        """拒绝审核中的提币，复用 WithdrawalService.reject_withdrawal。"""
        withdrawal = self.get_object()
        if withdrawal.review_status != WithdrawalReviewStatus.REVIEWING:
            raise APIError(ErrorCode.WITHDRAWAL_NOT_REVIEWABLE)

        serializer = WithdrawalRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reviewer = self._get_internal_reviewer()
        withdrawal = WithdrawalService.reject_withdrawal(
            withdrawal_id=withdrawal.pk,
            reviewer=reviewer,
            note=serializer.validated_data["reason"],
        )
        return Response(InternalWithdrawalDetailSerializer(withdrawal).data)

    @staticmethod
    def _get_internal_reviewer():
        """获取内网 API 操作的 reviewer 用户。

        WithdrawalService.approve/reject_withdrawal 需要一个 reviewer 对象。
        内网 API 调用来自 SaaS，使用系统超级用户作为 reviewer。
        """
        user_model = get_user_model()
        return user_model.objects.filter(is_superuser=True).first()
