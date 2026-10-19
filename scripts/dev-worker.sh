#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# shellcheck source=scripts/dev-env.sh
source "$(dirname "${BASH_SOURCE[0]}")/dev-env.sh"

export CELERY_BUSINESS_WORKER_CONCURRENCY="${CELERY_BUSINESS_WORKER_CONCURRENCY:-2}"

# 业务队列只消费 celery；链扫描由 dev-chain-runner.sh 的常驻进程负责。
exec uv run watchfiles --filter python celery.__main__.main --args "-A config.celery worker -l INFO --pool=threads --concurrency=${CELERY_BUSINESS_WORKER_CONCURRENCY} -Q celery"
