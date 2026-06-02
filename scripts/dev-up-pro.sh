#!/bin/bash
# dev-up-pro.sh — 以生产级方式在本地拉起所有服务，适用于压测场景。
# Django 用 gunicorn，Celery worker 不带 watchfiles 热重载。

set -o errexit
set -o nounset
set -o pipefail

ENV_FILE="${ENV_FILE:-.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
child_pids=()

# ── 环境变量 ──────────────────────────────────────────────────
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
fi

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.dev}"
export POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export REDIS_HOST="${REDIS_HOST:-127.0.0.1}"
export REDIS_PORT="${REDIS_PORT:-6379}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-postgres}"

# ── 并发数配置（可通过环境变量覆盖） ─────────────────────────
GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
CELERY_BUSINESS_WORKER_CONCURRENCY="${CELERY_BUSINESS_WORKER_CONCURRENCY:-16}"
CELERY_STRESS_WORKER_CONCURRENCY="${CELERY_STRESS_WORKER_CONCURRENCY:-16}"
CELERY_SCAN_WORKER_CONCURRENCY="${CELERY_SCAN_WORKER_CONCURRENCY:-4}"

# ── 生命周期管理 ──────────────────────────────────────────────
cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM

  if ((${#child_pids[@]} > 0)); then
    for pid in "${child_pids[@]}"; do
      if kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
      fi
    done
    wait "${child_pids[@]}" 2>/dev/null || true
  fi

  # 清理 beat 调度文件
  rm -f /tmp/xcash-celerybeat-schedule{,-shm,-wal}

  exit "${exit_code}"
}

wait_for_first_exit() {
  while true; do
    for pid in "${child_pids[@]}"; do
      if ! kill -0 "${pid}" 2>/dev/null; then
        wait "${pid}"
        return $?
      fi
    done
    sleep 1
  done
}

trap cleanup EXIT INT TERM

# ── 清理 beat 历史调度状态 ────────────────────────────────────
rm -f /tmp/xcash-celerybeat-schedule{,-shm,-wal}

echo "=== dev-up-pro: 生产级本地启动 ==="
echo "  gunicorn workers : ${GUNICORN_WORKERS}"
echo "  celery business  : ${CELERY_BUSINESS_WORKER_CONCURRENCY} threads"
echo "  celery stress    : ${CELERY_STRESS_WORKER_CONCURRENCY} threads"
echo "  celery scan      : ${CELERY_SCAN_WORKER_CONCURRENCY} threads"
echo ""

# ── 1. Gunicorn (替代 runserver) ──────────────────────────────
uv run gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${GUNICORN_WORKERS}" \
  --threads 4 \
  --worker-class gthread \
  --timeout 120 \
  --access-logfile - \
  --error-logfile - \
  --log-level info &
child_pids+=("$!")

# ── 3. Celery Worker — 业务队列 ──────────────────────────────
uv run celery -A config.celery worker \
  -l INFO \
  --pool=threads \
  --concurrency="${CELERY_BUSINESS_WORKER_CONCURRENCY}" \
  -Q celery \
  -n business@%h &
child_pids+=("$!")

# ── 4. Celery Worker — 压测队列 ──────────────────────────────
uv run celery -A config.celery worker \
  -l INFO \
  --pool=threads \
  --concurrency="${CELERY_STRESS_WORKER_CONCURRENCY}" \
  -Q stress \
  -n stress@%h &
child_pids+=("$!")

# ── 5. Celery Worker — 扫描队列 ──────────────────────────────
uv run celery -A config.celery worker \
  -l INFO \
  --pool=threads \
  --concurrency="${CELERY_SCAN_WORKER_CONCURRENCY}" \
  -Q scan \
  -n scan@%h &
child_pids+=("$!")

# ── 6. Celery Beat ───────────────────────────────────────────
uv run celery -A config.celery beat \
  -l INFO \
  -s /tmp/xcash-celerybeat-schedule &
child_pids+=("$!")

echo "=== 所有服务已启动 (PIDs: ${child_pids[*]}) ==="
wait_for_first_exit
