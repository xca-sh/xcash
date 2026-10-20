# ruff: noqa: E501
import socket
from copy import deepcopy

from .base import *  # noqa: F403
from .base import INSTALLED_APPS
from .base import MIDDLEWARE
from .base import REST_FRAMEWORK as BASE_REST_FRAMEWORK
from .base import env

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = True
# https://docs.djangoproject.com/en/dev/ref/settings/#secret-key
# 开发环境使用固定 SECRET_KEY，避免每次重启后 session 失效需要重新登录。
SECRET_KEY = env(
    "DJANGO_SECRET_KEY", default="django-insecure-dev-only-key-do-not-use-in-production"
)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False  # False表示关闭浏览器后session仍然有效
ALLOWED_HOSTS = ["*"]
# 本地开发常见是 Nginx/Caddy 以 loopback 反代 Django；默认信任本机代理来源。
TRUSTED_PROXY_IPS = ["127.0.0.1", "::1"]

# django-debug-toolbar
# ------------------------------------------------------------------------------
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#prerequisites
INSTALLED_APPS += ["debug_toolbar", "django_migration_linter"]
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#middleware
MIDDLEWARE += ["debug_toolbar.middleware.DebugToolbarMiddleware"]
# https://django-debug-toolbar.readthedocs.io/en/latest/configuration.html#debug-toolbar-config
DEBUG_TOOLBAR_CONFIG = {
    "DISABLE_PANELS": [
        "debug_toolbar.panels.redirects.RedirectsPanel",
        # Disable profiling panel due to an issue with Python 3.12:
        # https://github.com/jazzband/django-debug-toolbar/issues/1875
        "debug_toolbar.panels.profiling.ProfilingPanel",
    ],
    "SHOW_TEMPLATE_CONTEXT": True,
}
# https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#internal-ips
INTERNAL_IPS = ["127.0.0.1", "10.0.2.2"]

try:
    # 某些本地环境的 hostname 不可反解；开发配置应回退而不是直接阻断 manage.py。
    _hostname, _, _ips = socket.gethostbyname_ex(socket.gethostname())
except (socket.herror, socket.gaierror):
    _ips = []
INTERNAL_IPS += [".".join([*ip.split(".")[:-1], "1"]) for ip in _ips]

# django-migration-linter
# ------------------------------------------------------------------------------
# 保留 migration linter 的风险提示输出；WARNING 只提示不阻断。
# 字段/表删除和字段重命名由业务评审确认，linter 不再阻断对应迁移。
# - exclude_migration_tests 允许已确认的清理和重命名迁移。
# - ignore_initial_migrations=True 放过初始建表迁移中不可避免的 NOT NULL 列。
# - sql_analyser 显式指定 postgresql，避免 lint 时未连 DB 自动探测失败。
MIGRATION_LINTER_OPTIONS = {
    "sql_analyser": "postgresql",
    "exclude_migration_tests": [
        "DROP_COLUMN",
        "RENAME_COLUMN",
        "DROP_TABLE",
    ],
    "ignore_initial_migrations": True,
    "no_cache": True,
}

# Your stuff...
# ------------------------------------------------------------------------------

CSRF_TRUSTED_ORIGINS = []

# 开发环境允许所有跨域请求，生产环境必须通过 CORS_ALLOWED_ORIGINS 配置白名单。
CORS_ALLOW_ALL_ORIGINS = True

# 钱包助记词静态加密
# ------------------------------------------------------------------------------
# 开发环境提供固定默认密钥，保证 fresh init 时无需先手工补环境变量即可生成/派生钱包。
WALLET_MNEMONIC_ENCRYPTION_KEY = env.str(
    "WALLET_MNEMONIC_ENCRYPTION_KEY",
    default="dev-wallet-mnemonic-encryption-key-change-me",
)

# 本地联调的商户 webhook 常指向 localhost，反 SSRF 校验默认会拒；
# 仅在开发环境放行内部目标，生产仍保持 base.py 默认的拒绝策略。
WEBHOOK_ALLOW_INTERNAL_TARGETS = True

# 本地压测会以匿名方式高并发访问公开接口，沿用基础配置的 256/minute 很容易在建单阶段触发 429。
# 仅开发环境放宽匿名限流，生产仍继续使用 base.py 中的保守默认值。
REST_FRAMEWORK = deepcopy(BASE_REST_FRAMEWORK)
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["anon"] = "10000/minute"
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["vault_slot"] = "10000/minute"
