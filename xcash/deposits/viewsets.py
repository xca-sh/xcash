import re
import time

import structlog
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction as db_transaction
from django.utils.decorators import method_decorator
from rest_framework import viewsets
from rest_framework.decorators import action as view_action
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response

from chains.capabilities import ChainProductCapabilityService
from chains.models import Chain
from chains.models import ChainType
from chains.models import VaultSlot
from common.consts import APPID_HEADER
from common.error_codes import ErrorCode
from common.exceptions import APIError
from common.permission_check import check_saas_permission
from common.throttles import VaultSlotThrottle
from currencies.service import CryptoService
from projects.models import Customer
from projects.models import Project

logger = structlog.get_logger()

# uid 合法字符：字母、数字、下划线、中划线，长度 1~128
_UID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]{1,128}$")

# 本地联调（DEBUG）下同步等待 VaultSlot 充币地址上链的有界轮询参数。
_DEPLOY_WAIT_TIMEOUT_SECONDS = 30.0
_DEPLOY_WAIT_POLL_INTERVAL_SECONDS = 0.2


@method_decorator(db_transaction.non_atomic_requests, name="dispatch")
class DepositViewSet(viewsets.GenericViewSet):
    """
    充币相关接口。仅暴露 address 这一个 action，不注册 ModelViewSet 的默认 CRUD 路由。
    """

    @view_action(
        methods=["get"],
        detail=False,
        permission_classes=[AllowAny],
        throttle_classes=[VaultSlotThrottle],
    )
    def address(self, request: Request):
        """
        获取客户在指定链上的充币地址。

        请求头：XC-Appid
        Query 参数：uid、chain（链代码）、crypto（代币符号）
        """
        appid = request.headers.get(APPID_HEADER, None)
        if not appid:
            raise APIError(ErrorCode.INVALID_APPID)
        project = Project.retrieve(appid=appid)
        if project is None:
            raise APIError(ErrorCode.INVALID_APPID)

        # v2 SaaS 模式：校验该 tier 是否开放 deposit 功能
        check_saas_permission(appid=appid, action="deposit")

        uid = request.GET.get("uid")
        chain_code = request.GET.get("chain")
        crypto_symbol = request.GET.get("crypto", "")

        if not uid or not _UID_PATTERN.match(uid):
            raise APIError(ErrorCode.INVALID_UID)

        try:
            chain = Chain.objects.get(code=chain_code, active=True)
        except Chain.DoesNotExist as exc:
            raise APIError(ErrorCode.INVALID_CHAIN) from exc

        # 主网/测试网门控：测试项目只能在测试网建充币地址，非测试项目只能用主网，
        # 隔离两类代币防止资金/记账混淆。
        if chain.is_testnet != project.is_test:
            raise APIError(ErrorCode.INVALID_CHAIN)

        try:
            crypto = CryptoService.get_by_symbol(crypto_symbol)
        except ObjectDoesNotExist as exc:
            raise APIError(ErrorCode.INVALID_CRYPTO) from exc

        # 停用的币不允许申请充币地址，避免用户入金后因币种未启用而没有 Deposit 记录。
        if not crypto.active:
            raise APIError(ErrorCode.INVALID_CRYPTO)

        if not crypto.support_this_chain(chain=chain):
            raise APIError(
                ErrorCode.CHAIN_CRYPTO_NOT_SUPPORT,
                detail=f"{crypto_symbol} 不支持 {chain_code} 链",
            )
        if not ChainProductCapabilityService.supports_deposit_address(
            chain=chain,
            crypto=crypto,
        ):
            raise APIError(ErrorCode.INVALID_CHAIN)
        customer, _ = Customer.objects.get_or_create(project=project, uid=uid)

        deposit_address = VaultSlot.ensure_deposit_address(
            chain,
            customer,
            crypto=crypto,
        )
        if (
            settings.DEBUG
            and chain.type == ChainType.EVM
            and crypto.pk == chain.native_coin.pk
        ):
            wait_deposit_address_deployed(chain=chain, address=deposit_address)
        return Response({"deposit_address": deposit_address})


def wait_deposit_address_deployed(*, chain: Chain, address: str) -> None:
    """本地联调（DEBUG）下同步等待充币地址对应的 VaultSlot 合约上链。

    仅用于本地端到端验证，让端点返回时地址即可用。有界轮询 + 异常容忍：节点抖动时
    记日志后继续重试，到 _DEPLOY_WAIT_TIMEOUT_SECONDS 仍未部署则记日志后放行（地址
    本身有效，部署最终异步完成），避免请求线程被无限挂起。
    """
    deadline = time.monotonic() + _DEPLOY_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if len(chain.w3.eth.get_code(address)) > 0:  # noqa: SLF001
                return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "deposits.wait_deposit_address_deployed_error",
                chain=chain.code,
                address=address,
                error=str(exc),
            )
        time.sleep(_DEPLOY_WAIT_POLL_INTERVAL_SECONDS)
    logger.warning(
        "deposits.wait_deposit_address_deployed_timeout",
        chain=chain.code,
        address=address,
        timeout=_DEPLOY_WAIT_TIMEOUT_SECONDS,
    )
