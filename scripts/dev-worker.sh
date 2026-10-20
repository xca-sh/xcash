#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# shellcheck source=scripts/dev-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"

export WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-16}"

# 业务任务 worker：执行 PG 队列里的任务并按 cron 投递周期任务（不再需要 beat）；
# 链扫描与主动交易由 dev-chain-runner.sh 的常驻进程负责。
exec uv run watchfiles --filter python "python manage.py run_worker --concurrency ${WORKER_CONCURRENCY}"
