# ruff: noqa: ERA001, E501
"""Base settings to build other settings files upon."""

import sys
from pathlib import Path

import environ
import structlog

BASE_DIR = Path(__file__).resolve(strict=True).parent.parent.parent

env = environ.Env()

# Xcash/
APPS_DIR = BASE_DIR / "xcash"
sys.path.append(str(APPS_DIR))

from common.logger import configure_structlog  # noqa: E402
from common.logger import shared_processors  # noqa: E402
from config.admin_path import admin_route_prefix  # noqa: E402
from config.admin_path import normalize_admin_path  # noqa: E402

configure_structlog()

# Redis
# ------------------------------------------------------------------------------
# 修复：支持“依赖容器化、应用宿主机运行”的开发模式，避免 Redis 地址被硬编码为容器服务名。
REDIS_HOST = env.str("REDIS_HOST", default="redis")
REDIS_PORT = env.int("REDIS_PORT", default=6379)
REDIS_DB = env.int("REDIS_DB", default=0)
REDIS_URL = env.str(
    "REDIS_URL", default=f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
)
# Redis 只承担缓存、限流与签名 nonce，任务队列在 PostgreSQL（Procrastinate）。
# 限流键在 REDIS_DB（默认 0），缓存独占 REDIS_CACHE_DB（默认 1）。REDIS_URL 另供
# migrate_celery_messages 读取旧版本留在 Redis 里的 Celery 在途消息。
# Redis 的 maxmemory 淘汰策略是实例级、无法按库隔离，实例侧统一配 volatile-lru：
# 只淘汰带 TTL 的临时缓存键，timeout=None 的配置型缓存（watch set / runtime settings /
# 权限）天然豁免，永不被淘汰。
REDIS_CACHE_DB = env.int("REDIS_CACHE_DB", default=1)
REDIS_CACHE_URL = env.str(
    "REDIS_CACHE_URL", default=f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_CACHE_DB}"
)

# SaaS API
# ------------------------------------------------------------------------------
SAAS_API_TOKEN = env.str("SAAS_API_TOKEN", default="")
# 运行模式标志：True = SaaS 附属引擎，False = 自托管独立部署。
# 默认按 SAAS_API_TOKEN 是否存在自动推导，也可通过环境变量显式覆盖。
IS_SAAS = env.bool("IS_SAAS", default=bool(SAAS_API_TOKEN))
# 只填 SaaS 的 scheme+host，/callbacks/xcash 路径由 saas_callback 自己拼
# 同机部署约定：SaaS 的 Caddy 在 xcash_public 上暴露 xcash-saas-caddy 这个 DNS 别名，双方按此别名互通。
# 不用容器原名 xcash_saas_caddy，下划线违反 RFC 1034/1035，Django HTTP_HOST 校验会拒。
# 空串 = 关闭回调推送
SAAS_CALLBACK_URL = env.str("SAAS_CALLBACK_URL", default="http://xcash-saas-caddy")

# https://docs.djangoproject.com/en/dev/ref/settings/#allowed-hosts
# CORS 默认关闭全放行，基础白名单允许官网来源；各环境可继续追加自身域名。
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = ["https://xca.sh"]

DEFAULT_SUPERUSER_USERNAME = "admin"
# 该默认值只服务于本地开发。生产（DEBUG=False）下 ensure_default_superuser 会拒绝
# 用它建号：后台默认挂在站点根路径，固定口令又是开源仓库里的公开常量。
INSECURE_DEFAULT_SUPERUSER_PASSWORD = "Admin@123456"  # noqa: S105
DEFAULT_SUPERUSER_PASSWORD = env.str(
    "DJANGO_DEFAULT_SUPERUSER_PASSWORD",
    default=INSECURE_DEFAULT_SUPERUSER_PASSWORD,
)

# 钱包助记词静态加密
# ------------------------------------------------------------------------------
# 地址派生与 EVM 签名在主系统内部闭环完成（见 chains/keys.py）；助记词以 AES-256-GCM
# 加密入库，密钥取自该高熵主密钥。生产环境必须显式配置（chains.checks 在 check 阶段拦截缺失）。
WALLET_MNEMONIC_ENCRYPTION_KEY = env.str("WALLET_MNEMONIC_ENCRYPTION_KEY", default="")
TRON_RPC_TIMEOUT = 8.0
# Energy 是合约调用的主要成本，广播前必须能证明估算值乘以安全边际后可被覆盖；
# Bandwidth 不足时允许按 Tron 规则燃烧 TRX，但 Energy 仍不走本地余额兜底。
TRON_RESOURCE_SAFETY_MARGIN_BPS = env.int(
    "TRON_RESOURCE_SAFETY_MARGIN_BPS",
    default=12_000,
)
# 签名后用 JSON 载荷长度保守估算带宽成本，再额外预留固定字节。
TRON_BANDWIDTH_SAFETY_BYTES = env.int("TRON_BANDWIDTH_SAFETY_BYTES", default=512)

# 只有当 TCP 对端本身属于受信代理网段时，才接受其转发的 X-Real-IP。
# 默认留空，生产环境必须显式配置，例如 127.0.0.1、::1 或反向代理容器网段。
TRUSTED_PROXY_IPS = env.list("TRUSTED_PROXY_IPS", default=[])

# Webhook
# ------------------------------------------------------------------------------
# 投递目标默认拒绝 http / localhost / 私有网段，反 SSRF。
# 仅开发环境需要给本地回调（如指向 localhost 的商户 webhook）放行时打开。
WEBHOOK_ALLOW_INTERNAL_TARGETS = env.bool(
    "WEBHOOK_ALLOW_INTERNAL_TARGETS", default=False
)

# Rate Limit
RATELIMIT_BACKEND = "redis"
RATELIMIT_REDIS = {
    "host": REDIS_HOST,
    "port": REDIS_PORT,
    "db": REDIS_DB,
}
# django-smart-ratelimit 的取 IP 策略必须与 TRUSTED_PROXY_IPS 对齐，否则它会退回
# "信任 CF-Connecting-IP / X-Forwarded-For 最左项" 的兼容分支，登录等 IP 限流可被
# 任意伪造请求头绕过。TRUSTED_PROXY_IPS 为空列表时该库判定为未配置，因此还需要
# 显式关闭转发头信任，让其直接使用 REMOTE_ADDR。
RATELIMIT_TRUSTED_PROXIES = TRUSTED_PROXY_IPS
RATELIMIT_TRUST_FORWARDED_HEADERS = False
# EPay V1 /epay/submit.php 入口的 IP 维度限流。
# 该入口未经鉴权即触发 EpayMerchant.objects.get + serializer + 签名计算，
# 攻击者可用有效 pid + 错误 sign 大量探测形成 DB 查询型 DoS。
# 单独抽出来作为 setting，便于测试通过 override_settings 调阈值。
EPAY_SUBMIT_RATE_LIMIT = "60/m"

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = env.bool("DJANGO_DEBUG", False)
# Local time zone. Choices are
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# though not all of them may be available with every OS.
# In Windows, this must be set to your system time zone.
TIME_ZONE = "UTC"
# https://docs.djangoproject.com/en/dev/ref/settings/#language-code
LANGUAGE_CODE = "zh-hans"
# https://docs.djangoproject.com/en/dev/ref/settings/#languages
# from django.utils.translation import gettext_lazy as _
# 标准支持语言，顺序固定：中文、英语、俄语（见 CLAUDE.md「多语言要求」）。
LANGUAGES = [
    ("zh-hans", "🇨🇳 简体中文"),
    ("en", "🇺🇸 English"),
    ("ru", "🇷🇺 Русский"),
]
# https://docs.djangoproject.com/en/dev/ref/settings/#use-i18n
USE_I18N = True
# https://docs.djangoproject.com/en/dev/ref/settings/#use-tz
USE_TZ = True
# https://docs.djangoproject.com/en/dev/ref/settings/#locale-paths
LOCALE_PATHS = [
    str(BASE_DIR / "locale"),
    # 第三方后台组件（django-unfold）自身不带 locale，其界面文案的语言包由项目维护。
    # 单独放一个目录：makemessages 只写 locale/，写进去会在下次生成时被标记废弃。
    str(BASE_DIR / "locale_vendor"),
]

# DATABASES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#databases
if "DATABASE_URL" in env and not env.bool("XCASH_IGNORE_DATABASE_URL", default=False):
    default_db = env.db("DATABASE_URL")
else:
    default_db = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "xcash",
        "USER": "postgres",
        "PASSWORD": env.str("POSTGRES_PASSWORD"),
        "HOST": env.str("POSTGRES_HOST", default="db"),
        "PORT": env.int("POSTGRES_PORT", default=5432),
    }

DATABASES = {
    "default": default_db,
}
DATABASES["default"]["ATOMIC_REQUESTS"] = True
DATABASES["default"]["CONN_MAX_AGE"] = 60
# 持久连接健康检查：复用前探活，连接被干净关闭时透明重连，避免拿到死连接。
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
# libpq TCP keepalive + 连接超时：闲置期每 30s 发探针，既维持 NAT/conntrack 状态
# 不被回收，也能在数十秒内判定对端已死并快速失败，而非干等内核 TCP 重传超时
# （表现为闲置后首个请求卡十几秒）。用 setdefault 合并，保留 DATABASE_URL 解析出的 OPTIONS。
DATABASES["default"].setdefault("OPTIONS", {}).update(
    {
        "connect_timeout": 5,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
)
# https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-DEFAULT_AUTO_FIELD
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# URLS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#root-urlconf
ROOT_URLCONF = "config.urls"
# https://docs.djangoproject.com/en/dev/ref/settings/#wsgi-application
WSGI_APPLICATION = "config.wsgi.application"
# 统一无尾斜杠 URL 规范，避免 POST 对带斜杠 URL 自动 301 变 404 或丢 body。
APPEND_SLASH = False

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "unfold",
    "unfold.contrib.filters",
    "unfold.contrib.forms",
    "unfold.contrib.inlines",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]
THIRD_PARTY_APPS = [
    # PostgreSQL 任务队列。须排在本地 app 之前：core.admin 要替换它自带的任务后台。
    "procrastinate.contrib.django",
    "rest_framework",
    "rest_framework.authtoken",
    "corsheaders",
]

LOCAL_APPS = [
    "chains",
    "core",
    "users",
    "projects",
    "currencies",
    "invoices",
    "deposits",
    "webhooks",
    "aml",
    # EVM 链相关模型（EvmOnchainTask）
    "evm",
    # Tron 监听、扫描游标与 provider 接入
    "tron",
    "saas_api",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# AUTHENTICATION
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-user-model
AUTH_USER_MODEL = "users.User"

AUTHENTICATION_BACKENDS = ("django.contrib.auth.backends.ModelBackend",)

# Session Settings
SESSION_EXPIRE_AT_BROWSER_CLOSE = True  # False表示关闭浏览器后session仍然有效
SESSION_SAVE_EVERY_REQUEST = True  # 每次请求都更新session的过期时间
SESSION_COOKIE_AGE = 3600 * 48  # 会话过期时间（秒）

# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#password-hashers
PASSWORD_HASHERS = [
    # https://docs.djangoproject.com/en/dev/topics/auth/passwords/#using-argon2-with-django
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# MIDDLEWARE
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#middleware
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # 后台 session 超时由 SystemSettings 动态控制，每次请求刷新过期时间。
    "common.middlewares.AdminSessionTimeoutMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "common.middlewares.ExceptionMiddleware",
    # 商户 /v1 API 准入：项目状态 → IP 白名单 → HMAC 签名与 nonce。
    "common.middlewares.XcashMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#static-root
STATIC_ROOT = str(BASE_DIR / "staticfiles")
# https://docs.djangoproject.com/en/dev/ref/settings/#static-url
STATIC_URL = "/static/"
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#std:setting-STATICFILES_DIRS
STATICFILES_DIRS = [
    str(APPS_DIR / "static"),
]

# TEMPLATES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#templates
TEMPLATES = [
    {
        # https://docs.djangoproject.com/en/dev/ref/settings/#std:setting-TEMPLATES-BACKEND
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # https://docs.djangoproject.com/en/dev/ref/settings/#dirs
        "DIRS": [str(APPS_DIR / "templates")],
        # https://docs.djangoproject.com/en/dev/ref/settings/#app-dirs
        "APP_DIRS": True,
        "OPTIONS": {
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-context-processors
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# FIXTURES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#fixture-dirs
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# SECURITY
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#session-cookie-httponly
SESSION_COOKIE_HTTPONLY = True

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL.
# https://docs.djangoproject.com/en/dev/ref/settings/#admins
ADMIN_PATH = normalize_admin_path(env.str("ADMIN_PATH", default=""))
ADMIN_ROUTE_PREFIX = admin_route_prefix(ADMIN_PATH)
ADMIN_PATH_CONFIGURED = bool(ADMIN_PATH)
# 不配置 ADMINS / MANAGERS：本项目没有任何 EMAIL_BACKEND，Django 的 mail_admins 与
# AdminEmailHandler 均未启用，配了也发不出去；且开源仓库里写死某个私人邮箱，会让
# 每个自托管部署的错误报告都默认指向该地址。错误统一走 structlog JSON 打到 stdout，
# 由部署方的日志采集链路接管。

# CACHES
# ------------------------------------------------------------------------------
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            # django-redis 默认【不设任何 socket 超时】：Redis 进程假死或网络黑洞时，
            # 缓存调用会永久阻塞在 recv 上，把 gunicorn 的 gthread 线程逐个耗尽，
            # 表现为整站挂起而非快速失败。缓存操作本身是亚毫秒级的，5s 已极宽裕。
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
        },
    },
}

# LOGGING
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            "()": structlog.stdlib.ProcessorFormatter,
            "processors": [
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(),
            ],
            "foreign_pre_chain": shared_processors,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
        },
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    "loggers": {
        "django.security.DisallowedHost": {"level": "ERROR", "handlers": ["console"]},
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "httpx": {"level": "WARNING"},
        # 每个任务的 Starting/Success 以及「失败待重试」都记 INFO（后者还带 traceback），
        # 周期任务一天数万条；只保留 WARNING 以上，最终失败的 job_error 仍是 ERROR。
        "procrastinate": {"level": "WARNING"},
    },
}

# Procrastinate（PostgreSQL 任务队列）
# ------------------------------------------------------------------------------
# 各 app 的 tasks 模块自动发现；不在 tasks 模块里的任务与周期登记在这里显式列出，
# 否则 worker 不认识这些任务名，任务会以 TaskNotFound 落为 failed。
PROCRASTINATE_IMPORT_PATHS = [
    "config.periodic_jobs",
    "common.saas_callback",
    "evm.saas_gas_billing",
    "tron.saas_gas_billing",
]

# -------------------------------------------------------------------------------
# django-rest-framework - https://www.django-rest-framework.org/api-guide/settings/
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_THROTTLE_CLASSES": [
        # 不用 DRF 内置的 AnonRateThrottle：其默认取 IP 逻辑会把整条
        # X-Forwarded-For 当作限流身份，调用方换个头即换一个桶。
        "common.throttles.TrustedProxyAnonRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": "256/minute",
        "invoice_retrieve": "120/minute",
        "invoice_select_method": "10/minute",
        "vault_slot": "60/minute",
    },
    # 统一分页策略：GenericAPIView 及其子类（ModelViewSet/GenericViewSet 等）默认启用
    # page/size 分页，响应形如 {count, next, previous, results}。
    # 小而固定的参考数据（currencies/chains）在 ViewSet 内显式 pagination_class = None 排除。
    "DEFAULT_PAGINATION_CLASS": "common.pagination.PageNumberSizePagination",
    "PAGE_SIZE": 20,
}

# Your stuff...
# ------------------------------------------------------------------------------
from config.settings.unfold import UNFOLD  # noqa
