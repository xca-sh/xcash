# Xcash API 对接文档

本文档描述 Xcash 当前公开对接接口。所有 Django/DRF API 路由均**不带尾部 `/`**，示例中的路径请按原样请求。

## 网关地址

### 自托管部署

如果你已按 [README](README.md) 完成自托管部署，API 网关地址即为 `.env` 中配置的 `SITE_DOMAIN`：

| 用途 | URL | 说明 |
|------|-----|------|
| API 网关 | `https://{你的域名}` | 例如 `https://{你的域名}/v1/invoice` |
| 管理后台 | `https://{你的域名}` | 创建项目、配置链 RPC、配置 Webhook、查看账单收款与充值收款记录 |

### Xcash 官方服务

如果你使用 Xcash 官方托管版本（[xca.sh](https://xca.sh)），请使用：

| 用途 | URL | 说明 |
|------|-----|------|
| API 网关 | `https://pay.xca.sh` | 例如 `https://pay.xca.sh/v1/invoice` |
| EPay 网关 | `https://pay.xca.sh/epay/submit.php` | 易支付 V1 兼容入口 |
| SaaS 控制台 | `https://dash.xca.sh` | 获取 AppID / HMAC Key、配置项目 |

## 链与币种代码

### Chain Code

当前公开接口使用以下链 code：

| Chain | code | 类型 | Gas 代币 | Chain ID |
|------|------|------|----------|----------|
| Ethereum | `ethereum` | EVM | `ETH` | `1` |
| BNB Smart Chain | `bsc` | EVM | `BNB` | `56` |
| Polygon PoS | `polygon` | EVM | `POL` | `137` |
| Arbitrum One | `arbitrum-one` | EVM | `ETH` | `42161` |
| Optimism | `optimism` | EVM | `ETH` | `10` |
| Base | `base` | EVM | `ETH` | `8453` |
| Tron | `tron` | Tron | `TRX` | - |
| Sepolia | `sepolia` | EVM 测试网 | `ETH` | `11155111` |
| Nile | `nile` | Tron 测试网 | `TRX` | - |
| Anvil Local | `anvil` | EVM 本地测试链 | `ETH` | `31337` |

测试项目只能使用测试网或本地测试链（如 `sepolia`、`nile`、`anvil`），非测试项目只能使用主网链。

### Crypto Symbol

`crypto` 使用币种 symbol，例如 `USDT`、`USDC`、`DAI`、`ETH`、`BNB`、`POL`、`TRX`。实际可用组合取决于后台启用的链、币种和链上部署关系。

## 认证机制

除明确标注为公开接口的端点外，`/v1/*` 接口都需要 HMAC-SHA256 签名。

### 凭证

在管理后台创建项目后，系统生成：

| 字段 | 说明 |
|------|------|
| `appid` | 项目唯一标识，例如 `XC-A3BK7NMG` |
| `hmac_key` | 项目 HMAC 签名密钥 |

项目至少需要配置 `IP 白名单`、`通知地址`，并具备可用收款地址配置（钱包直收地址或对应链类型的智能合约归集地址），才能通过公开 API 的项目就绪检查。若项目把某链的账单收款模式设为智能合约，则必须配置该链类型的归集地址。

### 请求头

```http
XC-Appid: {appid}
XC-Timestamp: {unix_timestamp}
XC-Nonce: {unique_nonce}
XC-Signature: {hmac_signature}
Content-Type: application/json
```

| Header | 说明 |
|--------|------|
| `XC-Appid` | 项目 AppID |
| `XC-Timestamp` | 当前 Unix 时间戳，生产环境允许与服务器相差 300 秒 |
| `XC-Nonce` | 同一 AppID 下 300 秒内不可重复 |
| `XC-Signature` | HMAC-SHA256 签名，小写十六进制 |

### 签名计算

```text
message   = XC-Nonce + XC-Timestamp + request_body
signature = HMAC-SHA256(message, hmac_key).hexdigest()
```

`request_body` 必须是实际发送的原始请求体字符串。GET 请求没有 body 时使用空字符串 `""`。

Python 示例：

```python
import hashlib
import hmac
import json
import time
import uuid

appid = "XC-A3BK7NMG"
hmac_key = "your_hmac_key"

payload = {
    "out_no": "order-001",
    "title": "Premium Plan",
    "currency": "USD",
    "amount": "29.99",
}
body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
timestamp = str(int(time.time()))
nonce = str(uuid.uuid4())
signature = hmac.new(
    hmac_key.encode(),
    f"{nonce}{timestamp}{body}".encode(),
    hashlib.sha256,
).hexdigest()

headers = {
    "XC-Appid": appid,
    "XC-Timestamp": timestamp,
    "XC-Nonce": nonce,
    "XC-Signature": signature,
    "Content-Type": "application/json",
}
```

Node.js 示例：

```javascript
const crypto = require("crypto");

const appid = "XC-A3BK7NMG";
const hmacKey = "your_hmac_key";
const body = JSON.stringify({
  out_no: "order-001",
  title: "Premium Plan",
  currency: "USD",
  amount: "29.99",
});
const timestamp = Math.floor(Date.now() / 1000).toString();
const nonce = crypto.randomUUID();
const signature = crypto
  .createHmac("sha256", hmacKey)
  .update(`${nonce}${timestamp}${body}`)
  .digest("hex");
```

## 响应格式

成功时直接返回业务 JSON。创建类接口通常返回 HTTP `201`，查询类接口返回 HTTP `200`。

业务错误响应：

```json
{
  "code": "1001",
  "message": "AppID无效",
  "detail": ""
}
```

框架级错误（例如不存在的资源 `404`、请求方法错误 `405`、限流 `429`）可能返回 DRF 默认格式：

```json
{
  "detail": "Not found."
}
```

## 接口列表

| 方法 | 路径 | 说明 | 签名 |
|------|------|------|------|
| `POST` | `/v1/invoice` | 创建账单收款 | 需要 |
| `GET` | `/v1/invoice/{sys_no}` | 查询账单收款公开状态 | 不需要 |
| `GET` | `/v1/deposit/address` | 获取充值收款地址 | 需要 |
| `GET` / `POST` | `/epay/submit.php` | 易支付 V1 创建订单 | EPay MD5 签名 |

## 创建账单收款

`POST /v1/invoice`

创建一笔账单收款。创建成功后返回 `pay_url`，买家打开账单收款页完成选币、选链和付款。

### 请求参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `out_no` | string | 是 | 商户订单号，最长 32 位，同一项目内唯一 |
| `title` | string | 是 | 账单收款标题，最长 32 位 |
| `currency` | string | 是 | 计价法币代码（如 `USD`、`CNY`），必须是系统支持的法币；收款加密货币由 `methods` 指定 |
| `amount` | string | 是 | 计价金额，范围 `0.00000001` 到 `1000000` |
| `duration` | integer | 否 | 有效期分钟数，范围 `5` 到 `30`，默认 `10` |
| `methods` | object | 否 | 限定账单收款方式，格式 `{"币种": ["链码"]}` |
| `notify_url` | string | 否 | 账单收款级 Webhook 地址，优先于项目默认通知地址 |
| `return_url` | string | 否 | 账单收款完成后的同步跳转地址 |

### methods 生成规则

`methods` 是项目级可收款能力的收敛条件：

- 不传 `methods`：系统按项目配置生成当前项目可用的链币组合。
- 传入 `methods`：必须是系统生成组合的子集，否则返回无可用账单收款方式。
- `currency` 仅决定 `amount` 的计价单位（法币），与买家实际支付的加密货币解耦——后者由 `methods` 限定。
- 买家最终应支付的链、币、地址与数量以账单页/查询接口返回的 `chain`、`crypto`、`pay_address`、`pay_amount` 为准；不要自行按 `amount` 推导链上付款数量。
- `crypto` symbol 使用大写，`chain` code 使用上方表格中的小写 code。

### 收款模式与链支持

- 账单收款按链类型支持两种模式：钱包直收（Differ）和智能合约收款（VaultSlot）。
- 钱包直收模式下，系统使用项目配置的钱包直收地址，并可能通过微调 `pay_amount` 区分同地址上的不同账单。
- 智能合约收款模式下，系统为账单分配 VaultSlot 地址；确认后会调度归集，资金最终流入项目配置的对应链类型归集地址。
- EVM 与 Tron 均可用于账单收款；实际可用链币组合取决于后台启用状态、链上币种关系、项目收款模式和归集地址配置。
- EVM 归集地址必须是 checksum 地址，Tron 归集地址必须是 Base58 地址。
- 使用智能合约收款时，系统钱包需要在对应链保留少量 Gas / Energy，用于合约部署和归集交易广播。

### 请求示例

```json
{
  "out_no": "order-20260602-001",
  "title": "Premium Plan",
  "currency": "USD",
  "amount": "29.99",
  "duration": 15,
  "methods": {
    "USDT": ["ethereum"],
    "USDC": ["base"]
  },
  "notify_url": "https://merchant.example.com/xcash/webhook",
  "return_url": "https://merchant.example.com/payment/success"
}
```

限定只允许买家使用某种稳定币示例（USD 计价 + `methods` 限定 USDT）。最终链上付款数量仍以返回的 `pay_amount` 为准：

```json
{
  "out_no": "order-20260602-002",
  "title": "Contract Invoice",
  "currency": "USD",
  "amount": "100",
  "duration": 15,
  "methods": {
    "USDT": ["ethereum", "base"]
  },
  "notify_url": "https://merchant.example.com/xcash/webhook"
}
```

### 响应示例

```json
{
  "appid": "XC-A3BK7NMG",
  "sys_no": "INV2606028X7K2P9Q",
  "out_no": "order-20260602-001",
  "title": "Premium Plan",
  "currency": "USD",
  "amount": "29.99",
  "methods": {
    "USDT": ["ethereum"],
    "USDC": ["base"]
  },
  "chain": null,
  "crypto": null,
  "crypto_address": null,
  "pay_address": null,
  "pay_amount": null,
  "pay_url": "https://pay.xca.sh/pay/INV2606028X7K2P9Q",
  "started_at": "2026-06-02T12:00:00Z",
  "created_at": "2026-06-02T12:00:00Z",
  "expires_at": "2026-06-02T12:15:00Z",
  "notify_url": "https://merchant.example.com/xcash/webhook",
  "return_url": "https://merchant.example.com/payment/success",
  "payment": null,
  "status": "waiting",
  "risk_level": null,
  "risk_score": null
}
```

如果最终只剩一个账单收款组合，系统会在创建时自动选择该方式，此时 `chain`、`crypto`、`pay_address`、`pay_amount` 可能已返回具体值。

### 限流

默认匿名限流：`256/minute`。

## 查询账单收款

`GET /v1/invoice/{sys_no}`

公开接口，无需签名。该接口用于账单收款页或买家侧轮询账单收款状态，不返回 `appid`、`out_no`、`notify_url`。

### 响应字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `sys_no` | string | 系统账单收款号；格式为前缀 + 6 位日期(YYMMDD) + 8 位大写字母数字，账单收款前缀 `INV`、充值收款前缀 `DXC` |
| `title` | string | 标题 |
| `currency` | string | 计价币种 |
| `amount` | string | 计价金额 |
| `methods` | object | 可选账单收款方式 |
| `chain` | string \| null | 已选链 |
| `crypto` | string \| null | 已选币种 |
| `crypto_address` | string \| null | 币种在该链上的合约地址；Gas 代币通常为空 |
| `pay_address` | string \| null | 账单收款地址 |
| `pay_amount` | string \| null | 买家应付加密货币数量 |
| `pay_url` | string | 账单收款页地址 |
| `started_at` | string | 账单创建时间；买家切换支付方式后会更新为当前支付指引分配时间 |
| `created_at` | string | 创建时间 |
| `expires_at` | string | 过期时间 |
| `return_url` | string \| null | 同步跳转地址 |
| `payment` | object \| null | 匹配到的链上转账 |
| `payment_uri` | string \| null | EVM 链可用的 EIP-681 支付 URI；非 EVM 或无法精确编码金额时为空 |
| `status` | string | `waiting` / `completed` / `expired` |
| `risk_level` | string \| null | 风险等级 |
| `risk_score` | string \| null | 风险分数 |

`payment` 对象字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `chain` | string | 链 code |
| `block` | integer | 区块高度 |
| `hash` | string | 交易哈希 |
| `from_address` | string | 付款地址 |
| `to_address` | string | 收款地址 |
| `crypto` | string | 币种 |
| `amount` | string | 链上到账金额 |
| `datetime` | string | 交易时间 |
| `status` | string | 转账状态 |
| `confirm_progress` | object | 确认进度，包含 `has_confirmed_count`、`need_confirmed_count`、`progress` |

### 限流

`60/minute`，按 `sys_no + IP` 维度。

## 获取充值收款地址

`GET /v1/deposit/address`

需要 HMAC 签名。为项目下的终端客户获取充值收款地址。同一项目、同一 `uid`、同一链会稳定返回同一个智能合约收款地址。

### 查询参数

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `uid` | string | 是 | 终端客户标识，1 到 128 位，只允许字母、数字、下划线和中划线 |
| `chain` | string | 是 | 链 code，如 `ethereum`、`base`、`tron` |
| `crypto` | string | 是 | 币种 symbol，如 `USDT` |

请求示例：

```text
GET /v1/deposit/address?uid=user-10001&chain=base&crypto=USDC
```

GET 请求签名时 `request_body` 为空字符串。

响应示例：

```json
{
  "deposit_address": "0xAbCd1234..."
}
```

注意事项：

- 当前充值收款地址接口支持 EVM 与 Tron；Tron 当前开放 USDT 与原生 TRX。
- 请求的链、币种和链上币种关系必须均已启用，且项目的测试/主网属性必须与链匹配。
- 充值收款确认后，系统会调度智能合约归集；系统钱包需要在对应链保留少量 Gas。

### 限流

`60/minute`，按 `appid + IP` 维度。

## Webhook 回调

Xcash 在账单收款或充值收款进入关键状态时向商户投递 Webhook。

### 投递地址

- Xcash 原生协议账单收款事件：优先使用账单收款创建时传入的 `notify_url`，为空时使用项目默认通知地址。
- 充值收款事件：使用项目默认通知地址。
- EPay V1 订单：使用 EPay 下单参数中的 `notify_url`，且按 EPay 协议 GET query 投递。

生产环境默认只允许投递到 HTTPS 公网地址，拒绝 `http`、`localhost` 和私有网段地址。

### Xcash Webhook 签名

Xcash 原生协议账单收款与充值收款事件使用 `POST application/json`，并带 HMAC 头：

```http
XC-Appid: {appid}
XC-Nonce: {event_nonce}
XC-Timestamp: {unix_timestamp}
XC-Signature: {hmac_signature}
Content-Type: application/json
```

签名算法与 API 请求一致：

```text
message   = XC-Nonce + XC-Timestamp + request_body
signature = HMAC-SHA256(message, hmac_key).hexdigest()
```

商户应验证签名，并保证同一 `XC-Nonce` 幂等处理。

### 响应与重试

- Xcash Webhook 成功响应：HTTP `200`，响应体去除首尾空白后等于 `ok`。
- EPay V1 通知成功响应：HTTP `200`，响应体去除首尾空白后等于 `success`。
- 单次 HTTP 请求超时为 5 秒。
- 只有网络错误或 5xx 会按指数退避重试；2xx 非 200、3xx、4xx 不重试。
- 项目通知开关必须开启；即使账单或 EPay 订单传入了独立 `notify_url`，项目通知开关关闭时也不会投递。

### 账单收款 Webhook

触发逻辑：账单收款进入 `completed` 后发送一次通知，`confirmed=true`。

示例：

```json
{
  "type": "invoice",
  "data": {
    "sys_no": "INV2606028X7K2P9Q",
    "out_no": "order-20260602-001",
    "crypto": "USDT",
    "chain": "ethereum",
    "pay_address": "0xAbCd1234...",
    "pay_amount": "29.870001",
    "hash": "0xabc123...",
    "block": 12345678,
    "confirmed": true,
    "risk_level": null,
    "risk_score": null
  }
}
```

### 充值收款 Webhook

触发逻辑：充值收款对应链上转账达到确认要求后发送一次通知，`confirmed=true`。

`risk_level` 与 `risk_score` 是发送通知时的风险快照；AML 任务异步执行时，这两个字段可能暂时为 `null`，不要把首次 Webhook 视为最终风控报告。

示例：

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

## 易支付 V1 兼容

Xcash 提供易支付 V1 兼容入口，适配常见 Typecho、WordPress、Discuz 等易支付插件。

### 商户身份

通过 Xcash SaaS 创建的项目会自动分配 EPay 商户身份；自托管或后台手工创建的项目请在管理后台确认 EPay 配置已存在并启用：

| 字段 | 说明 |
|------|------|
| `pid` | EPay 商户 ID |
| `secret_key` | EPay MD5 签名密钥 |
| `active` | 是否启用 EPay 入口 |

可在管理后台项目页的 EPay 配置区域查看和修改。

### 创建订单

`GET /epay/submit.php` 或 `POST /epay/submit.php`

EPay 入口不使用 Xcash HMAC 头，使用 EPay 自有 MD5 签名。`POST` 请求请使用 `application/x-www-form-urlencoded` 或 `multipart/form-data` 表单编码；不要发送 JSON。

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pid` | integer | 是 | EPay 商户 ID |
| `type` | string | 否 | EPay 兼容字段，最长 32 位；Xcash 不会用该字段限定链或币种，只在通知中原样回传 |
| `out_trade_no` | string | 是 | 商户订单号，最长 64 位，同一 EPay 商户下唯一 |
| `notify_url` | string | 是 | EPay 异步通知地址 |
| `return_url` | string | 否 | 同步跳转地址 |
| `name` | string | 是 | 商品名称，最长 128 位 |
| `money` | string | 是 | 金额，最小 `0.01`，最多两位小数 |
| `currency` | string | 否 | Xcash 扩展字段，默认 `CNY`；传入时必须是系统支持的法币代码 |
| `param` | string | 否 | 业务扩展参数，最长 512 位 |
| `sign` | string | 是 | MD5 签名 |
| `sign_type` | string | 是 | 固定 `MD5` |

EPay 订单按项目账单收款模式创建，有效期 15 分钟。相同 `out_trade_no` 且订单元数据完全一致时，重复提交会返回同一笔账单；若元数据不一致则失败。

### EPay 签名

1. 去掉 `sign`、`sign_type`。
2. 去掉值为 `null` 或空字符串的字段。
3. 按字段名 ASCII 升序排序。
4. 拼接 `key=value`，字段之间用 `&` 连接。
5. 在末尾直接追加 `secret_key`。
6. 对整体字符串取 MD5，输出小写十六进制。

Python 示例：

```python
import hashlib

params = {
    "pid": "1001",
    "out_trade_no": "order-001",
    "notify_url": "https://merchant.example.com/epay/notify",
    "return_url": "https://merchant.example.com/epay/return",
    "name": "Premium Plan",
    "money": "29.99",
    "sign_type": "MD5",
}

filtered = {
    k: str(v)
    for k, v in params.items()
    if k not in {"sign", "sign_type"} and v not in (None, "")
}
sign_string = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
secret_key = "your_epay_secret_key"
params["sign"] = hashlib.md5(
    f"{sign_string}{secret_key}".encode("utf-8"),
    usedforsecurity=False,
).hexdigest()
```

### 创建响应

- 成功：HTTP `302`，重定向到 Xcash 账单收款页 `/pay/{sys_no}`。
- 失败：HTTP `400`，纯文本 `fail`。
- 触发限流时可能返回 HTTP `429`。

### EPay 异步通知

账单收款完成后，Xcash 向 `notify_url` 发送 GET 请求，query string 包含：

| 字段 | 说明 |
|------|------|
| `pid` | EPay 商户 ID |
| `trade_no` | Xcash 系统订单号 |
| `out_trade_no` | 商户订单号 |
| `type` | EPay 兼容字段 |
| `name` | 商品名称 |
| `money` | 订单金额，两位小数 |
| `trade_status` | 固定 `TRADE_SUCCESS` |
| `param` | 下单时传入的扩展参数，有值才返回 |
| `sign_type` | 固定 `MD5` |
| `sign` | EPay MD5 签名 |

商户响应 HTTP `200` 且响应体为 `success` 时，视为通知成功。

### 同步跳转

账单收款完成后，若 EPay 下单传入 `return_url`，公开查询接口中的 `return_url` 会在账单收款完成时返回带 EPay 参数和签名的同步跳转 URL。同步跳转只表示账单收款页流程结束，核心发货逻辑仍应以异步通知为准。

## 错误码

### 通用错误

| 错误码 | 说明 | HTTP |
|--------|------|------|
| `1000` | 参数错误 | 400 |
| `1001` | AppID 无效 | 400 |
| `1002` | IP 禁止 | 403 |
| `1003` | 签名错误 | 403 |
| `1004` | 项目未配置 | 400 |
| `1007` | 单号 `out_no` 重复 | 400 |
| `1008` | Timestamp 请求头未设置或过期 | 400 |
| `1009` | 请求重复 | 400 |

### 链与币种错误

| 错误码 | 说明 | HTTP |
|--------|------|------|
| `2000` | 无效链 | 400 |
| `2001` | 无效加密货币 | 400 |
| `2002` | 本链不支持此加密货币 | 400 |

### 充值收款错误

| 错误码 | 说明 | HTTP |
|--------|------|------|
| `4000` | 无效 UID | 400 |
| `4001` | 项目未配置该链的归集收款地址 | 400 |
| `4002` | 充值用户数已达到当前套餐上限 | 403 |

### 账单收款错误

| 错误码 | 说明 | HTTP |
|--------|------|------|
| `5000` | 不支持的计价法币 | 400 |
| `5008` | 无可用账单收款方式 | 400 |
| `5009` | 待支付记录过多 | 400 |

### SaaS / 内部权限错误

| 错误码 | 说明 | HTTP |
|--------|------|------|
| `6000` | 内部 API 令牌无效 | 401 |
| `6002` | 项目不存在 | 404 |
| `6004` | 账户已冻结 | 403 |

## 完整流程

### 账单收款

```text
商户服务器 -> Xcash: POST /v1/invoice
Xcash -> 商户服务器: 返回 sys_no / pay_url
买家 -> Xcash: 打开 pay_url，在账单收款页选币、选链
买家 -> 区块链: 转账
Xcash -> 商户服务器: Webhook invoice
商户服务器 -> Xcash: ok
```

### 充值收款

```text
商户服务器 -> Xcash: GET /v1/deposit/address
Xcash -> 商户服务器: 返回 deposit_address
商户系统 -> 用户: 展示 deposit_address
用户 -> 区块链: 转账
Xcash -> 商户服务器: Webhook deposit
商户服务器 -> Xcash: ok
```
