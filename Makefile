ENV_FILE ?= .env
DC = docker compose --env-file $(ENV_FILE) -f docker-compose.dev.yml

.PHONY: help init-env up down upgrade dev-sync dev-up dev-up-pro dev-up-deps dev-up-chain dev-down dev-logs dev-chain-logs dev-ps dev-web dev-worker dev-worker-stress dev-worker-scan dev-beat dev-manage dev-mm dev-migrate dev-clear-migrations dev-shell dev-test pytest dev-local-init dev-bootstrap

help:
	@echo "可用命令："
	@echo "  make init-env        生成生产 .env（自动填充随机密钥）"
	@echo "  make up              启动生产 Docker Compose 服务"
	@echo "  make down            停止生产 Docker Compose 服务"
	@echo "  make upgrade         升级到 main 最新版"
	@echo "  开发环境准备：cp .env.example .env 后按需改成开发值"
	@echo "  make dev-sync         同步本地开发依赖（uv dev group）"
	@echo "  make dev-up           前台运行 Django + Celery（开发模式）"
	@echo "  make dev-up-pro       生产级方式运行（gunicorn + 高并发 worker，适合压测）"
	@echo "  make dev-up-deps      仅启动 django-db/redis"
	@echo "  make dev-up-chain     启动 django-db/redis/anvil"
	@echo "  make dev-down         停止开发依赖容器"
	@echo "  make dev-logs         查看依赖容器日志"
	@echo "  make dev-chain-logs   查看本地区块链容器日志"
	@echo "  make dev-ps           查看依赖容器状态"
	@echo "  make dev-web          宿主机启动 Django"
	@echo "  make dev-worker       宿主机启动业务 Celery worker"
	@echo "  make dev-worker-stress 宿主机启动 stress Celery worker"
	@echo "  make dev-worker-scan  宿主机启动 scan Celery worker"
	@echo "  make dev-beat         宿主机启动 Celery beat"
	@echo "  make dev-manage ARGS='check'"
	@echo "  make dev-mm           宿主机执行 Django makemigrations"
	@echo "  make dev-migrate      宿主机执行 Django migrate"
	@echo "  make dev-clear-migrations 删除所有 app 的迁移文件（保留 __init__.py）"
	@echo "  make dev-shell        宿主机进入 Django shell_plus"
	@echo "  make dev-test         启动依赖后使用 Postgres/Redis 运行 Django 测试"
	@echo "  make pytest           使用 pytest 重建测试库并跳过迁移运行测试"
	@echo "  make dev-local-init   初始化本地联调链配置（anvil）"
	@echo "  make dev-bootstrap    初始化主库和本地联调链"

init-env:
	./scripts/init_env.sh

up:
	docker compose up -d

down:
	docker compose down

upgrade:
	./scripts/upgrade.sh main

dev-sync:
	uv sync --group dev

dev-up:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-up.sh

dev-up-pro:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-up-pro.sh

dev-up-deps:
	$(DC) up -d django-db redis

dev-up-chain:
	$(DC) up -d django-db redis anvil

dev-down:
	$(DC) down

dev-logs:
	$(DC) logs -f django-db redis flower

dev-chain-logs:
	$(DC) logs -f anvil

dev-ps:
	$(DC) ps

dev-web:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-web.sh

dev-worker:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-worker.sh

dev-worker-stress:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-worker-stress.sh

dev-worker-scan:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-worker-scan.sh

dev-beat:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-beat.sh

dev-manage:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-manage.sh $(ARGS)

dev-mm:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-manage.sh makemigrations

dev-migrate:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-manage.sh migrate

dev-clear-migrations:
	./scripts/dev-clear-migrations.sh

dev-shell:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-manage.sh shell_plus

dev-test:
	$(DC) up -d django-db redis
	# 复用已有测试库，避免非交互环境在 test_xcash 已存在时卡住确认提示。
	PYTHONPATH=xcash ./.venv/bin/python manage.py test --settings=config.settings.test --keepdb

pytest:
	uv run pytest --create-db --nomigrations -q

dev-local-init:
	$(DC) up -d django-db redis anvil
	# 本地链初始化与生产默认 init 分离，避免误写 Sepolia / mainnet 配置到开发库。
	ENV_FILE=$(ENV_FILE) ./scripts/dev-manage.sh init_local_chains

dev-bootstrap:
	ENV_FILE=$(ENV_FILE) ./scripts/dev-bootstrap.sh
