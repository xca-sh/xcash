# Xcash API 对接文档

## 基础约定

- 所有路径**不带尾部 `/`**，按原样请求。
- 请求/响应体均为 JSON（EPay 入口除外）；金额一律为**字符串**，去除末尾多余的 0（如 `"29.9"`、`"100"`）。
- 时间为 ISO 8601 UTC，如 `2026-06-02T12:00:00.123456Z`。
- `chain` 用小写链 code，`crypto` 用大写币种 symbol，`currency` 用大写法币代码。

### 网关地址

| 部署方式 | API / 支付页 | EPay 入口 | 控制台 |
|------|------|------|------|
| 自托管 | `https://{SITE_DOMAIN}` | `https://{SITE_DOMAIN}/epay/submit.php` | `https://{SITE_DOMAIN}/{ADMIN_PATH}`（未设 `ADMIN_PATH` 时为根路径） |
| 官方托管 | `https://pay.xca.sh` | `https://pay.xca.sh/epay/submit.php` | `https://dash.xca.sh` |

### 链 code

| 链 | code | 类型 | 原生币 | Chain ID | 网络 |
|------|------|------|------|------|------|
| Ethereum | `ethereum` | EVM | `ETH` | `1` | 主网 |
| BNB Smart Chain | `bsc` | EVM | `BNB` | `56` | 主网 |
| Polygon PoS | `polygon` | EVM | `POL` | `137` | 主网 |
| Arbitrum One | `arbitrum-one` | EVM | `ETH` | `42161` | 主网 |
| Optimism | `optimism` | EVM | `ETH` | `10` | 主网 |
| Base | `base` | EVM | `ETH` | `8453` | 主网 |
| Avalanche C-Chain | `avalanche` | EVM | `AVAX` | `43114` | 主网 |
| Linea | `linea` | EVM | `ETH` | `59144` | 主网 |
| Scroll | `scroll` | EVM | `ETH` | `534352` | 主网 |
| Tron | `tron` | Tron | `TRX` | - | 主网 |
| Sepolia | `sepolia` | EVM | `ETH` | `11155111` | 测试网 |
| Nile | `nile` | Tron | `TRX` | - | 测试网 |
| Anvil Local | `anvil` | EVM | `ETH` | `31337` | 本地测试链 |

- 实际可用的链以后台启用为准，可通过 [`GET /v1/metadata`](#链币元数据) 查询。
- **测试项目只能用测试网/本地链，非测试项目只能用主网**，不匹配的链对该项目不可用。

### 币种与法币

- 系统内置币种：`USDT`、`USDC`、`DAI` 及各链原生币 `ETH`、`BNB`、`POL`、`TRX`。某链上是否可用取决于后台启用的「币 × 链」关系。
- **Tron 仅支持 `USDT` 与原生 `TRX`**（账单与充值均如此）。
- 内置法币（`currency`）：`USD`、`CNY`、`EUR`、`JPY`、`HKD`，区分大小写。

## 认证

所有 `/v1/*` 接口都需要 HMAC 签名，**以下公开端点除外**：`GET /v1/invoice/{sys_no}`、`POST /v1/invoice/{sys_no}/select-method`、`GET /v1/metadata`。

### 凭证

在控制台创建项目后获得：

| 字段 | 说明 |
|------|------|
| `appid` | 项目标识，格式 `XC-` + 8 位大写字母数字，如 `XC-A3BK7NMG` |
| `hmac_key` | 签名密钥，32 位 |

### 请求头

| Header | 说明 |
|------|------|
| `XC-Appid` | 项目 AppID |
| `XC-Timestamp` | 当前 Unix 时间戳（秒），与服务器时间相差不得超过 300 秒 |
| `XC-Nonce` | 随机串，必填；同一 AppID 下 300 秒内不可重复 |
| `XC-Signature` | 签名，小写十六进制 |
| `Content-Type` | `application/json` |

### 签名算法

```text
message   = XC-Nonce + XC-Timestamp + request_body
signature = hex( HMAC-SHA256(key = hmac_key, msg = message) )
```

- `request_body` 是实际发送的原始请求体字符串，签名后不得再重新序列化。
- GET 请求 `request_body` 为空字符串 `""`，**查询参数不参与签名**。

```python
import hashlib, hmac, json, time, uuid
import requests

appid, hmac_key = "XC-A3BK7NMG", "your_hmac_key"
body = json.dumps({"out_no": "order-001", "title": "Premium", "currency": "USD", "amount": "29.99"},
                  separators=(",", ":"), ensure_ascii=False)
timestamp = str(int(time.time()))
nonce = uuid.uuid4().hex
signature = hmac.new(hmac_key.encode(), f"{nonce}{timestamp}{body}".encode(), hashlib.sha256).hexdigest()
headers = {"XC-Appid": appid, "XC-Timestamp": timestamp, "XC-Nonce": nonce,
           "XC-Signature": signature, "Content-Type": "application/json"}
# 发送的就是上面签名用的 body 字符串，encode 只是转成 UTF-8 字节（含中文时 requests 无法直接发送 str）
# 不要用 json=payload：requests 会重新序列化（空格、中文转义都不同），导致验签失败
requests.post(url, data=body.encode("utf-8"), headers=headers)
```

```javascript
const crypto = require("crypto");
const hmacKey = "your_hmac_key";
const body = JSON.stringify({ out_no: "order-001", title: "Premium", currency: "USD", amount: "29.99" });
const timestamp = Math.floor(Date.now() / 1000).toString();
const nonce = crypto.randomUUID();
const signature = crypto.createHmac("sha256", hmacKey).update(`${nonce}${timestamp}${body}`).digest("hex");
```

### 校验顺序

签名接口按以下顺序校验，命中即返回：

`1001` AppID 无效 → `1005` 项目已停用 → `1008` 时间戳缺失或超窗 → `1004` 项目未就绪 → `1002` IP 不在白名单 → `1003` 签名错误 → `1000` 缺少 Nonce → `1009` Nonce 重复

**项目就绪条件**（不满足返回 `1004`，`detail` 列出缺失项）：

1. 至少有一个启用的钱包直收地址，或已配置 EVM / Tron 归集地址；
2. 某链类型的账单收款模式为「智能合约」时，必须配置该链类型的归集地址；
3. IP 白名单非空（默认 `*` 放行全部）；
4. 项目通知地址（Webhook）非空。

## 响应格式

成功直接返回业务 JSON。业务错误统一为：

```json
{ "code": "1001", "message": "AppID无效", "detail": "" }
```

- 以 `code` 判断错误类型；`message` 会随 `Accept-Language` 本地化，不要依赖其文本。
- `detail` 为补充信息，可能是字符串或对象（参数错误时为各字段的错误详情）。
- 资源不存在 `404`、方法不允许 `405`、限流 `429` 等框架级错误为 `{"detail": "..."}`。

## 接口列表

| 方法 | 路径 | 说明 | 签名 | 限流 |
|------|------|------|------|------|
| `POST` | `/v1/invoice` | 创建账单 | 需要 | 256/分钟，按 IP |
| `GET` | `/v1/invoice/{sys_no}` | 查询账单 | 不需要 | 120/分钟，按 sys_no + IP |
| `POST` | `/v1/invoice/{sys_no}/select-method` | 选择/切换支付方式 | 不需要 | 10/分钟，按 sys_no + IP |
| `GET` | `/v1/deposit/address` | 获取充值地址 | 需要 | 60/分钟，按 appid + IP |
| `GET` | `/v1/metadata` | 链/币元数据 | 不需要 | 256/分钟，按 IP |
| `GET` / `POST` | `/epay/submit.php` | 易支付 V1 下单 | EPay MD5 | 60/分钟，按 IP |

## 创建账单

`POST /v1/invoice`

创建一笔按法币计价的账单，返回支付页 `pay_url`，买家在支付页选币、选链并付款。

### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `out_no` | string | 是 | 商户订单号，最长 32 位，项目内唯一（与 EPay 的 `out_trade_no` 共用命名空间） |
| `title` | string | 是 | 标题，最长 32 位 |
| `currency` | string | 是 | 计价法币，如 `USD`、`CNY` |
| `amount` | string | 是 | 计价金额，`0.01` ~ `1000000`，最多 2 位小数（如 `"10.000"` 视为 3 位，会被拒绝） |
| `duration` | integer | 否 | 有效期（分钟），`5` ~ `30`，默认 `10` |
| `methods` | object | 否 | 限定支付方式，格式 `{"币种": ["链 code", ...]}`；不传则使用项目全部可用组合 |
| `notify_url` | string | 否 | 本账单的 Webhook 地址，优先于项目通知地址 |
| `return_url` | string | 否 | 支付完成后支付页的跳转地址 |

```json
{
  "out_no": "order-20260602-001",
  "title": "Premium Plan",
  "currency": "USD",
  "amount": "29.99",
  "duration": 15,
  "methods": { "USDT": ["ethereum", "tron"], "USDC": ["base"] },
  "notify_url": "https://merchant.example.com/xcash/webhook",
  "return_url": "https://merchant.example.com/payment/success"
}
```

### 响应（HTTP 201）

```json
{
  "appid": "XC-A3BK7NMG",
  "sys_no": "INV2606028X7K2P9Q",
  "out_no": "order-20260602-001",
  "title": "Premium Plan",
  "currency": "USD",
  "amount": "29.99",
  "methods": { "USDT": ["ethereum", "tron"], "USDC": ["base"] },
  "chain": null,
  "crypto": null,
  "crypto_address": null,
  "pay_address": null,
  "pay_amount": null,
  "pay_url": "https://pay.xca.sh/pay/INV2606028X7K2P9Q",
  "started_at": "2026-06-02T12:00:00.123456Z",
  "created_at": "2026-06-02T12:00:00.123456Z",
  "expires_at": "2026-06-02T12:15:00.123456Z",
  "notify_url": "https://merchant.example.com/xcash/webhook",
  "return_url": "https://merchant.example.com/payment/success",
  "payment": null,
  "status": "waiting",
  "risk_level": null,
  "risk_score": null
}
```

字段含义同[查询账单](#查询账单)；未传的 `notify_url` / `return_url` 返回空字符串 `""`。

### 规则

- **幂等**：`out_no` 已存在时，若 `title`、`currency`、`amount`、`notify_url`、`return_url` 与原账单完全一致，返回原账单（HTTP `200`）；否则返回 `1007`。
- **methods**：必须是项目可用组合的子集，否则返回 `5008`；未知币种返回 `2001`，未知链返回 `2000`。响应中的 `methods` 是最终生效的组合。
- **自动选择**：最终只剩一个「币 × 链」组合时，创建时即自动分配，响应中 `chain`、`crypto`、`pay_address`、`pay_amount` 已有值。
- **待支付上限**：项目 `waiting` 状态账单达到 100 笔时，拒绝新建（`5009`）。

### 收款模式与付款金额

每个项目按链类型（EVM / Tron）分别设置账单收款模式，默认「钱包直收」：

| 模式 | 收款地址 | `pay_amount` | 限制 |
|------|------|------|------|
| 钱包直收 | 项目配置的钱包地址（多账单共用） | 按汇率换算后**向上取整到 0.01**；该金额在所有钱包地址上都被待支付账单占用时，再加 0.01 的整数倍（最多 +0.99）以区分账单 | EVM 链不支持原生币 |
| 智能合约 | 系统分配的合约地址，确认后自动归集到项目归集地址 | 按汇率换算，向上取整到币种精度 | 需配置对应链类型的归集地址 |

账单入账的匹配条件（全部满足才算支付成功）：

1. 转入当前的 `pay_address`，链与币种与当前 `chain`、`crypto` 一致；
2. **到账金额与 `pay_amount` 完全相等**，多付、少付均不匹配；
3. 交易时间在 `started_at` ~ `expires_at` 之间。

买家切换支付方式后，旧的地址/金额立即失效。请始终以接口返回的 `chain`、`crypto`、`pay_address`、`pay_amount` 为准，不要自行按 `amount` 推算。

## 查询账单

`GET /v1/invoice/{sys_no}`

公开接口，无需签名，供支付页或买家侧轮询。不返回 `appid`、`out_no`、`notify_url`。不存在时返回 `404`。

| 字段 | 类型 | 说明 |
|------|------|------|
| `sys_no` | string | 系统单号：`INV` + 6 位日期(YYMMDD) + 8 位大写字母数字 |
| `title` | string | 标题 |
| `currency` | string | 计价法币 |
| `amount` | string | 计价金额 |
| `methods` | object | 可选支付方式 |
| `chain` | string \| null | 当前链，未选择时为 `null` |
| `crypto` | string \| null | 当前币种 |
| `crypto_address` | string \| null | 币种在该链的合约地址；原生币为 `""`；未选择时为 `null` |
| `pay_address` | string \| null | 收款地址 |
| `pay_amount` | string \| null | 买家应付的加密货币数量 |
| `pay_url` | string | 支付页地址 |
| `started_at` | string | 当前支付指引的生效时间；切换支付方式时更新 |
| `created_at` | string | 创建时间 |
| `expires_at` | string | 过期时间 |
| `return_url` | string | 跳转地址，未设置为 `""`；EPay 订单完成后为带签名参数的跳转 URL |
| `payment` | object \| null | 已匹配的链上转账，见下表 |
| `payment_uri` | string \| null | EVM 链的 EIP-681 支付 URI；非 EVM 或金额无法精确编码时为 `null` |
| `evm_payment` | object \| null | EVM 钱包一键支付参数 `{chain_id, to, value, data}`；不适用时为 `null` |
| `tron_payment` | object \| null | TronLink 一键支付参数 `{is_native, to, contract, amount, fee_limit, is_testnet}`；不适用时为 `null` |
| `status` | string | `waiting` / `completed` / `expired` |
| `risk_level` | string \| null | AML 风险等级：`Low` / `Moderate` / `High` / `Severe` |
| `risk_score` | string \| null | AML 风险分数 |

`payment` 对象：

| 字段 | 类型 | 说明 |
|------|------|------|
| `chain` | string | 链 code |
| `block` | integer | 区块高度 |
| `hash` | string | 交易哈希 |
| `from_address` | string | 付款地址 |
| `to_address` | string | 收款地址 |
| `crypto` | string | 币种 |
| `amount` | string | 到账金额 |
| `datetime` | string | 交易时间 |
| `status` | string | `confirming` / `confirmed` |
| `confirm_progress` | object | `{has_confirmed_count, need_confirmed_count, progress}`，`progress` 为 0~100 的整数 |

**状态说明**：

- `waiting`：待支付。链上已出现匹配转账但未确认时，`payment` 有值、`status` 仍为 `waiting`。
- `completed`：已确认，终态。
- `expired`：已过期。**不是终态**：过期前已上链的付款在过期后确认，账单仍会转为 `completed` 并发送 Webhook。
- 账单美元价值低于项目「快速确认阈值」（默认 10 USD）时，观察到付款即确认，不等待区块确认数。

## 选择支付方式

`POST /v1/invoice/{sys_no}/select-method`

公开接口，无需签名。为账单分配或切换支付方式，内置支付页即调用此接口；自建收银台时可使用。

```json
{ "crypto": "USDT", "chain": "tron" }
```

- 成功返回 HTTP `200`，响应体同[查询账单](#查询账单)。
- 选择与当前相同的方式时直接返回现有指引；切换时重新分配 `pay_address` / `pay_amount` 并刷新 `started_at`。
- 错误：`5006` 账单非 `waiting` 或已匹配到付款；`5012` 已过期；`2001` / `2000` 币种或链不在该账单 `methods` 内；`2002` 该链不支持此币种；`5008` 收款地址分配失败；`1000` 参数错误或暂无报价。

## 获取充值地址

`GET /v1/deposit/address?uid={uid}&chain={chain}&crypto={crypto}`

需要签名（`request_body` 为空字符串）。为终端用户获取专属充值地址（智能合约地址）。

| 参数 | 必填 | 说明 |
|------|------|------|
| `uid` | 是 | 终端用户标识，1~128 位，仅限字母、数字、`_`、`-` |
| `chain` | 是 | 链 code |
| `crypto` | 是 | 币种 symbol |

```json
{ "deposit_address": "0xAbCd...1234" }
```

- 同一项目、同一 `uid`、同一链的地址固定不变，**与 `crypto` 无关**（同链各币种共用一个地址）。
- 支持 EVM 链与 Tron（Tron 仅 `USDT`、`TRX`）。需配置对应链类型的归集地址（否则 `4001`）。
- **EVM 原生币（ETH/BNB/POL 等）**：
  - 必须以 `crypto=原生币` 调用过本接口，系统才会部署该地址的合约；合约部署前到账的原生币，在部署完成后才入账。
  - 充值地址是合约，转账 gas limit 必须高于 21000（由钱包自动估算即可），写死 21000 的转账会失败。
- 错误：`4000` uid 非法；`2000` 链不存在/未启用/与项目测试属性不符/不支持该币种充值；`2001` 币种不存在或未启用；`2002` 该币未在此链启用；`4001` 未配置归集地址；`4002` 充值用户数达到套餐上限（仅官方托管版）。

## 链币元数据

`GET /v1/metadata`

公开接口，返回当前启用的链与币种（不区分项目的主网/测试网属性）。

```json
{
  "chains": [{ "code": "ethereum", "name": "Ethereum", "icon": "https://...", "is_testnet": false }],
  "cryptos": [{ "symbol": "USDT", "name": "Tether", "icon": "https://...", "is_native": false }]
}
```

## Webhook

### 投递规则

| 事件 | 投递地址 | 方式 | 成功响应 |
|------|------|------|------|
| 账单完成（原生协议） | 账单 `notify_url`，为空时用项目通知地址 | `POST` JSON + HMAC 头 | HTTP `200` 且响应体为 `ok` |
| 充值到账 | 项目通知地址 | `POST` JSON + HMAC 头 | HTTP `200` 且响应体为 `ok` |
| EPay 订单完成 | 下单时的 `notify_url` | `GET` query + MD5 签名 | HTTP `200` 且响应体为 `success` |

- 响应体会去除首尾空白后精确比较，区分大小写。单次请求超时 5 秒。
- **可重试**：网络错误/超时、`5xx`、`408`、`425`、`429`。**其余情况直接判定失败、不再重试**，包括 `200` 但响应体不符、非 `200` 的 `2xx`、`3xx`、其他 `4xx`。
- 默认最多投递 5 次（含首次），指数退避约 4s / 8s / 16s / 32s（单次上限 120s）；次数与上限可在后台「系统参数」调整；失败事件可在后台 Webhook 事件列表中重新投递。
- 项目通知开关关闭或投递地址为空时，事件挂起，每 10 分钟重试一次，同样计入投递次数。
- 默认只投递到 HTTPS 公网地址；`http`、`localhost`、内网地址直接判定失败（自托管可用 `WEBHOOK_ALLOW_INTERNAL_TARGETS=true` 放开）。
- 重试窗口很短，账单状态请以[查询账单](#查询账单)接口兜底对账。

### 签名校验

请求头为 `XC-Appid`、`XC-Nonce`、`XC-Timestamp`、`XC-Signature`，算法与 API 请求相同：

```python
expected = hmac.new(hmac_key.encode(), f"{nonce}{timestamp}{raw_body}".encode(), hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, signature)
```

- `raw_body` 必须是收到的原始请求体，不要解析后再序列化（JSON 字段顺序、空格都不保证）。
- `XC-Nonce` 在同一事件的多次重试中保持不变，请用它做幂等去重。`XC-Timestamp` 为每次投递的发送时间。

### 账单完成

账单变为 `completed` 时发送一次。

```json
{
  "type": "invoice",
  "data": {
    "sys_no": "INV2606028X7K2P9Q",
    "out_no": "order-20260602-001",
    "crypto": "USDT",
    "chain": "ethereum",
    "pay_address": "0xAbCd...1234",
    "pay_amount": "29.99",
    "hash": "0xabc123...",
    "block": 12345678,
    "confirmed": true,
    "risk_level": null,
    "risk_score": null
  }
}
```

### 充值到账

充值转账达到确认要求后发送一次（`confirmed` 恒为 `true`）。后台已停用的币种到账只入账、不发送 Webhook。

```json
{
  "type": "deposit",
  "data": {
    "sys_no": "DXC2606026K9P2QWX",
    "uid": "user-10001",
    "chain": "base",
    "block": 12345678,
    "hash": "0xabc123...",
    "crypto": "USDC",
    "amount": "500",
    "confirmed": true,
    "risk_level": null,
    "risk_score": null
  }
}
```

> `risk_level` / `risk_score` 是发送时的快照。AML 筛查在确认后异步执行，因此 Webhook 中通常为 `null`，且筛查完成后**不会补发通知**。账单的最新风控结果可通过查询账单接口获取。

## 易支付 V1 兼容

兼容常见 Typecho、WordPress、Discuz 等易支付插件。不使用 HMAC 头，使用 EPay 自有 MD5 签名。

### 商户身份

| 字段 | 说明 |
|------|------|
| `pid` | EPay 商户 ID |
| `secret_key` | MD5 签名密钥（独立于 `hmac_key`） |
| `active` | 是否启用 EPay 入口 |

- 官方托管版：创建项目时自动分配，在控制台查看。
- 自托管：在后台项目页的「EPay 配置」标签中添加。

### 下单

`GET /epay/submit.php` 或 `POST /epay/submit.php`（POST 用表单编码，不要发 JSON）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pid` | integer | 是 | EPay 商户 ID |
| `type` | string | 否 | 兼容字段，最长 32 位；不限定链或币种，仅在通知中原样回传 |
| `out_trade_no` | string | 是 | 商户订单号，最长 64 位，项目内唯一（与 `/v1/invoice` 的 `out_no` 共用命名空间） |
| `notify_url` | string | 是 | 异步通知地址 |
| `return_url` | string | 否 | 同步跳转地址 |
| `name` | string | 是 | 商品名称，最长 128 位 |
| `money` | string | 是 | 金额，`0.01` ~ `1000000`，最多两位小数 |
| `currency` | string | 否 | Xcash 扩展：计价法币，默认 `CNY`，不区分大小写 |
| `param` | string | 否 | 扩展参数，最长 512 位 |
| `sign` | string | 是 | MD5 签名（小写） |
| `sign_type` | string | 是 | 固定 `MD5` |

- 成功：HTTP `302` 跳转到支付页 `/pay/{sys_no}`；失败：HTTP `400`，纯文本 `fail`（不返回具体原因）；限流：HTTP `429`。
- 账单有效期固定 15 分钟，支付方式为项目全部可用组合。
- 相同 `out_trade_no` 重复提交时，若 `type`、`name`、`money`、`currency`、`notify_url`、`return_url`、`param` 均一致，返回同一账单；否则失败。

### 签名算法

1. 取所有请求参数，去掉 `sign`、`sign_type` 以及值为空的字段；
2. 按参数名 ASCII 升序排序，拼成 `k1=v1&k2=v2`；
3. 末尾直接拼接 `secret_key`，取 MD5 小写十六进制。

```python
import hashlib

def epay_sign(params: dict, secret_key: str) -> str:
    items = sorted((k, str(v)) for k, v in params.items()
                   if k not in ("sign", "sign_type") and v not in (None, ""))
    raw = "&".join(f"{k}={v}" for k, v in items) + secret_key
    return hashlib.md5(raw.encode("utf-8")).hexdigest()
```

### 异步通知

账单完成后，向 `notify_url` 发送 GET 请求，query 参数如下（签名算法同上，商户需验签）：

| 字段 | 说明 |
|------|------|
| `pid` | EPay 商户 ID |
| `trade_no` | Xcash 系统单号（`sys_no`） |
| `out_trade_no` | 商户订单号 |
| `type` | 下单时的 `type`，未传为空 |
| `name` | 商品名称 |
| `money` | 订单金额，两位小数 |
| `trade_status` | 固定 `TRADE_SUCCESS` |
| `param` | 下单时的 `param`，有值才返回 |
| `sign_type` | `MD5` |
| `sign` | MD5 签名 |

商户返回 HTTP `200` 且响应体为 `success` 视为成功，重试规则见 [Webhook](#投递规则)。

### 同步跳转

账单完成后，若下单传了 `return_url`，查询账单接口返回的 `return_url` 为附带上表参数及签名的跳转 URL，支付页据此跳回商户。同步跳转仅表示支付页流程结束，发货请以异步通知为准。

## 错误码

| 错误码 | HTTP | 说明 |
|------|------|------|
| `1000` | 400 | 参数错误 |
| `1001` | 400 | AppID 无效 |
| `1002` | 403 | IP 不在白名单 |
| `1003` | 403 | 签名错误 |
| `1004` | 400 | 项目未就绪（`detail` 列出缺失配置） |
| `1005` | 403 | 项目已停用 |
| `1007` | 400 | `out_no` 已存在且参数不一致 |
| `1008` | 400 | 时间戳缺失或超出 300 秒 |
| `1009` | 400 | Nonce 重复 |
| `2000` | 400 | 无效链 |
| `2001` | 400 | 无效币种 |
| `2002` | 400 | 该链不支持此币种 |
| `4000` | 400 | 无效 uid |
| `4001` | 400 | 项目未配置该链类型的归集地址 |
| `4002` | 403 | 充值用户数达到套餐上限（官方托管版） |
| `5000` | 400 | 不支持的计价法币 |
| `5006` | 400 | 账单状态不允许此操作 |
| `5008` | 400 | 无可用支付方式 |
| `5009` | 400 | 待支付账单过多 |
| `5012` | 400 | 账单已过期 |
| `6004` | 403 | 账户已冻结（官方托管版） |

## 对接流程

**账单**：商户 `POST /v1/invoice` → 引导买家打开 `pay_url` → 买家选币选链并付款 → Xcash 推送 `invoice` Webhook → 商户返回 `ok` → 商户发货（可用 `GET /v1/invoice/{sys_no}` 兜底对账）。

**充值**：商户 `GET /v1/deposit/address` → 向用户展示 `deposit_address` → 用户转账 → Xcash 推送 `deposit` Webhook → 商户返回 `ok` → 商户上账（按 `XC-Nonce` 或 `sys_no` 去重）。
