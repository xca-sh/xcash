// 注入式 EVM 钱包工具：纯浏览器原生能力，零额外依赖。
// 基于 EIP-6963 做多钱包发现、EIP-1193 做账户连接/网络切换/发送交易。
// 不引入 ethers/viem/wagmi 等库，所有交互直接走 provider.request。

// 已发现的钱包提供方，按 rdns 去重。模块单例，import 即开始收集。
const discoveredProviders = new Map()
// 订阅者集合，每次发现新钱包都用最新列表回调。
const subscribers = new Set()

// EIP-6963：钱包通过 announceProvider 事件广播自身，我们监听并收集。
function handleAnnounce(event) {
  const detail = event?.detail
  if (!detail?.info?.rdns || !detail?.provider) {
    return
  }
  // 按 rdns 去重：同一钱包可能多次 announce，保留最新一次即可。
  discoveredProviders.set(detail.info.rdns, {
    info: detail.info,
    provider: detail.provider,
  })
  notifySubscribers()
}

// 仅在浏览器环境注册监听并主动请求一次发现广播。
if (typeof window !== "undefined") {
  window.addEventListener("eip6963:announceProvider", handleAnnounce)
  window.dispatchEvent(new Event("eip6963:requestProvider"))
}

// 当前钱包列表快照。列表为空但存在 window.ethereum 时，合成一个兜底项，
// 兼容仅注入 window.ethereum、未实现 EIP-6963 的老钱包。
function currentProviders() {
  const list = Array.from(discoveredProviders.values())
  if (list.length === 0 && typeof window !== "undefined" && window.ethereum) {
    return [
      {
        info: { name: "浏览器钱包", rdns: "injected-fallback" },
        provider: window.ethereum,
      },
    ]
  }
  return list
}

function notifySubscribers() {
  const list = currentProviders()
  subscribers.forEach((cb) => cb(list))
}

/**
 * 订阅钱包列表变化。
 * 幂等：重复传入同一回调只注册一次；注册时立刻用当前列表回调一次，
 * 并再次广播 requestProvider 以触发尚未 announce 的钱包补报。
 * 返回 unsubscribe 函数。
 */
export function subscribeProviders(cb) {
  subscribers.add(cb)
  // 注册即回调一次当前快照，避免订阅者错过已发现的钱包。
  cb(currentProviders())
  // 再次请求广播：有些钱包在页面加载后才注入，主动催一次更稳。
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event("eip6963:requestProvider"))
  }
  return () => {
    subscribers.delete(cb)
  }
}

/**
 * 连接钱包，返回当前选中账户地址。
 * eth_requestAccounts 返回本站已授权账户，accounts[0] 即钱包当前选中的那个。
 */
export async function connect(provider) {
  const accounts = await provider.request({ method: "eth_requestAccounts" })
  return accounts?.[0]
}

/**
 * 静默读取当前已连接账户（不弹窗）。未连接或读取失败时返回 null。
 * 用于在支付前展示「将用哪个账户付款」，以及跟随钱包内账户切换。
 */
export async function getCurrentAccount(provider) {
  try {
    const accounts = await provider.request({ method: "eth_accounts" })
    return accounts?.[0] ?? null
  } catch {
    return null
  }
}

/**
 * 切换付款账户：重新请求账户授权，弹出钱包账户选择器。
 *
 * MetaMask 等按站点授权账户：已连接后 eth_requestAccounts 不会再弹选择，
 * 故切到「未连接到本站的账户」必须经此显式 wallet_requestPermissions 重新授权。
 * 返回用户选定后的当前账户；用户在选择器中取消时由 provider 抛错，交上层忽略。
 */
export async function switchAccount(provider) {
  await provider.request({
    method: "wallet_requestPermissions",
    params: [{ eth_accounts: {} }],
  })
  const accounts = await provider.request({ method: "eth_accounts" })
  return accounts?.[0] ?? null
}

/**
 * 十进制 chainId 转 EIP-1193 要求的十六进制字符串。
 */
export function hexChainId(n) {
  return "0x" + Number(n).toString(16)
}

/**
 * 确保钱包当前网络与目标链一致，不一致则请求切换。
 * 我们没有公共 RPC，无法 wallet_addEthereumChain 自动添加未知链，
 * 故钱包未添加该链（code 4902）时抛出 CHAIN_NOT_ADDED 让上层提示手动切换。
 */
export async function ensureChain(provider, chainId) {
  const target = hexChainId(chainId)
  const current = await provider.request({ method: "eth_chainId" })
  if (current === target) {
    return
  }
  try {
    await provider.request({
      method: "wallet_switchEthereumChain",
      params: [{ chainId: target }],
    })
  } catch (e) {
    if (e?.code === 4902) {
      throw Object.assign(new Error("chain not added"), {
        code: "CHAIN_NOT_ADDED",
      })
    }
    throw e
  }
}

/**
 * 发送支付交易，返回 txhash。
 * 参数 to/value/data 原样来自后端 evm_payment，前端不重算金额。
 * data 为 null 时不带该键（合约调用才有 calldata）。
 */
export async function sendPayment(provider, { from, to, value, data }) {
  const tx = { from, to, value }
  if (data != null) {
    tx.data = data
  }
  return await provider.request({
    method: "eth_sendTransaction",
    params: [tx],
  })
}

/**
 * 归一化 provider 错误为上层可据此选 i18n 文案的标记。
 * 4001：用户在钱包里拒绝；CHAIN_NOT_ADDED：链未添加；其余为通用失败。
 */
export function normalizeError(e) {
  if (e?.code === 4001) {
    return { kind: "rejected" }
  }
  if (e?.code === "CHAIN_NOT_ADDED") {
    return { kind: "chainNotAdded" }
  }
  return { kind: "failed", message: e?.message }
}
