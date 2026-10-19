#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# shellcheck source=scripts/dev-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"

# 常驻按链循环进程：扫描与主动交易（见 xcash/chains/runner.py），Python 文件变动时自动重启。
exec uv run watchfiles --filter python "python manage.py run_chains"
