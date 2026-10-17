#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# shellcheck source=scripts/dev-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"

export CELERY_SCAN_WORKER_CONCURRENCY="${CELERY_SCAN_WORKER_CONCURRENCY:-2}"

# 扫描任务独占 worker，避免被高并发业务任务挤占后出现游标假卡住。
exec uv run watchfiles --filter python celery.__main__.main --args "-A config.celery worker -l INFO --pool=threads --concurrency=${CELERY_SCAN_WORKER_CONCURRENCY} -Q scan"
