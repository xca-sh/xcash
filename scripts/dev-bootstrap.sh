#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

ENV_FILE="${ENV_FILE:-.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 地址派生与签名已在主系统内部闭环，无独立 signer 服务；
# 这里只初始化主库与本地联调链配置。
ENV_FILE="${ENV_FILE}" "${SCRIPT_DIR}/dev-manage.sh" migrate
ENV_FILE="${ENV_FILE}" "${SCRIPT_DIR}/dev-manage.sh" init_local_chains
