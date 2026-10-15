#!/bin/bash

set -o errexit
set -o nounset
set -o pipefail

# 本脚本落盘的内容（数据库 dump、迁移日志/plan、升级锁）均属运维敏感数据，
# 统一收紧新建文件权限为 600 / 目录 700；已存在的文件与目录不受影响。
umask 077

ENV_FILE="${ENV_FILE:-.env}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}"
# 默认在线 dump（不停机生成演练数据，缩短停机窗口、提升升级体验）：dump 期间
# django/worker/beat 继续服务，仅在 production migrate 前才停机。
# 代价：dump 时刻到停机时刻之间的新写入不在演练样本内。要求"演练样本即上线前最后
# 状态"时显式设 STOP_BEFORE_REHEARSAL=true 走停机 dump（停机更久）。
STOP_BEFORE_REHEARSAL="${STOP_BEFORE_REHEARSAL:-false}"
UPGRADE_LOCK_FILE="${UPGRADE_LOCK_FILE:-/tmp/xcash-upgrade.lock}"
# 迁移演练用数据库 dump 目录：dump 必须落在 TMP_DIR 之外，避免演练库 restore 时
# 受临时目录清理影响；演练成功后立即删除本次生成的 dump，缩短敏感数据生命周期。
# .gitignore 已忽略 backups/。
BACKUP_DIR="${BACKUP_DIR:-./backups}"
# postgres 就绪等待上限（秒）：避免 DB 起不来时在持有升级锁的情况下无限 hang。
POSTGRES_WAIT_TIMEOUT="${POSTGRES_WAIT_TIMEOUT:-120}"
APP_READY_TIMEOUT="${APP_READY_TIMEOUT:-360}"
# 拉取镜像的超时上限（秒）：避免 registry 网络卡住时在持有升级锁的情况下无限 hang。
IMAGE_PULL_TIMEOUT="${IMAGE_PULL_TIMEOUT:-1800}"

# cleanup 需要区分失败发生在哪个阶段：
# - production migrate 前：production 库未被触碰，只恢复本脚本停过的旧容器
# - production migrate 中：DB 可能处于中间态，不自动启动业务服务
# - production migrate 后：DB 已到新 schema，后置步骤失败时按新镜像尝试恢复服务
LOCK_ACQUIRED=false
# 切换版本前必须全部停掉的业务服务。任何会读写业务库的常驻服务都要列在这里——
# 漏掉一个，它就会在 migrate 执行期间继续按旧代码读写处于中间态的 schema。
# worker 单独分组，确保排空时保留 HTTP；迁移前仍停止所有业务服务。
APP_WORKER_SERVICES=(worker worker-scan)
APP_RUNTIME_SERVICES=(django "${APP_WORKER_SERVICES[@]}")
APP_SERVICES=("${APP_RUNTIME_SERVICES[@]}" beat)
APP_SERVICES_STOP_REQUESTED=false
APP_SERVICES_TO_RESTORE=()
REHEARSAL_IN_PROGRESS=false
PRODUCTION_MIGRATE_STARTED=false
PRODUCTION_MIGRATE_COMPLETED=false
PRODUCTION_SETUP_COMPLETED=false
APPLICATION_START_ATTEMPTED=false
HTTP_STOPPED_AT=""
# 演练 dump 文件路径，dump 时赋值；cleanup 在 nounset 下引用，故先声明为空。
MAIN_DUMP=""
RUN_MIGRATION_REHEARSAL=true

COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")
REHEARSAL_COMPOSE=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" --profile migration-rehearsal)
TMP_DIR=""

log() {
  printf '[upgrade %s +%ss] %s\n' "$(date '+%H:%M:%S')" "${SECONDS}" "$*"
}

run_stage() {
  local description="$1"
  shift
  local started=${SECONDS}
  local result=0
  log "${description}"
  "$@" || result=$?
  log "${description}: finished in $((SECONDS - started))s (exit ${result})"
  return "${result}"
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
        printf '[upgrade] 演练 dump 尚未删除，可临时作为回滚输入：%s\n' "${MAIN_DUMP}" >&2
        printf '[upgrade] 回滚方向：停业务服务 → 将 db 重置为空库 → 用该 dump 执行 pg_restore。\n' >&2
        printf '[upgrade] 先看上方迁移报错，判断是修正后前滚（fix-forward）还是回滚到该 dump。\n' >&2
      else
        printf '[upgrade] 演练 dump 已在演练成功后清理；回滚需依赖外部备份。\n' >&2
        printf '[upgrade] 优先看上方迁移报错，判断能否修正后前滚（fix-forward）。\n' >&2
      fi
      printf '[upgrade] 核对/恢复 schema 后，再用 docker compose up -d 拉起服务。\n' >&2
    elif [[ "${APPLICATION_START_ATTEMPTED}" == "true" ]]; then
      printf '\n[upgrade] application startup/readiness failed; inspect running services before retrying.\n' >&2
    elif [[ "${PRODUCTION_MIGRATE_COMPLETED}" == "true" ]]; then
      printf '\n[upgrade] failure after production migrations completed; retrying setup before starting services\n' >&2
      run_cleanup_command "start services on migrated schema" \
        recover_application_services
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

  log "dump db (full)"
  "${COMPOSE[@]}" exec -T db sh -c \
    'pg_dump --format=custom --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"${output}"
}

restore_main_database() {
  local service="$1"
  local input="$2"

  log "restore main dump into ${service}"
  "${REHEARSAL_COMPOSE[@]}" exec -T "${service}" sh -c \
    'pg_restore --exit-on-error --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <"${input}"
}

delete_rehearsal_dump() {
  if [[ -z "${MAIN_DUMP}" || ! -f "${MAIN_DUMP}" ]]; then
    return
  fi

  log "delete migration rehearsal dump"
  rm -f "${MAIN_DUMP}"
  log "migration rehearsal dump deleted after successful rehearsal: ${MAIN_DUMP}"
}

run_main_manage() {
  local postgres_host="$1"
  shift

  # POSTGRES_PASSWORD 由 compose 的 env_file (.env) 注入，不再通过命令行 -e 传递，
  # 避免短时间内出现在宿主 ps aux 输出中。
  # one-off 容器在 compose 网络内访问 db，必须使用容器端口 5432，
  # 避免 .env 中宿主映射端口污染生产/演练 manage.py 连接。
  # -T 禁用 PTY，确保 stdout 是干净字节流，避免 ANSI 控制序列/CR 行尾污染 plan 比对。
  "${COMPOSE[@]}" run --rm --no-deps -T \
    -e XCASH_IGNORE_DATABASE_URL=true \
    -e POSTGRES_HOST="${postgres_host}" \
    -e POSTGRES_PORT=5432 \
    django python -u manage.py "$@"
}

stop_app_services() {
  local reason="$1"
  local running_services

  log "stop app services ${reason}"
  if [[ "${APP_SERVICES_STOP_REQUESTED}" != "true" ]]; then
    running_services="$("${COMPOSE[@]}" ps --services --filter status=running "${APP_SERVICES[@]}")"
    if [[ -n "${running_services}" ]]; then
      while IFS= read -r service; do
        [[ -n "${service}" ]] && APP_SERVICES_TO_RESTORE+=("${service}")
      done <<<"${running_services}"
    fi
  fi

  APP_SERVICES_STOP_REQUESTED=true
  # 先停调度源，再停消费者，避免 worker 退出时旧 Beat 继续向旧队列投递。
  run_stage "stop Beat" "${COMPOSE[@]}" stop beat || return 1
  # 此时 schema 尚未改变，旧 HTTP 可继续处理请求；新投递的异步任务留在 broker，
  # 待旧消费者全部退出后由新版消费。生产迁移仍须等 Django 也停止，不能交错版本。
  run_stage "drain workers; HTTP remains available" \
    "${COMPOSE[@]}" stop "${APP_WORKER_SERVICES[@]}" || return 1
  HTTP_STOPPED_AT="$(date +%s)"
  run_stage "stop Django; HTTP downtime begins" "${COMPOSE[@]}" stop django
}

prepare_application() {
  run_stage "initialize production runtime" \
    run_main_manage db bootstrap_runtime --skip-migrations || return 1
  PRODUCTION_SETUP_COMPLETED=true
}

recover_application_services() {
  # 初始化失败时绝不能使用 --prepared 启动，也不能让 worker 抢先读未补齐的主数据。
  prepare_application || return 1
  start_application_services
}

start_application_services() {
  [[ "${PRODUCTION_SETUP_COMPLETED}" == "true" ]] || return 1
  APPLICATION_START_ATTEMPTED=true
  local prepared_override="${TMP_DIR}/prepared.yml"
  local readiness_args=()
  if [[ -n "${HTTP_STOPPED_AT}" ]]; then
    readiness_args=(--http-stopped-at "${HTTP_STOPPED_AT}")
  fi
  # override 只供本次启动使用，不写 .env，不改变普通首次部署的初始化入口。
  printf 'services:\n  django:\n    command: ["/start", "--prepared"]\n' >"${prepared_override}" || return 1
  # cleanup 的条件调用会抑制 errexit，每个门控必须显式传播失败。
  run_stage "start prepared application services" \
    "${COMPOSE[@]}" -f "${prepared_override}" up -d --remove-orphans "${APP_RUNTIME_SERVICES[@]}" caddy \
    || return 1
  "${COMPOSE[@]}" exec -T django python -u manage.py wait_for_runtime \
    --phase consumers --timeout "${APP_READY_TIMEOUT}" "${readiness_args[@]}" \
    || return 1
  # 新 Beat 启动即投递健康探针，可能早于等待命令自身完成启动；以发起 Beat 启动的
  # 时刻作为探针发布下限。旧 Beat 早已停止，此后发布的探针只可能来自新 Beat。
  local beat_start_requested_at
  beat_start_requested_at="$(date +%s)"
  run_stage "start Beat" "${COMPOSE[@]}" up -d --no-deps beat || return 1
  if ! "${COMPOSE[@]}" exec -T django python -u manage.py wait_for_runtime \
    --phase scheduler --timeout "${APP_READY_TIMEOUT}" \
    --published-after "${beat_start_requested_at}"; then
    # 消费回执不完整时暂停投递源，保留应用进程供排查；不能打印升级成功。
    "${COMPOSE[@]}" stop beat || true
    return 1
  fi
  # Caddy 不监视配置文件；caddy 容器未被重建时，git pull 带来的 Caddyfile 变更不会
  # 自动生效，这里显式 reload。reload 平滑切换、不断开连接；新配置有误时 reload
  # 失败、旧配置继续服务。放在最后执行：此时刚（重）建的 caddy 管理端口早已就绪，
  # 且配置错误不会拖住 Beat 等业务进程的启动。
  run_stage "reload Caddy config" "${COMPOSE[@]}" exec -T caddy \
    caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
}

remove_unused_images() {
  local project="${COMPOSE_PROJECT_NAME:-xcash}"

  # 旧版本在服务器本地构建镜像，切换到 GHCR 后这两个镜像不再被任何服务引用。
  docker image rm "${project}-app:local" "${project}-caddy" >/dev/null 2>&1 || true
  # 删除被本次拉取顶掉的旧版本（悬空镜像），避免每次升级都让磁盘只增不减。
  # 只删悬空镜像，不动正在使用或带标签的镜像；清理失败不影响升级结果。
  run_stage "prune dangling images" docker image prune -f \
    || log "WARNING: failed to prune dangling images"
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
  local rehearsed_plan="$1"
  local production_plan="$2"
  local label="$3"
  local rehearsed_norm="${rehearsed_plan}.norm"
  local production_norm="${production_plan}.norm"

  extract_plan "${rehearsed_plan}" >"${rehearsed_norm}"
  extract_plan "${production_plan}" >"${production_norm}"

  if ! diff -u "${rehearsed_norm}" "${production_norm}"; then
    die "${label} migrate --plan changed since the rehearsed plan"
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

# 用户运行前先 git pull 更新 compose 与脚本；脚本按当前工作区的 compose 拉取 GHCR
# 镜像并部署，不切分支或更新代码。
# 拒绝版本参数，避免用户误以为脚本会自动切换到指定版本。
[[ $# -eq 0 ]] || die "unexpected argument: $1 — this script deploys the current working tree; update code manually before running it"

require_command docker
require_command git
require_command flock
require_command timeout

[[ -f "${ENV_FILE}" ]] || die "env file not found: ${ENV_FILE}"
[[ -f "${COMPOSE_FILE}" ]] || die "compose file not found: ${COMPOSE_FILE}"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

[[ -n "${POSTGRES_PASSWORD:-}" ]] || die "POSTGRES_PASSWORD is required"
[[ "${APP_READY_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "APP_READY_TIMEOUT must be a positive integer"
[[ "${IMAGE_PULL_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] || die "IMAGE_PULL_TIMEOUT must be a positive integer"
ensure_git_clean
log "deploy current working tree; update code manually with git pull before running this script"

# 互斥锁：避免两人同时执行 upgrade.sh 造成 dump/restore/migrate 交叉污染。
# 文件描述符 9 在脚本退出时自动释放，无需在 cleanup 中显式处理。
exec 9>"${UPGRADE_LOCK_FILE}"
flock -n 9 || die "another upgrade is in progress (lock: ${UPGRADE_LOCK_FILE})"
LOCK_ACQUIRED=true

TMP_DIR="$(mktemp -d)"
MAIN_PENDING_PLAN="${TMP_DIR}/main-pending.plan"
MAIN_PRODUCTION_PLAN="${TMP_DIR}/main-production.plan"

# 演练 dump 落在 TMP_DIR 之外的持久目录，失败时不随 cleanup 删除，便于排查失败演练。
# 同一份 dump 用于灌入演练库——在线 dump 时不必为演练在停机窗口内再 dump 一次，
# 省停机时间（代价见顶部 STOP_BEFORE_REHEARSAL 注释）。演练成功后会立即删除本次
# dump，避免敏感数据长时间落盘。
mkdir -p "${BACKUP_DIR}"
BACKUP_DIR="$(cd "${BACKUP_DIR}" && pwd)"
MAIN_DUMP="${BACKUP_DIR}/xcash-pre-upgrade-$(date +%Y%m%d-%H%M%S).dump"

# 镜像由 CI 构建并发布到 GHCR，服务器只拉取不构建。拉取在停任何服务之前：网络失败
# 直接中止，线上服务不受影响，重跑即可；运行中的容器绑定镜像 ID，不受拉取影响。
run_stage "pull production images" timeout "${IMAGE_PULL_TIMEOUT}" "${COMPOSE[@]}" pull

log "ensure database and cache dependencies are running"
# --no-recreate：在演练 dump 落盘之前，绝不因 compose 配置/镜像变更重建生产 DB
# 容器，保证演练样本必定取自升级前已知良好的实例。否则一旦本次升级恰好改动了 db 服务
# 配置（典型如卷映射写错），重建后的新容器会以空库启动，随后的 dump 与演练都将在
# 空库上全绿通过，整条防线失效。db/redis 的配置变更统一推迟到升级末尾的
# up -d --remove-orphans 应用——那时真实数据已完成演练并进入受控升级路径。
"${COMPOSE[@]}" up -d --no-recreate db redis
wait_for_postgres db

# 代码已由用户提前拉取，无法用本次运行前后的 Git 差异判断迁移。
# 用新镜像只读查询生产库的待执行计划；查询失败必须中止，不能当作无需迁移。
log "check pending production migrations"
run_main_manage db migrate --plan 2>&1 \
  | tee "${TMP_DIR}/main-pending-plan.log" >"${MAIN_PENDING_PLAN}"
if [[ -z "$(extract_plan "${MAIN_PENDING_PLAN}")" ]]; then
  RUN_MIGRATION_REHEARSAL=false
  log "no pending migrations; skip migration rehearsal"
fi

if [[ "${RUN_MIGRATION_REHEARSAL}" == "true" ]]; then
if [[ "${STOP_BEFORE_REHEARSAL}" == "true" ]]; then
  log "quiesced dump: stop app services before dump (backup == production at migrate time)"
  stop_app_services "before rehearsal dump"
else
  log "online dump: app services stay up during dump (shorter downtime; backup is the pre-dump snapshot)"
fi

dump_main_database "${MAIN_DUMP}"
log "migration rehearsal dump saved: ${MAIN_DUMP}"

log "reset rehearsal database"
# -v 清理上一轮残留的匿名卷，理由见 cleanup 中同命令的注释。
"${REHEARSAL_COMPOSE[@]}" rm -sfv migration-rehearsal-db >/dev/null 2>&1 || true
"${REHEARSAL_COMPOSE[@]}" up -d migration-rehearsal-db
wait_for_postgres migration-rehearsal-db

restore_main_database migration-rehearsal-db "${MAIN_DUMP}"

log "run main database migration rehearsal"
# 演练在 production 数据副本上执行与生产完全相同的 bootstrap_runtime（系统检查 +
# migrate + 基础数据 + 默认管理员）：失败即代表本实例存量数据迁不过，
# REHEARSAL_IN_PROGRESS 让 cleanup 给出面向运维的诊断。此刻 production 尚未被触碰。
# 全部步骤放在一个一次性容器里，每多一个容器就多一次数秒的 Django 冷启动。
# 被演练的迁移计划就是上方的 pending plan：dump 取自同一生产库，升级锁又排除了
# 并发 migrate，演练副本的迁移状态与其一致；生产迁移前还会再复核一次。
# 不在此跑 check --deploy：部署检查只产出 warning、拦不住升级，CI 已对生产配置执行。
REHEARSAL_IN_PROGRESS=true
run_main_manage migration-rehearsal-db bootstrap_runtime 2>&1 \
  | tee "${TMP_DIR}/main-rehearsal-bootstrap.log"
REHEARSAL_IN_PROGRESS=false
delete_rehearsal_dump
fi

if [[ "${RUN_MIGRATION_REHEARSAL}" == "true" ]]; then
  # 迁移计划只由 django_migrations 决定，停服务只结束进程、不执行迁移，复核结论
  # 在停服务前后一致；放在停服务之前，这次一次性容器就不占 HTTP 停机窗口。
  log "verify production migration plan"
  run_main_manage db migrate --plan 2>&1 \
    | tee "${TMP_DIR}/main-production-plan.log" >"${MAIN_PRODUCTION_PLAN}"
  compare_plans "${MAIN_PENDING_PLAN}" "${MAIN_PRODUCTION_PLAN}" "main database"
fi

if [[ "${APP_SERVICES_STOP_REQUESTED}" != "true" ]]; then
  # 没有待执行迁移只跳过演练，不能跳过版本切换屏障；队列路由等纯代码变更也
  # 需要先停止旧进程。拉取、在线演练和 plan 复核完成后才停服务，保持停机窗口尽可能短。
  stop_app_services "before production migration"
fi

# 跨过此线后 production 主库可能进入（部分）迁移后状态；迁移完成后
# cleanup 才会按新 schema 尝试拉起服务。
PRODUCTION_MIGRATE_STARTED=true

bootstrap_result=0
run_stage "migrate and initialize production runtime" run_main_manage db bootstrap_runtime 2>&1 \
  | tee "${TMP_DIR}/main-production-bootstrap.log" || bootstrap_result=$?
# bootstrap_runtime 仅用 20 表示 migrate 完成后的初始化失败。其他错误（包括
# 容器中断、信号退出、日志管道失败）均不能证明迁移完成，不自动恢复业务进程。
case "${bootstrap_result}" in
  0)
    PRODUCTION_MIGRATE_COMPLETED=true
    PRODUCTION_SETUP_COMPLETED=true
    ;;
  20)
    PRODUCTION_MIGRATE_COMPLETED=true
    exit "${bootstrap_result}"
    ;;
  *) exit "${bootstrap_result}" ;;
esac

log "start application services"
start_application_services

remove_unused_images

log "upgrade completed"
