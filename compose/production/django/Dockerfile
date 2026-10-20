# define an alias for the specific python version used in this file.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS python-build-stage

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

ARG APP_HOME=/app

WORKDIR ${APP_HOME}

# Install apt packages
RUN apt-get update && apt-get install --no-install-recommends -y \
  # dependencies for building Python packages \
  build-essential \
  # psycopg dependencies \
  libpq-dev \
  # coincurve (secp256k1 bindings) build dependencies \
  pkg-config \
  libsecp256k1-dev

# Requirements are installed here to ensure they will be cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --group production --no-group dev
# 项目配置 package=false，不安装项目包；无需复制源码后再次 uv sync。

# Python 'run' stage
FROM python:3.13-slim-bookworm AS python-run-stage

ARG APP_HOME=/app

WORKDIR ${APP_HOME}

RUN addgroup --system django \
  && adduser --system --ingroup django django

# Install required system dependencies
RUN apt-get update && apt-get install --no-install-recommends -y \
  # psycopg runtime dependency \
  libpq5 \
  # Translations dependencies \
  gettext \
  # entrypoint \
  wait-for-it \
  # cleaning up unused files \
  && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
  && rm -rf /var/lib/apt/lists/*

# 依赖单独成层，并放在经常改动的启动脚本和源码之前。只改业务代码时不再
# 将整个 .venv 重新写入新镜像层，也不重新导出/解包这份大依赖层。
COPY --from=python-build-stage --chown=django:django ${APP_HOME}/.venv ${APP_HOME}/.venv

# 复制项目启动脚本
COPY --chown=django:django ./compose/production/django/entrypoint /entrypoint
COPY --chown=django:django ./compose/production/django/start /start
COPY --chown=django:django ./compose/production/django/worker/start /start-worker
COPY --chown=django:django ./compose/production/django/chain-runner/start /start-chain-runner
RUN set -eux; \
    sed -i 's/\r$//g' /entrypoint /start /start-worker /start-chain-runner; \
    chmod +x /entrypoint /start /start-worker /start-chain-runner

# 源码独立成层；.dockerignore 排除本地虚拟环境和运行密钥。
COPY --chown=django:django . ${APP_HOME}

# Place executables in the environment at the front of the path
ENV PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production

RUN chmod +x ./compose/production/django/build-assets.sh && \
    ./compose/production/django/build-assets.sh && \
    chown -R django:django /app/staticfiles

USER django

ENTRYPOINT ["/entrypoint"]
