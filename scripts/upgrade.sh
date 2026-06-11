#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# 本脚本落盘的内容（数据库 dump、迁移日志/plan、升级锁）均属运维敏感数据，
# 统一收紧新建文件权限为 600 / 目录 700；已存在的文件与目录不受影响。
umask 077

ENV_FILE="${ENV_FILE:-.env}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-false}"
# 默认在线 dump（不停机生成演练数据，缩短停机窗口、提升升级体验）：dump 期间
# django/worker/beat 继续服务，仅在 production migrate 前才停机。
# 代价：dump 时刻到停机时刻之间的新写入不在备份内，production migrate 失败回滚
# 会丢这段增量。要求"备份即上线前最后状态、回滚零丢失"时显式设
# STOP_BEFORE_REHEARSAL=true 走停机 dump（停机更久）。
STOP_BEFORE_REHEARSAL="${STOP_BEFORE_REHEARSAL:-false}"
UPGRADE_LOCK_FILE="${UPGRADE_LOCK_FILE:-/tmp/xcash-upgrade.lock}"
# 升级前数据库备份目录：production migrate 一旦中途失败，这份 dump 是回滚到升级前
# 状态的唯一依据，必须落在 TMP_DIR 之外、不随脚本退出清理。.gitignore 已忽略 backups/。
BACKUP_DIR="${BACKUP_DIR:-./backups}"
# postgres 就绪等待上限（秒）：避免 DB 起不来时在持有升级锁的情况下无限 hang。
POSTGRES_WAIT_TIMEOUT="${POSTGRES_WAIT_TIMEOUT:-120}"

# cleanup 需要区分失败发生在哪个阶段：
# - production migrate 前：production 库未被触碰，只恢复本脚本停过的旧容器
# - production migrate 中：DB 可能处于中间态，不自动启动业务服务
# - production migrate 后：DB 已到新 schema，后置步骤失败时按新镜像尝试恢复服务
LOCK_ACQUIRED=false
APP_SERVICES_STOP_REQUESTED=false
APP_SERVICES_TO_RESTORE=()
REHEARSAL_IN_PROGRESS=false
PRODUCTION_MIGRATE_STARTED=false
PRODUCTION_MIGRATE_COMPLETED=false
# 升级前备份文件路径，dump 时赋值；cleanup 在 nounset 下引用，故先声明为空。
MAIN_DUMP=""

COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")
REHEARSAL_COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile migration-rehearsal)
TMP_DIR=""

log() {
  printf '[upgrade] %s\n' "$*"
}

die() {
  printf '[upgrade] ERROR: %s\n' "$*" >&2
  exit 1
}

cleanup() {
  local exit_code=$?

  # 失败退出时把 TMP_DIR 下所有 *.log 一次性回放到 stderr，
  # 避免长流程中真正的失败原因被后续输出冲掉。
  # plan/.norm 等结构化数据不在回放范围（无人类可读价值，且 die 之前已经 diff 过）。
  if [[ "${exit_code}" -ne 0 && -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
    shopt -s nullglob
    local log_file
    for log_file in "${TMP_DIR}"/*.log; do
      printf '\n[upgrade] === replay %s ===\n' "$(basename "${log_file}")" >&2
      cat "${log_file}" >&2
    done
    shopt -u nullglob
  fi

  # 只有持有升级锁的进程才允许恢复服务或清理 rehearsal 容器，避免并发启动失败
  # 的第二个进程误碰正在执行升级的一号进程。
  if [[ "${exit_code}" -ne 0 && "${LOCK_ACQUIRED}" == "true" ]]; then
    if [[ "${PRODUCTION_MIGRATE_STARTED}" == "true" && "${PRODUCTION_MIGRATE_COMPLETED}" != "true" ]]; then
      printf '\n[upgrade] WARNING: production migrate was in progress when failure occurred.\n' >&2
      printf '[upgrade] NOT restarting services automatically — DB may be mid-migration.\n' >&2
      if [[ -n "${MAIN_DUMP}" && -f "${MAIN_DUMP}" ]]; then
        printf '[upgrade] 升级前备份已保留：%s\n' "${MAIN_DUMP}" >&2
        printf '[upgrade] 回滚方向：停业务服务 → 将 django-db 重置为空库 → 用该 dump 执行 pg_restore。\n' >&2
        printf '[upgrade] 先看上方迁移报错，判断是修正后前滚（fix-forward）还是回滚到此备份。\n' >&2
      else
        printf '[upgrade] WARNING: 未找到升级前备份，回滚需依赖你的外部备份。\n' >&2
      fi
      printf '[upgrade] 核对/恢复 schema 后，再用 docker compose up -d 拉起服务。\n' >&2
    elif [[ "${PRODUCTION_MIGRATE_COMPLETED}" == "true" ]]; then
      printf '\n[upgrade] failure after production migrations completed; starting services on migrated schema\n' >&2
      run_cleanup_command "start services on migrated schema" \
        "${COMPOSE[@]}" up -d --remove-orphans
    elif [[ "${REHEARSAL_IN_PROGRESS}" == "true" ]]; then
      print_rehearsal_failure_help
      if [[ "${APP_SERVICES_STOP_REQUESTED}" == "true" ]]; then
        printf '[upgrade] restoring app services stopped before rehearsal\n' >&2
        restore_pre_migration_services
      fi
    elif [[ "${APP_SERVICES_STOP_REQUESTED}" == "true" ]]; then
      printf '\n[upgrade] failure before production migrate; restoring previously stopped app services\n' >&2
      restore_pre_migration_services
    fi
  fi

  if [[ -n "${TMP_DIR}" && -d "${TMP_DIR}" ]]; then
    rm -rf "${TMP_DIR}"
  fi

  if [[ "${LOCK_ACQUIRED}" == "true" ]]; then
    # -v 连带删除匿名卷：postgres 镜像声明 VOLUME /var/lib/postgresql，演练库
    # 未显式挂卷，不加 -v 每次升级都会残留一个含完整生产数据副本的匿名卷
    # （磁盘只增不减，且敏感数据藏在无名卷里难以察觉）。
    "${REHEARSAL_COMPOSE[@]}" rm -sfv migration-rehearsal-db >/dev/null 2>&1 || true
  fi

  exit "${exit_code}"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

run_cleanup_command() {
  local description="$1"
  shift
  local output

  if ! output="$("$@" 2>&1)"; then
    printf '[upgrade] WARNING: failed to %s\n' "${description}" >&2
    printf '%s\n' "${output}" >&2
  fi
}

ensure_git_clean() {
  if [[ "${ALLOW_DIRTY_UPGRADE:-false}" == "true" ]]; then
    return
  fi

  if [[ -n "$(git status --porcelain)" ]]; then
    die "git worktree is dirty; commit or stash changes before production upgrade"
  fi
}

pull_code() {
  if [[ "${SKIP_GIT_PULL}" == "true" ]]; then
    log "skip git pull; using current working tree"
    return
  fi

  ensure_git_clean
  # 发布渠道锁定 main 最新。显式 checkout 是收敛动作：若有人在服务器上切到其他
  # 分支排查问题后忘记切回，bare git pull 会把那个分支当作"最新版"部署上线。
  # 将来开始按 release tag 发版时，再引入版本参数（注意 detached HEAD 下需跳过 pull）。
  log "fetch and fast-forward main"
  git fetch --prune
  git checkout main
  git pull --ff-only
  ensure_git_clean
}

wait_for_postgres() {
  local service="$1"
  local compose=("${COMPOSE[@]}")

  if [[ "${service}" == migration-rehearsal-* ]]; then
    compose=("${REHEARSAL_COMPOSE[@]}")
  fi

  log "wait for ${service}"
  # 就绪探测【必须走 TCP（-h 127.0.0.1）】，不能用 pg_isready 默认的 Unix socket：
  # postgres 官方镜像首次初始化会先起一个【仅监听 socket、不监听 TCP】的临时实例跑
  # 建库脚本，再停掉它、重启为正式实例。socket 版 pg_isready 会在临时实例阶段就报
  # 就绪，导致随后经 TCP 连接的 restore/migrate 撞上"临时实例→正式实例"切换窗口而
  # 偶发失败。临时实例不监听 TCP，故 -h 127.0.0.1 探测只有正式实例起来后才成功，
  # 天然规避该竞态。叠加超时上限，避免 DB 起不来时在持锁状态下无限 hang。
  "${compose[@]}" exec -T -e WAIT_TIMEOUT="${POSTGRES_WAIT_TIMEOUT}" "${service}" sh -c '
    deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
    until pg_isready -h 127.0.0.1 -p 5432 -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
      if [ "$(date +%s)" -ge "$deadline" ]; then
        echo "pg_isready timed out after ${WAIT_TIMEOUT}s waiting for TCP readiness" >&2
        exit 1
      fi
      sleep 1
    done'
}

dump_main_database() {
  local output="$1"

  log "dump django-db (full)"
  "${COMPOSE[@]}" exec -T django-db sh -c \
    'pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"${output}"
}

restore_main_database() {
  local service="$1"
  local input="$2"

  log "restore main dump into ${service}"
  "${REHEARSAL_COMPOSE[@]}" exec -T "${service}" sh -c \
    'pg_restore --exit-on-error --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <"${input}"
}

run_main_manage() {
  local postgres_host="$1"
  shift

  # POSTGRES_PASSWORD 由 compose 的 env_file (.env) 注入，不再通过命令行 -e 传递，
  # 避免短时间内出现在宿主 ps aux 输出中。
  # one-off 容器在 compose 网络内访问 django-db，必须使用容器端口 5432，
  # 避免 .env 中宿主映射端口污染生产/演练 manage.py 连接。
  # -T 禁用 PTY，确保 stdout 是干净字节流，避免 ANSI 控制序列/CR 行尾污染 plan 比对。
  "${COMPOSE[@]}" run --rm --no-deps -T \
    -e XCASH_IGNORE_DATABASE_URL=true \
    -e POSTGRES_HOST="${postgres_host}" \
    -e POSTGRES_PORT=5432 \
    django python manage.py "$@"
}

stop_app_services() {
  local reason="$1"
  local running_services

  log "stop app services ${reason}"
  if [[ "${APP_SERVICES_STOP_REQUESTED}" != "true" ]]; then
    running_services="$("${COMPOSE[@]}" ps --services --filter status=running django worker beat)"
    if [[ -n "${running_services}" ]]; then
      while IFS= read -r service; do
        [[ -n "${service}" ]] && APP_SERVICES_TO_RESTORE+=("${service}")
      done <<<"${running_services}"
    fi
  fi

  APP_SERVICES_STOP_REQUESTED=true
  "${COMPOSE[@]}" stop django worker beat
}

restore_pre_migration_services() {
  if [[ "${#APP_SERVICES_TO_RESTORE[@]}" -eq 0 ]]; then
    printf '[upgrade] no app services were running before stop; nothing to restore\n' >&2
    return
  fi

  run_cleanup_command "restore previously running app services" \
    "${COMPOSE[@]}" start "${APP_SERVICES_TO_RESTORE[@]}"
}

# 从 migrate --plan 输出中只保留真正的迁移行：Django 将其【顶格】输出（形如
# "app_label.NNNN_name"），前导空白按可选兼容上游格式微调；丢弃提示文案、空行、
# 日志、以及可能漏入的 compose 进度残留，让 diff 比对鲁棒。
# 操作明细行（形如 "    Add field xxx"）首字母为大写，不匹配 [a-z_]，会被自然过滤。
extract_plan() {
  grep -E '^[[:space:]]*[a-z_][a-z0-9_]*\.[0-9]{4}_' "$1" || true
}

compare_plans() {
  local rehearsal_plan="$1"
  local production_plan="$2"
  local label="$3"
  local rehearsal_norm="${rehearsal_plan}.norm"
  local production_norm="${production_plan}.norm"

  extract_plan "${rehearsal_plan}" >"${rehearsal_norm}"
  extract_plan "${production_plan}" >"${production_norm}"

  if ! diff -u "${rehearsal_norm}" "${production_norm}"; then
    die "${label} migrate --plan differs between rehearsal and production"
  fi
}

# 演练失败时面向（开源各部署实例）运维的导向性说明：原始报错由 cleanup 回放
# main-rehearsal-*.log 给出，这里负责把它翻译成"发生了什么 / production 是否安全 /
# 怎么处理"，避免运维只看到一堆 traceback 不知所措。
print_rehearsal_failure_help() {
  printf '\n[upgrade] ============================================================\n' >&2
  printf '[upgrade] 迁移演练失败 —— production 数据库【未被改动】，升级已安全中止。\n' >&2
  printf '[upgrade] 含义：本实例的存量数据无法通过新版本的数据库迁移（演练在 production\n' >&2
  printf '[upgrade]       数据的副本上运行，专为在不触碰 production 的前提下暴露此类问题）。\n' >&2
  printf '[upgrade] 详情：见上方 replay 的 main-rehearsal-*.log —— 失败的迁移名与抛错操作\n' >&2
  printf '[upgrade]       （通常是 NOT NULL / UNIQUE / FK / CHECK 等约束撞上违规存量数据）。\n' >&2
  printf '[upgrade] 处理：按报错定位并清洗/修正违规数据，或前滚一版补数据归一化的迁移，\n' >&2
  printf '[upgrade]       然后重跑本脚本；全程 production 未受影响，可放心排查。\n' >&2
  printf '[upgrade] ============================================================\n' >&2
}

trap cleanup EXIT

# 不接受任何位置参数：发布渠道已锁定 main 最新（见 pull_code）。显式拒绝而非
# 静默忽略，防止有人习惯性传 "./upgrade.sh v1.2.0" 却被实际升级到 main 而不自知。
[[ $# -eq 0 ]] || die "unexpected argument: $1 — this script always upgrades to latest main (set SKIP_GIT_PULL=true to deploy the current tree)"

require_command docker
require_command git
require_command flock

[[ -f "${ENV_FILE}" ]] || die "env file not found: ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || die "compose file not found: ${COMPOSE_FILE}"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

[[ -n "${POSTGRES_PASSWORD:-}" ]] || die "POSTGRES_PASSWORD is required"

# 互斥锁：避免两人同时执行 upgrade.sh 造成 dump/restore/migrate 交叉污染。
# 文件描述符 9 在脚本退出时自动释放，无需在 cleanup 中显式处理。
exec 9>"${UPGRADE_LOCK_FILE}"
flock -n 9 || die "another upgrade is in progress (lock: ${UPGRADE_LOCK_FILE})"
LOCK_ACQUIRED=true

TMP_DIR="$(mktemp -d)"
MAIN_REHEARSAL_PLAN="${TMP_DIR}/main-rehearsal.plan"
MAIN_PRODUCTION_PLAN="${TMP_DIR}/main-production.plan"

# 升级前备份落在 TMP_DIR 之外的持久目录，绝不随 cleanup 删除；转绝对路径便于失败时
# 照搬做回滚。同一份 dump 也用于灌入演练库——在线 dump 时不必为备份在停机窗口内再
# dump 一次，省停机时间（代价见顶部 STOP_BEFORE_REHEARSAL 注释）。
mkdir -p "${BACKUP_DIR}"
BACKUP_DIR="$(cd "${BACKUP_DIR}" && pwd)"
MAIN_DUMP="${BACKUP_DIR}/xcash-pre-upgrade-$(date +%Y%m%d-%H%M%S).dump"

pull_code

log "build production images"
"${COMPOSE[@]}" build

log "ensure database and cache dependencies are running"
# --no-recreate：在备份 dump 落盘之前，绝不因 compose 配置/镜像变更重建生产 DB
# 容器，保证备份必定取自升级前已知良好的实例。否则一旦本次升级恰好改动了 db 服务
# 配置（典型如卷映射写错），重建后的新容器会以空库启动，随后的"备份"与演练都将在
# 空库上全绿通过，整条防线失效。db/redis 的配置变更统一推迟到升级末尾的
# up -d --remove-orphans 应用——那时真实数据的备份已经在手。
"${COMPOSE[@]}" up -d --no-recreate django-db redis
wait_for_postgres django-db

if [[ "${STOP_BEFORE_REHEARSAL}" == "true" ]]; then
  log "quiesced dump: stop app services before dump (backup == production at migrate time)"
  stop_app_services "before rehearsal dump"
else
  log "online dump: app services stay up during dump (shorter downtime; backup is the pre-dump snapshot)"
fi

dump_main_database "${MAIN_DUMP}"
log "pre-upgrade backup saved: ${MAIN_DUMP}"

log "reset rehearsal database"
# -v 清理上一轮残留的匿名卷，理由见 cleanup 中同命令的注释。
"${REHEARSAL_COMPOSE[@]}" rm -sfv migration-rehearsal-db >/dev/null 2>&1 || true
"${REHEARSAL_COMPOSE[@]}" up -d migration-rehearsal-db
wait_for_postgres migration-rehearsal-db

restore_main_database migration-rehearsal-db "${MAIN_DUMP}"

log "run main database migration rehearsal"
# 演练在 production 数据副本上真实 apply 迁移：失败即代表本实例存量数据迁不过，
# REHEARSAL_IN_PROGRESS 让 cleanup 给出面向运维的诊断。此刻 production 尚未被触碰。
REHEARSAL_IN_PROGRESS=true
# plan 阶段输出短：tee 同时写 plan 文件（用于比对）+ log 文件（用于失败回放），stdout 静默。
run_main_manage migration-rehearsal-db migrate --plan 2>&1 \
  | tee "${TMP_DIR}/main-rehearsal-plan.log" >"${MAIN_REHEARSAL_PLAN}"
# migrate / check 阶段输出长且重要：tee 写 log 文件 + terminal 实时显示。
run_main_manage migration-rehearsal-db migrate --noinput 2>&1 \
  | tee "${TMP_DIR}/main-rehearsal-migrate.log"
run_main_manage migration-rehearsal-db check --deploy 2>&1 \
  | tee "${TMP_DIR}/main-rehearsal-check.log"
REHEARSAL_IN_PROGRESS=false

if [[ "${STOP_BEFORE_REHEARSAL}" != "true" ]]; then
  stop_app_services "before production migration"
fi

log "verify production migration plans"
run_main_manage django-db migrate --plan 2>&1 \
  | tee "${TMP_DIR}/main-production-plan.log" >"${MAIN_PRODUCTION_PLAN}"
compare_plans "${MAIN_REHEARSAL_PLAN}" "${MAIN_PRODUCTION_PLAN}" "main database"

# 跨过此线后 production 主库可能进入（部分）迁移后状态；迁移完成后
# cleanup 才会按新 schema 尝试拉起服务。
PRODUCTION_MIGRATE_STARTED=true

log "apply production migrations"
run_main_manage django-db migrate --noinput 2>&1 \
  | tee "${TMP_DIR}/main-production-migrate.log"
PRODUCTION_MIGRATE_COMPLETED=true

log "run production post-migration setup"
run_main_manage django-db collectstatic --noinput
run_main_manage django-db ensure_default_superuser

log "start application services"
"${COMPOSE[@]}" up -d --remove-orphans

log "upgrade completed"
log "pre-upgrade backup kept at ${MAIN_DUMP} (confirm new version is stable, then delete to reclaim space)"
