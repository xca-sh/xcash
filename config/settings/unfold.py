from django.templatetags.static import static
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _

BASE_UNFOLD = {
    "SITE_TITLE": "Xcash",
    "SITE_HEADER": "Xcash",
    "SITE_URL": "https://xca.sh/",
    "SITE_SYMBOL": "dashboard",  # symbol from icon set
    "DASHBOARD_CALLBACK": "core.dashboard.dashboard_callback",
    "LOGIN": {
        "image": lambda request: static("login-bg.jpg"),
    },
    # django-unfold 发布的是 Tailwind 预编译产物，只包含它自身模板用到的类；
    # 项目模板需要的少量补充能力（等宽数字、行分隔、语义色指标卡等）由这份样式提供。
    "STYLES": [
        lambda request: static("core/css/admin.css"),
    ],
    "SITE_FAVICONS": [
        {
            "rel": "icon",
            "sizes": "32x32",
            "type": "image/png",
            "href": lambda request: static("logo.png"),
        },
    ],
    "SITE_ICON": {
        "light": lambda request: static("logo.png"),  # light mode
        "dark": lambda request: static("logo.png"),  # dark mode
    },
    "SHOW_LANGUAGES": True,
    # 后台大量页面是只读审计视图，保留历史入口便于追溯人工操作。
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    "LANGUAGES": {
        "navigation": [
            {
                "bidi": False,
                "code": "en",
                "name": "English",
                "name_local": "🇺🇸 English",
                "name_translated": "🇺🇸 English",
            },
	        {
                "bidi": False,
                "code": "ru",
                "name": "Русский",
                "name_local": "🇷🇺 Русский",
                "name_translated": "🇷🇺 Русский",
            },
            {
                "bidi": False,
                "code": "zh-hans",
                "name": "简体中文",
                "name_local": "🇨🇳 简体中文",
                "name_translated": "🇨🇳 简体中文",
            },
        ],
    },
    "SITE_DROPDOWN": [
        {
            "icon": "home",
            "title": "Xcash",
            "link": "https://xca.sh",
            "attrs": {
                "target": "_blank",
            },
        },
        {
            "icon": "docs",
            "title": _("文档"),
            "link": "https://docs.xca.sh",
        },
    ],
    "BORDER_RADIUS": "16px",
    "COLORS": {
        "primary": {
            "50": "239 246 255",
            "100": "219 234 254",
            "200": "191 219 254",
            "300": "147 197 253",
            "400": "96 165 250",
            "500": "37 99 235",
            "600": "29 78 216",
            "700": "30 64 175",
            "800": "30 58 138",
            "900": "23 37 84",
            "950": "15 23 42",
        },
    },
}

SIDEBAR_UNFOLD = {
    "SIDEBAR": {
        "navigation": [
            {
                # 所有后台用户都统一从总览进入，避免继续维护双后台心智模型。
                "title": _("系统"),
                "collapsible": False,
                "items": [
                    {
                        "title": _("经营看板"),
                        "icon": "insert_chart",
                        "link": reverse_lazy("admin:index"),
                    },
                    {
                        "title": _("异常巡检"),
                        "icon": "health_and_safety",
                        "link": reverse_lazy("operational-inspection"),
                        "badge": "core.dashboard.operational_inspection_sidebar_badge",
                        "badge_variant": "danger",
                        "badge_style": "solid",
                        "permission": "core.dashboard.has_operational_inspection_risk",
                    },
                    {
                        "title": _("异常巡检"),
                        "icon": "health_and_safety",
                        "link": reverse_lazy("operational-inspection"),
                        "permission": "core.dashboard.has_no_operational_inspection_risk",
                    },
                    {
                        "title": _("系统钱包"),
                        "icon": "account_balance_wallet",
                        "link": reverse_lazy("admin:core_systemwallet_changelist"),
                    },
                    {
                        "title": _("运行参数"),
                        "icon": "tune",
                        "link": reverse_lazy("admin:core_systemsettings_changelist"),
                    },
                ],
            },
            {
                "title": _("项目"),
                "collapsible": False,
                "items": [
                    {
                        "title": _("项目列表"),
                        "icon": "widgets",
                        "link": reverse_lazy("admin:projects_project_changelist"),
                    },
                    {
                        "title": _("客户"),
                        "icon": "group",
                        "link": reverse_lazy("admin:projects_customer_changelist"),
                    },
                    {
                        "title": _("钱包直收地址"),
                        "icon": "alternate_email",
                        "link": reverse_lazy(
                            "admin:invoices_differrecipientaddress_changelist"
                        ),
                    },
                ],
            },
            {
                "title": _("账单"),
                "collapsible": False,
                "items": [
                    {
                        "title": _("账单记录"),
                        "icon": "receipt",
                        "link": reverse_lazy("admin:invoices_invoice_changelist"),
                    },
                    {
                        "title": _("账单合约"),
                        "icon": "contract",
                        "link": reverse_lazy(
                            "admin:chains_invoicevaultslot_changelist"
                        ),
                    },
                ],
            },
            {
                "title": _("充值"),
                "collapsible": False,
                "items": [
                    {
                        "title": _("充值记录"),
                        "icon": "download",
                        "link": reverse_lazy("admin:deposits_deposit_changelist"),
                    },
                    {
                        "title": _("充值合约"),
                        "icon": "contract",
                        "link": reverse_lazy(
                            "admin:chains_depositvaultslot_changelist"
                        ),
                    },
                ],
            },
            {
                "title": _("通知"),
                "collapsible": False,
                "items": [
                    {
                        "title": _("通知事件"),
                        "icon": "notifications_active",
                        "link": reverse_lazy("admin:webhooks_webhookevent_changelist"),
                    },
                    {
                        "title": _("投递日志"),
                        "icon": "send",
                        "link": reverse_lazy(
                            "admin:webhooks_deliveryattempt_changelist"
                        ),
                    },
                ],
            },
            {
                "title": _("区块链"),
                "collapsible": False,
                "items": [
                    {
                        "title": _("公链"),
                        "icon": "memory",
                        "link": reverse_lazy("admin:chains_chain_changelist"),
                    },
                    {
                        "title": _("链上转账"),
                        "icon": "sync_alt",
                        "link": reverse_lazy("admin:chains_transfer_changelist"),
                    },
                    {
                        "title": _("上链任务"),
                        "icon": "bolt",
                        "link": reverse_lazy("admin:chains_txtask_changelist"),
                    },
                    {
                        "title": _("归集计划"),
                        "icon": "move_down",
                        "link": reverse_lazy(
                            "admin:chains_vaultslotcollectschedule_changelist"
                        ),
                    },
                ],
            },
            {
                # 链适配层：只有排障时才需要下钻到具体链的任务与游标，默认折叠。
                "title": _("链适配"),
                "collapsible": True,
                "items": [
                    {
                        "title": _("EVM 上链任务"),
                        "icon": "bolt",
                        "link": reverse_lazy("admin:evm_evmtxtask_changelist"),
                    },
                    {
                        "title": _("EVM 扫描游标"),
                        "icon": "radar",
                        "link": reverse_lazy("admin:evm_evmscancursor_changelist"),
                    },
                    {
                        "title": _("Tron 上链任务"),
                        "icon": "bolt",
                        "link": reverse_lazy("admin:tron_trontxtask_changelist"),
                    },
                    {
                        "title": _("Tron 扫描游标"),
                        "icon": "radar",
                        "link": reverse_lazy("admin:tron_tronwatchcursor_changelist"),
                    },
                    {
                        "title": _("钱包"),
                        "icon": "wallet",
                        "link": reverse_lazy("admin:chains_wallet_changelist"),
                    },
                    {
                        "title": _("派生地址"),
                        "icon": "key",
                        "link": reverse_lazy("admin:chains_address_changelist"),
                    },
                ],
            },
            {
                "title": _("货币"),
                "collapsible": True,
                "items": [
                    {
                        "title": _("加密货币"),
                        "icon": "currency_bitcoin",
                        "link": reverse_lazy("admin:currencies_crypto_changelist"),
                    },
                    {
                        "title": _("法定货币"),
                        "icon": "attach_money",
                        "link": reverse_lazy("admin:currencies_fiat_changelist"),
                    },
                ],
            },
            {
                "title": _("风控"),
                "collapsible": True,
                "items": [
                    {
                        "title": _("风险评估"),
                        "icon": "shield",
                        "link": reverse_lazy("admin:aml_riskassessment_changelist"),
                    },
                ],
            },
            {
                "title": _("运维"),
                "collapsible": True,
                "items": [
                    {
                        "title": _("任务日志"),
                        "icon": "task",
                        "link": reverse_lazy(
                            "admin:django_celery_results_taskresult_changelist",
                        ),
                    },
                    {
                        "title": _("后台用户"),
                        "icon": "manage_accounts",
                        "link": reverse_lazy("admin:users_user_changelist"),
                    },
                    {
                        "title": _("权限组"),
                        "icon": "groups",
                        "link": reverse_lazy("admin:auth_group_changelist"),
                    },
                    {
                        "title": _("API Token"),
                        "icon": "vpn_key",
                        "link": reverse_lazy("admin:authtoken_tokenproxy_changelist"),
                    },
                ],
            },
        ],
    },
}

UNFOLD = {**BASE_UNFOLD, **SIDEBAR_UNFOLD}
