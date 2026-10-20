#!/bin/bash
# dev-up-pro.sh — 以生产级方式在本地拉起所有服务，适用于压测场景。
# Django 用 gunicorn，worker 与 chain-runner 不带 watchfiles 热重载。

set -o errexit
set -o nounset
set -o pipefail

ENV_FILE="${ENV_FILE:-.env}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
child_pids=()

# ── 环境变量 ──────────────────────────────────────────────────
# shellcheck source=scripts/dev-env.sh
source "${SCRIPT_DIR}/dev-env.sh"

# ── 并发数配置（可通过环境变量覆盖） ─────────────────────────
GUNICORN_WORKERS="${GUNICORN_WORKERS:-4}"
WORKER_CONCURRENCY="${WORKER_CONCURRENCY:-16}"

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

echo "=== dev-up-pro: 生产级本地启动 ==="
echo "  gunicorn workers : ${GUNICORN_WORKERS}"
echo "  worker           : ${WORKER_CONCURRENCY} threads"
echo "  chain-runner     : 每条活跃链一个线程"
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

# ── 2. worker — 业务任务与周期任务投递 ───────────────────────
uv run python manage.py run_worker --concurrency "${WORKER_CONCURRENCY}" &
child_pids+=("$!")

# ── 3. chain-runner — 按链常驻扫描与主动交易 ─────────────────
uv run python manage.py run_chains &
child_pids+=("$!")

echo "=== 所有服务已启动 (PIDs: ${child_pids[*]}) ==="
wait_for_first_exit
