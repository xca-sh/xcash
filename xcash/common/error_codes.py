from dataclasses import dataclass
from enum import Enum
from enum import unique
from typing import Any

from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str | Promise
    status: int


@unique
class ErrorCode(Enum):
    # Common
    PARAMETER_ERROR = ErrorInfo("1000", _("参数错误"), 400)

    INVALID_APPID = ErrorInfo("1001", _("AppID无效"), 400)
    IP_FORBIDDEN = ErrorInfo("1002", _("IP禁止"), 403)
    SIGNATURE_ERROR = ErrorInfo("1003", _("签名错误"), 403)
    PROJECT_NOT_READY = ErrorInfo("1004", _("项目未配置"), 400)
    ACCESS_DENY = ErrorInfo("1005", _("无访问权限"), 403)
    NO_FEE = ErrorInfo("1006", _("手续费不足"), 403)
    DUPLICATE_OUT_NO = ErrorInfo("1007", _("单号 out_no 重复"), 400)
    EXPIRED = ErrorInfo("1008", _("Timestamp请求头未设置或过期"), 400)
    REPLAY_ATTACK = ErrorInfo("1009", _("请求重复"), 400)

    # Chain
    INVALID_CHAIN = ErrorInfo("2000", _("无效链"), 400)
    INVALID_CRYPTO = ErrorInfo("2001", _("无效加密货币"), 400)
    CHAIN_CRYPTO_NOT_SUPPORT = ErrorInfo("2002", _("本链不支持此加密货币"), 400)
    INVALID_ADDRESS = ErrorInfo("2003", _("非校验和的地址格式"), 400)
    CANT_CONTRACT_ADDRESS = ErrorInfo("2004", _("合约地址"), 400)
    INVALID_CHAIN_CRYPTO = ErrorInfo("2005", _("链、加密货币设置错误"), 400)

    AMOUNT_PRECISION_EXCEEDED = ErrorInfo(
        "3006", _("金额精度超过该链上代币所支持的小数位"), 400
    )

    # Deposit
    # 修复：同上
    INVALID_UID = ErrorInfo("4000", _("无效UID"), 400)
    RECIPIENT_NOT_CONFIGURED = ErrorInfo(
        "4001", _("项目未配置该链的归集收款地址"), 400
    )

    # Invoice
    INVALID_INVOICE_CURRENCY = ErrorInfo("5000", _("账单收款类型错误"), 400)
    DURATION_ERROR = ErrorInfo("5003", _("账单收款时间错误"), 400)
    INVALID_INVOICE_ID = ErrorInfo("5005", _("无效参数：sys_no"), 400)
    INVALID_INVOICE_STATUS = ErrorInfo("5006", _("账单收款状态错误"), 400)
    CHAIN_CRYPTO_NOT_ALLOWED = ErrorInfo("5007", _("不允许的链与加密货币"), 400)
    NO_RECIPIENT_ADDRESS = ErrorInfo(
        "5008", _("无可用账单收款方式。请确保已设置账单收款地址且 methods 可用。"), 400
    )
    TOO_MANY_WAITING = ErrorInfo("5009", _("待支付记录过多，请勿滥用"), 400)
    NO_AVAILABLE_METHOD = ErrorInfo("5010", _("无效的账单收款方式"), 400)
    INVOICE_NOT_EXIST = ErrorInfo("5011", _("账单收款不存在"), 400)
    INVOICE_EXPIRED = ErrorInfo("5012", _("账单收款已过期"), 400)

    # SaaS API
    INVALID_SAAS_TOKEN = ErrorInfo("6000", _("SaaS API 令牌无效"), 401)
    PROJECT_NOT_FOUND = ErrorInfo("6002", _("项目不存在"), 404)
    FEATURE_NOT_ENABLED = ErrorInfo("6003", _("该功能未开放"), 403)
    ACCOUNT_FROZEN = ErrorInfo("6004", _("账户已冻结"), 403)

    def __init__(self, info: ErrorInfo):
        self._info = info

    @property
    def code(self):
        """获取错误码"""
        return self._info.code

    @property
    def message(self):
        """获取信息"""
        return self._info.message

    @property
    def status(self):
        """获取状态码"""
        return self._info.status

    def to_payload(self, detail: Any = "") -> dict[str, Any]:
        detail_value = "" if detail is None else detail
        return {
            "code": self.code,
            "message": self.message,
            "detail": detail_value,
        }
