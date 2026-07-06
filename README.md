# Xcash

<p align="center">
  <strong>开源自托管加密货币收款网关</strong>
  <br />
  支持 USDT、ETH 与主流 EVM 及 Tron 链资产收款，零平台手续费，完全自托管。
</p>

<p align="center">
  <a href="https://xca.sh"><img src="https://img.shields.io/badge/Website-xca.sh-blue" alt="Website"></a>
  <a href="https://xca.sh/docs/"><img src="https://img.shields.io/badge/Docs-xca.sh/docs-blue" alt="Docs"></a>
  <a href="https://github.com/xca-sh/xcash/stargazers"><img src="https://img.shields.io/github/stars/xca-sh/xcash" alt="GitHub Stars"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.13-blue.svg" alt="Python">
  <img src="https://img.shields.io/badge/django-5.2-green.svg" alt="Django">
</p>

<p align="center">
  <a href="README.en.md">English</a> | 简体中文
</p>

## Xcash 是什么？

**Xcash** 是最先进的开源、自托管**加密货币收款网关**，面向商家、SaaS 产品、交易所和钱包平台，提供账单收款、USDT 收款和充值收款能力。

完整部署、配置和 API 对接说明请查看 [Xcash 官方文档](https://xca.sh/docs/)。

不同于 CoinGate、OpenNode 这类托管式收款处理商，Xcash 强调**完全自托管**：收款经智能合约直达你的归集地址，Xcash 全程不过手资金，也不抽取平台手续费。它适合需要多链资产收款、充值收款和 Webhook 通知能力的业务系统。

**适用场景：** 电商加密货币账单收款、USDT 充值收款系统、跨境稳定币结算、SaaS 加密货币订阅计费、链上收款基础设施，以及企业内部数字资产入账管理。

## Xcash 有多安全？

打个比方，如果你用 Xcash 作为项目的加密货币收款网关，即使部署 Xcash 的服务器被攻破、数据库被拖库、密钥泄露，只要保证您的归集地址不被篡改，您的资产都不会有任何安全风险，而且恢复服务后用户完成账单收款或充值收款的款项，仍然会流入您的归集地址，黑客无能为力。

## Xcash 为什么这么安全？

安全是 Xcash 与生俱来的特性，是我们秉承的核心原则。

- Xcash 永不过手您的收款。
- 一切收款走智能合约，智能合约写死资金流向为您的归集地址。
- 收款合约极简，攻击面为 0。

```mermaid
flowchart LR
    Buyer(["买家"])
    Contract["收款智能合约<br/>资金流向写死"]
    Wallet["你的归集地址<br/>私钥只在你手中"]

    Buyer -->|付款| Contract -->|只能流向| Wallet

    subgraph XcashBox["Xcash 系统 · 控制面"]
        Core["API / Worker / 数据库"]
    end
    Core -.->|匹配账单 · 状态 · 通知<br/>全程不经手资金| Contract

    Attacker["攻击者<br/>攻破服务器 / 拖库 / 密钥泄露"]
    Attacker -->|最多控制| XcashBox
    Attacker -- 改不动资金流向 --x Wallet

    classDef money fill:#e6f4ea,stroke:#34a853,color:#000;
    classDef danger fill:#fce8e6,stroke:#ea4335,color:#000;
    class Buyer,Contract,Wallet money;
    class Attacker danger;
```

资金路径（绿色）由智能合约写死，只在「买家 → 收款合约 → 你的归集地址」之间流动；Xcash 仅作控制面，负责账单匹配、状态流转与通知，**不在资金路径上**。因此攻击者即便完全控制 Xcash 系统，也无法改写合约里写死的资金流向。

## 特性

| 特性 | 说明                              |
|------|---------------------------------|
| 账单收款 | 定额、定时的账单式收款，适合电商下单、订阅计费等场景      |
| 充值收款 | 为每个用户分配专属充值收款地址，随时转入、即时入账，体验同交易所  |
| 完全自托管 | 收款经智能合约直达你的钱包，Xcash 全程不过手资金     |
| 零平台手续费 | 不按交易抽成，只承担链上微量 Gas              |
| 多链多币种 | 覆盖主流 EVM 链，支持任意 ERC-20 代币 |
| 多商户多项目 | 单实例隔离管理多个商户与项目                  |
| 智能合约收款 | EVM 链可为每笔账单收款生成独立智能合约收款地址 |
| 链上风控 | 接入 MistTrack 对账单收款、充值收款的来源地址做风险评分   |
| Webhook 回调 | 实时推送账单收款、充值收款事件                     |
| 兼容易支付 | 支持标准易支付 V1 协议，便于平滑迁移            |
| Docker 部署 | Docker Compose 一键部署生产服务         |

## 账单收款 vs 充值收款

Xcash 提供两种入账方式，对接前请先区分：

- **账单收款**：账单式收款。每笔交易创建一张定额、限时的账单，买家付款后账单完成，适合电商下单、订阅计费等一次性收款场景。默认使用智能合约收款：系统为账单收款分配独立收款地址，地址互不冲突、天然支持高并发，金额无需浮动。
- **充值收款**：交易所式充值收款。为每个用户分配专属充值收款地址，且多链共享，实时监控，用户可随时转入、区块确认后入账，无需创建订单，适合需要维护用户余额的钱包、交易类业务。

## Gas 成本与资金归集

启用智能合约收款后，系统会为每笔账单收款、每个充值收款用户分配**独立的收款地址**。很多人第一反应会担心：地址这么多，是不是每笔收款都要单独付一次 Gas 去归集？

答案是**不需要**。Xcash 内置两道归集闸门，把归集次数和 Gas 开销压到最低：

- **归集延迟（周期性批量归集）**：收款确认后不会立即归集，而是等待一个可配置的延迟窗口再尝试。EVM 链默认 **2 分钟**，Tron 链默认 **6 小时**。同一地址在窗口内的多笔入账会被**合并成一次归集**，而不是每笔各付一次 Gas。
- **归集金额门槛**：到期尝试归集时，若该地址该币种的余额价值低于门槛（默认 **1 USD**），则**跳过本次并继续累积**，避免为几毛钱的"粉尘"付出更高的 Gas、注定亏本。

**只有「延迟到达」和「金额达标」两个条件同时满足，系统才会自动发起归集交易。** 未达标的地址不会被丢弃，后续入账会重新评估，攒够门槛后再归集。两道闸门的具体数值都可以在管理后台按链灵活调整。

同时，Xcash 的收款合约保持极简：每次归集的核心动作基本等同于对应代币的一次普通转账，所以单次归集的 Gas 消耗也基本接近该代币转账本身，几乎没有额外合约开销。

更巧妙的是，归集本身是一个**无需许可、与安全无关**的操作：

- 归集合约的 `collect()` **没有任何权限校验**，任何账户都能替你发起归集，调用方只是自付 Gas；而资金流向早已被合约写死为你的归集地址，谁来触发都改变不了结果。
- 所以除了系统按闸门自动归集，你也可以在**任何时间手动触发归集**，不受延迟和门槛限制。
- 无论自动还是手动、无论谁来调用，归集资金**只会流入你的归集地址**，Xcash 全程不过手（原理见[「Xcash 为什么这么安全？」](#xcash-为什么这么安全)）。

因此你只需为系统钱包保留少量原生资产用于支付归集 Gas（见[部署步骤 6](#6-为系统钱包充值-gas)），无需为每笔支付或充币单独操心 Gas——真正的 Gas 开销，由你设定的归集频率与门槛共同决定。

## 链支持

| 功能 | ETH | BNB Chain | Arbitrum | Base | Tron | Polygon | Optimism |
|:--:|:---:|:---------:|:--------:|:----:|:----:|:-------:|:--------:|
| 账单收款 | 是 | 是 | 是 | 是 | 是 | 是 | 是 |
| 充值收款 | 是 | 是 | 是 | 是 | 是 | 是 | 是 |

## 代币支持

| 链类型 | 原生资产 | 代币标准 | 当前支持范围 | 启用方式 |
|:------:|:--------:|:--------:|:------------|:---------|
| EVM | ETH、BNB、POL 等原生资产（用于支付 Gas） | ERC-20 | 支持任意 ERC-20 代币，适合按业务需要接入 USDT、USDC 或其他链上资产收款 | 在后台添加代币合约地址并启用对应公链 |
| Tron | TRX | TRC-20 | 当前放行 USDT 与原生 TRX；其他 TRC-20 暂不作为账单收款或充值收款资产 | 配置 Tron 链 RPC / TronGrid，并启用对应资产 |

## 内置风控接入

Xcash 内置的是风控查询、缓存、记录和展示能力；当前风险地址识别依赖外部 MistTrack（慢雾 MistTrack）服务，并非项目内部自行维护黑名单或自研链上风控模型。

风控系统当前覆盖两类核心资金入口：

- **账单收款**：账单匹配到链上付款后，系统会对付款方地址进行异步风险查询，并将风险等级和风险分数同步到账单收款记录。
- **充值收款**：充值收款记录创建后，系统会对转入资金的来源地址进行异步风险查询，并将风险等级和风险分数同步到充值收款记录。

风险结果会同时写入独立的**风险评估**记录，包含查询状态、目标类型、来源地址、交易哈希、风险等级、风险分数。管理后台可直接查看账单收款、充值收款和风险评估记录中的风险信息，便于运营人员进行人工复核、业务放行或进一步处置。账单收款和充值收款的 API/Webhook 输出也会携带 `risk_level` 与 `risk_score`，方便商户系统同步展示或接入自己的处置流程。

Xcash 优先使用 MistTrack OpenAPI V3；未配置 MistTrack OpenAPI API Key 时，自动回退到 QuickNode MistTrack add-on。
两者都未配置，则不启用风控功能。

## 后台截图

![Xcash 管理后台 Dashboard](xcash/static/xcash-dashboard.jpeg)

## 架构

```mermaid
graph LR
    Buyer["买家<br/>账单收款页"]
    Merchant["商家系统"]

    subgraph Xcash
        API["Xcash API"]
        Worker["Xcash Worker<br/>交易监听 · 归集 · 状态流转"]
        Wallet["Xcash 钱包引擎<br/>助记词托管 · 地址派生 · 交易签名"]
        Webhook["Xcash Webhook<br/>异步通知"]
    end

    Blockchain["区块链网络<br/>EVM · Tron"]

    Buyer -->|发起账单收款| API
    Merchant <-->|创建账单收款 / 查询| API
    API <--> Worker
    Worker <--> Wallet
    Worker <-->|监听 · 广播| Blockchain
    Webhook -->|推送事件| Merchant
```

## 部署前准备

在开始部署之前，请准备以下条件：

- Linux 服务器，推荐 Ubuntu 22.04+ 或 Debian 12+
- Docker 和 Docker Compose
- 已解析到服务器 IP 的域名
- 需要启用的公链 RPC 节点
- 如需启用 Tron 账单收款，需要准备 TronGrid API Key

推荐服务器配置：

| 性能模式 | 硬件配置 | 可承载链数量 |
|:-------:|:-------:|:-----------:|
| low | 1 核 / 2 GB | 2 - 3 条 EVM 链 |
| medium | 4 核 / 8 GB | 8 - 15 条 EVM 链 |
| high | 8 核 / 16 GB | 15 - 30 条 EVM 链 |

`PERFORMANCE` 为可设置到 `.env` 中的性能参数，可选值为 `low`、`medium`、`high`。不设置时默认使用 `low`。

EVM 账单收款与充值收款都通过链上事件扫描感知和确认状态，且二者均默认启用、需要同时监听。实际可承载的链数量取决于 RPC 节点吞吐、区块出块速度和事件量，建议按上表保守配置性能档位。

## 快速开始

### 1. 克隆项目

```bash
git clone https://github.com/xca-sh/xcash.git
cd xcash
```

### 2. 初始化环境变量

```bash
./scripts/init_env.sh
```

该命令会生成 `.env`，并自动填充运行所需的随机密钥和数据库口令。
如果 `.env` 已存在，脚本会拒绝覆盖并退出；如需重新生成，请先手动备份并删除旧文件。

### 3. 设置访问域名

编辑 `.env` 设置 `SITE_DOMAIN`：

```env
SITE_DOMAIN=xcash.example.com
```

请确保该域名的 DNS 已解析到服务器 IP，并配置 Nginx 或 Caddy 等反向代理，将流量转发至 `http://localhost:6688`。

可选：设置 `ADMIN_PATH` 将后台入口移动到自定义路径，例如：

```env
ADMIN_PATH=secure-admin
```

未设置时后台仍挂在站点根路径，并会在后台右上角显示安全提醒。

### 4. 启动服务

```bash
docker compose up -d
```

启动脚本会先执行数据库迁移并补齐默认链、币种等主数据。首次启动时，如果数据库内还没有任何管理员账号，系统会自动创建默认后台账号：

```text
username: admin
password: Admin@123456
```

首次登录后台后请立即修改默认密码。

### 5. 配置链 RPC

系统已预置主流链的基础信息，但 **RPC 节点地址需要自行填写**，网关才能与区块链通信。

登录管理后台，进入 **区块链** **公链** 页面，为需要使用的链填写 RPC 地址。推荐使用 [QuickNode](https://www.quicknode.com/)、[Alchemy](https://www.alchemy.com/) 或 [Infura](https://www.infura.io/) 等节点服务商。Tron 账单收款需要在 [TronGrid](https://www.trongrid.io/) 注册并获取 API Key。

### 6. 为系统钱包充值 Gas

登录管理后台，进入 **系统** **系统钱包** 页面，复制系统钱包地址，并在每条启用的 EVM 链上向该地址充值少量原生资产用于支付 Gas，例如 ETH、BNB、POL 等。

系统钱包只用于平台基础设施交易，例如智能合约部署、智能合约归集等需要由系统主动发起的链上操作；业务收款资金仍按合约规则流向你的收款归集地址。这里不需要存入业务资金，只需要保留覆盖近期操作的小额 Gas，避免因 Gas 不足导致合约部署或归集任务无法广播。归集并非每笔收款各触发一次——系统通过归集延迟与金额门槛两道闸门批量归集，进一步压低 Gas 开销，详见[「Gas 成本与资金归集」](#gas-成本与资金归集)。

### 7. 配置项目

登录管理后台，进入 **项目** **项目列表** 页面，创建或编辑项目。项目是 API 对接的基本隔离单元，每个项目都有独立的 `Appid` 和 `HMAC密钥`，用于接口鉴权与签名。

请至少确认以下配置：

- **IP 白名单**：限制允许调用网关 API 的商户服务器 IP；测试阶段可使用 `*`，生产环境建议收窄到固定出口 IP 或网段。
- **通知地址**：用于接收账单收款、充值收款等 Webhook 事件；如暂未配置，项目会显示为未就绪。
- **收款归集地址**：业务资金最终流入的地址。启用智能合约收款或充值收款前必须配置 EVM 多签地址；该地址会写入智能合约规则，一旦设置不可修改。

## API 对接

部署完成后，参考 [API 对接文档](https://xca.sh/docs/#api-base) 接入账单收款、充值收款和 Webhook 回调。

创建账单收款时可传入账单收款级 `notify_url` 覆盖项目默认 Webhook；兼容易支付 V1 的 `submit.php` 入口也会将 `notify_url` 翻译为账单收款自身的通知地址。

## 运维命令

### 停止服务

```bash
docker compose down
```

该命令会停止并移除生产 Docker Compose 服务容器，不会删除数据库数据卷。

### 升级到最新版

```bash
./scripts/upgrade.sh
```

该命令会拉取 `main` 分支最新版并执行完整生产升级流程。

## 技术栈

- **后端**：Django 5.2 + Django REST Framework
- **任务队列**：Celery + Redis
- **数据库**：PostgreSQL
- **区块链交互**：web3.py（EVM）
- **钱包派生**：BIP44 HD 钱包（bip-utils）
- **前端账单收款页**：React 19 + Vite + Tailwind CSS
- **部署**：Docker Compose

## 路线图

- [ ] Solana 链支持
- [x] Tron 链支持
- [ ] 完善文档站

## 云服务

如果你不想自己部署和维护，可以直接使用官方托管版本：

**[xca.sh](https://xca.sh)** — 开箱即用，免部署，持续更新。

## 商业支持

如果你在部署或使用过程中需要专业协助，欢迎联系我们获取技术支持服务：

tech@xca.sh

## 贡献

欢迎提交 Issue 和 Pull Request。

## License

[MIT](LICENSE)
