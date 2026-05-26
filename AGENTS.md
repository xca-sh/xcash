## 基本要求

中文交流
项目正在进行破坏性重构，所以不需要考虑历史数据迁移，也不需要生成数据库迁移文件，一切代码以长期有利为准，不考虑历史兼容性
## 项目概述

- Xcash, 基于 Django 的开源企业级加密货币金融网关，支持支付、充币、提币， 支持多商户与多项目管理。
- 使用 uv 进行环境管理，本地调试使用 uv run 运行 python 命令。
- Address 模型私钥仅存在于内部系统，不可能在系统外发送交易，所以 evm 类型的 nonce 在每个 chain 严格从 0 逐一递增。
- 当前 admin theme 用的是 django-unfold，admin 页面开发要遵循UI风格统一。
- 本地通过 docker-compose.dev.yml 拉起数据库、redis、测试链的服务。
- 生成的docs文件统统不纳入git。
- 执行方式默认用 Subagent-Driven。
- commit 信息用中文写，同时在尾部标注本次代码开发使用的模型信息
- 尽量在 main 分支进行开发
- 项目兼容易支付 V1接口，文档地址：https://pay.v8jisu.cn/doc_old.html
- `xcash-saas` 是 xcash 加密货币支付网关的 SaaS 商业化层；xcash 是区块链引擎。
- **xcash 仓库**：`/Users/void/PycharmProjects/xcash`（本项目，Django 5.2）
- **xcash-saas 仓库**：`/Users/void/PycharmProjects/xcash-saas`（Django 5.2 + DRF）

## 测试要求

- 只为核心业务逻辑、状态流转、并发安全、金额计算、外部接口异常处理等“行为正确性”编写测试。
- 不为纯配置数值、资源档位、展示文案这类“具体取值”写测试；这类规则以 README、.env.example 或部署文档为准。只有当配置解析、非法值校验、环境变量覆盖、权限边界或业务行为会被影响时，才补行为测试。
- 对配置名、常量名、枚举值、资源档位名称这类不改变业务逻辑的替换，只同步已有测试中的旧名称或旧取值，不为了证明新名称存在、旧名称失效而新增测试；此约束优先于通用 TDD 流程。
- 对纯展示层改动，默认不写测试，包括但不限于：admin 列表列是否显示、字段顺序、文案展示、readonly_fields
  配置、help_text、verbose_name 这类不影响业务逻辑的变更。
- 如果某个后台展示配置会直接影响业务操作结果、权限边界或安全性，才允许为其补测试，并需要在说明中明确测试价值。
- 避免为低价值、易碎的界面配置测试增加维护成本。

## 编码要求

- 功能设计要符合第一性原理，不要过度设计
- 核心业务逻辑需有完整的逻辑注释，关键的代码与逻辑必须写对应的单元测试
- 保证代码的逻辑清晰，人类可读性高
- 所有外部 API 调用（区块链节点等）必须有异常处理和超时设置
- 本项目所有 Django/DRF API 端点一律使用不带尾部 / 的 URL：禁止定义、生成、文档化或请求带 / 结尾的路由，并始终保持 APPEND_SLASH=False、DRF router 的 trailing_slash=False。

## 迁移要求

- **收窄数据合法域的任何 DB 操作**（新增 NOT NULL 字段、把已有字段改为 NOT NULL、新增 UNIQUE / unique_together / CHECK / FK 约束、收紧字段长度或枚举范围等），必须在**同一迁移或前置迁移**中用 `RunPython` 按**确定性、幂等**的规则把存量数据归一化到满足新约束的形态，再执行收窄操作。归一化规则要写在 RunPython 函数 docstring 里，说明对冲突数据的处理方式（回填值 / 去重判据 / 删孤儿等），便于运维事后排查。
- 因为 `django-migration-linter` 看不到 `RunPython` 的回填/清洗语义，会把这类迁移的收窄步骤判为 `NOT_NULL` / `UNIQUE` 等违规：必须在该迁移文件里插入 `IgnoreMigration()`（`from django_migration_linter.operations import IgnoreMigration`）做**定点豁免**，并在 operations 上方写注释说明"已通过 RunPython 归一化，故安全"。禁止用全局排除规则或调低 linter 等级绕过。
- 因为上一条要求迁移文件在模块顶层 `from django_migration_linter.operations import IgnoreMigration`，`django-migration-linter` 必须放在 `pyproject.toml` 的 `[project].dependencies` 主依赖里（而非 `dev` group），否则 production 镜像不装该包，`migrate` 加载迁移模块即 `ModuleNotFoundError`。该包仅在 dev settings 才加入 `INSTALLED_APPS`，production 装而不启用，运行时无副作用。
- **如果某个约束没有可以默认接受的归一化规则**（典型如 UNIQUE 冲突无法机械判定保留谁、FK 孤儿删了会丢业务数据），**禁止在该迁移直接加约束**。改用一版只做"标记/旁路冲突行 + 日志告警"的过渡迁移让运维人工处理，下一版再加约束；或把约束降级为应用层校验，从源头掐住新增冲突，等存量自然消化。
- `RunPython` 的反向函数若无法真实还原数据，要写明确说明原因的 no-op，而不是留空或抛异常。
- 新建表（首次迁移）内的 NOT NULL / UNIQUE 等约束、以及带常量 default 的新增列，不在此约束之列。

## 安全要求

- 金融操作必须防止并发竞争（使用 select_for_update或其他有效措施），并且保证数据安全、一致
- 敏感日志不得记录私钥相关信息
