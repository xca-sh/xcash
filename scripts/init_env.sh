#!/bin/sh
set -eu

# 项目根目录即本脚本（scripts/）的上级。不再依赖 .env.example 模板：
# 所有变量与默认值由本脚本内嵌，缺失密钥自动随机填充。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$PROJECT_DIR/.env"

if [ -e "$ENV_FILE" ] || [ -L "$ENV_FILE" ]; then
    {
        echo "错误：$ENV_FILE 已存在，拒绝覆盖。"
        echo "如需重新生成，请先手动备份并删除现有 .env。"
    } >&2
    exit 1
fi

# 生成指定长度的随机字符串（a-zA-Z0-9）
generate_secret() {
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$1"
}

# 先把所有值解析到变量（单一真值源），再写文件。
SITE_DOMAIN="pay.example.com"
LISTEN_TO="127.0.0.1"
DJANGO_SECRET_KEY=$(generate_secret 64)
# 后台超管口令必须随机：固定默认值在开源仓库里等同公开常量，且后台在未设置
# ADMIN_PATH 时就挂在站点根路径，扫到即可直接登录。
DJANGO_DEFAULT_SUPERUSER_PASSWORD=$(generate_secret 24)
PERFORMANCE="low"
POSTGRES_PASSWORD=$(generate_secret 32)
# 自带 Caddy 与 django 同处 Docker 网络，其对端地址落在该私有段内。声明为受信代理，
# 后端才会采信 Caddy 覆写的 X-Real-IP，商户 IP 白名单与 IP 限流才能拿到真实来源 IP。
TRUSTED_PROXY_IPS="172.16.0.0/12"
# 钱包助记词加密密钥：地址派生与签名已在主系统内部闭环，密钥随主应用一起加载。
WALLET_MNEMONIC_ENCRYPTION_KEY=$(generate_secret 64)

# 主环境文件 .env：主应用容器（django/worker/worker-scan/beat）的 env_file + docker compose 插值 + 本地 dev 共用。
# 已存在时脚本已在入口拒绝执行，避免覆盖 WALLET_MNEMONIC_ENCRYPTION_KEY 等关键密钥。
cat > "$ENV_FILE" <<EOF
# Xcash 主环境变量
# 用途：主应用容器（django/worker/worker-scan/beat）的 env_file + docker compose 解析期插值 + 本地 dev。
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

# 可选：自定义后台路径。不设置时后台仍挂在站点根路径，并在后台右上角显示安全提醒。
# ADMIN_PATH=secure-admin

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

# 口令是随机生成的，只有这一次机会记录；.env 本身也保存了一份。
echo ""
echo "================ 后台管理员账号 ================"
echo "  用户名: admin"
echo "  密　码: ${DJANGO_DEFAULT_SUPERUSER_PASSWORD}"
echo "  请立即保存到密码管理器，并在首次登录后修改。"
echo "==============================================="
echo ""

echo "完成。主应用容器加载 .env。"
