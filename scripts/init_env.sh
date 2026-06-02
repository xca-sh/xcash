#!/bin/sh
set -eu

# 项目根目录即本脚本（scripts/）的上级。不再依赖 .env.example 模板：
# 所有变量与默认值由本脚本内嵌，缺失密钥自动随机填充。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

# 生成指定长度的随机字符串（a-zA-Z0-9）
generate_secret() {
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$1"
}

# 取某个键的值：优先复用 .env 中的已有值（避免重复生成导致改掉已在用的密钥）；
# 没有则用传入的默认值。
val_of() {
    key="$1"
    default="$2"
    if [ -f "$ENV_FILE" ]; then
        line=$(grep -E "^${key}=" "$ENV_FILE" | head -n 1 || true)
        if [ -n "$line" ]; then
            printf '%s' "${line#*=}"
            return 0
        fi
    fi
    printf '%s' "$default"
}

# 先把所有值解析到变量（单一真值源），再写文件。
SITE_DOMAIN=$(val_of SITE_DOMAIN "pay.example.com")
LISTEN_TO=$(val_of LISTEN_TO "127.0.0.1")
DJANGO_SECRET_KEY=$(val_of DJANGO_SECRET_KEY "$(generate_secret 64)")
DJANGO_DEFAULT_SUPERUSER_PASSWORD=$(val_of DJANGO_DEFAULT_SUPERUSER_PASSWORD "Admin@123456")
PERFORMANCE=$(val_of PERFORMANCE "low")
POSTGRES_PASSWORD=$(val_of POSTGRES_PASSWORD "$(generate_secret 32)")
TRUSTED_PROXY_IPS=$(val_of TRUSTED_PROXY_IPS "")
# 钱包助记词加密密钥：地址派生与签名已在主系统内部闭环，密钥随主应用一起加载。
WALLET_MNEMONIC_ENCRYPTION_KEY=$(val_of WALLET_MNEMONIC_ENCRYPTION_KEY "$(generate_secret 64)")

# 主环境文件 .env：主应用容器（django/worker/beat）的 env_file + docker compose 插值 + 本地 dev 共用。
# 已存在则不覆盖，并作为密钥复用源（确保 WALLET_MNEMONIC_ENCRYPTION_KEY 等关键密钥稳定不变）。
if [ ! -f "$ENV_FILE" ]; then
    cat > "$ENV_FILE" <<EOF
# Xcash 主环境变量
# 用途：主应用容器（django/worker/beat）的 env_file + docker compose 解析期插值 + 本地 dev。
# 由 scripts/init_env.sh 生成，缺失密钥自动随机填充。请妥善保管并备份，切勿提交版本库。
#
# >>> WALLET_MNEMONIC_ENCRYPTION_KEY 生成后【严禁修改】 <<<
# 该密钥用于加/解密所有钱包助记词。一旦更改，数据库中已加密的助记词将永久无法解密、
# 热钱包私钥彻底丢失、归集能力全部失效，且不可恢复。请按最高密级离线备份本文件。

# 域名访问
SITE_DOMAIN=${SITE_DOMAIN}
# 监听地址，默认仅本机
LISTEN_TO=${LISTEN_TO}

# Django 主应用密钥（容器重建后需稳定，避免签名状态漂移）
DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
# 首次部署且库内无管理员时自动创建后台账号（用户名固定 admin）
DJANGO_DEFAULT_SUPERUSER_PASSWORD=${DJANGO_DEFAULT_SUPERUSER_PASSWORD}

# 性能档位：low=1c2g，middle=4c8g，high=8c16g；不设置默认 low
PERFORMANCE=${PERFORMANCE}

# 主应用数据库口令
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# 钱包助记词加密密钥（生成后严禁修改，泄露或更改即等同种子失守）
WALLET_MNEMONIC_ENCRYPTION_KEY=${WALLET_MNEMONIC_ENCRYPTION_KEY}

# 可选增强
TRUSTED_PROXY_IPS=${TRUSTED_PROXY_IPS}
EOF
    echo "已生成 .env（主应用 + compose 插值 + dev）。"
else
    echo ".env 已存在，保留并作为密钥复用源。"
fi

echo "完成。主应用容器加载 .env。"
