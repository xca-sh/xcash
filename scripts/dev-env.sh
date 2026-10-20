#!/bin/bash
# 开发脚本共用的环境引导：加载 .env 并补齐宿主机本地默认值。
# 由各 dev-*.sh 通过 source 引入，不单独执行。
#
# 这里刻意不设置 errexit/nounset/pipefail：source 进来的脚本改变调用方的
# shell 选项容易产生意外，交由各调用脚本自己声明。

ENV_FILE="${ENV_FILE:-.env}"

if [[ -f "${ENV_FILE}" ]]; then
  # 本地开发统一从 .env 注入环境，避免宿主机启动 Django / worker 时手工 export。
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

# 开发脚本统一默认指向 dev settings，避免继续引用历史 local 命名。
export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.dev}"
export POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6379}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"
