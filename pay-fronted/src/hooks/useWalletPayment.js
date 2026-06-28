import { useCallback, useEffect, useState } from "react"
import {
  connect,
  ensureChain,
  getCurrentAccount,
  normalizeError,
  sendPayment,
  subscribeProviders,
  switchAccount as walletSwitchAccount,
} from "@/lib/wallet"

/**
 * 注入式 EVM 钱包支付 Hook。
 *
 * 维护已发现的钱包列表，并暴露 pay 方法走「连接 → 切链 → 发送」三段流程。
 * 仅负责广播交易拿到 txhash，绝不触碰账单状态——账单确认仍由 useInvoice 轮询驱动。
 *
 * 返回：
 * - available：账单是否支持钱包支付（由后端 evm_payment 决定，与是否装钱包无关）
 * - wallets：已发现的钱包数组（含兜底项）
 * - status：idle | connecting | switching | sending | submitted | error
 * - error：normalizeError 结果，仅在 status === 'error' 时有意义
 * - txHash：广播成功后的交易哈希
 * - account：当前付款账户（默认跟随钱包选中的已连接账户）
 * - pay(provider)：对指定钱包发起支付
 * - switchAccount(provider)：弹出账户选择器，改连 / 切换付款账户
 */
export function useWalletPayment(invoice) {
  const [wallets, setWallets] = useState([])
  const [status, setStatus] = useState("idle")
  const [error, setError] = useState(null)
  const [txHash, setTxHash] = useState(null)
  // 当前付款账户：默认跟随钱包选中的已连接账户，用户也可经「切换账户」改连其他账户。
  const [account, setAccount] = useState(null)

  const evmPayment = invoice?.evm_payment
  const available = Boolean(evmPayment)

  // 订阅钱包发现：组件挂载时拿一次当前列表，后续每有新钱包 announce 都更新；
  // 卸载时取消订阅，避免对已卸载组件 setState。
  useEffect(() => {
    const unsubscribe = subscribeProviders((list) => {
      setWallets(list)
    })
    return unsubscribe
  }, [])

  // 单钱包时静默读取并跟随当前账户：让用户在支付前就看到「将用哪个账户付款」，
  // 并在用户于钱包内切换（已连接的）账户时自动同步。多钱包时账户在点选钱包后再定。
  useEffect(() => {
    if (wallets.length !== 1) {
      setAccount(null)
      return
    }
    const { provider } = wallets[0]
    let cancelled = false
    getCurrentAccount(provider).then((acc) => {
      if (!cancelled) setAccount(acc)
    })
    const onAccountsChanged = (accs) => setAccount(accs?.[0] ?? null)
    provider.on?.("accountsChanged", onAccountsChanged)
    return () => {
      cancelled = true
      provider.removeListener?.("accountsChanged", onAccountsChanged)
    }
  }, [wallets])

  const pay = useCallback(
    async (provider) => {
      if (!evmPayment || !provider) {
        return
      }
      // 每次发起支付前清空上一轮的错误/哈希，状态机从头走起。
      setError(null)
      setTxHash(null)
      try {
        setStatus("connecting")
        const from = await connect(provider)

        setStatus("switching")
        await ensureChain(provider, evmPayment.chain_id)

        setStatus("sending")
        const hash = await sendPayment(provider, {
          from,
          to: evmPayment.to,
          value: evmPayment.value,
          data: evmPayment.data,
        })

        // 仅标记「已提交」，真正的确认交给账单轮询，前端不擅自标记已支付。
        setTxHash(hash)
        setStatus("submitted")
      } catch (e) {
        setError(normalizeError(e))
        setStatus("error")
      }
    },
    [evmPayment]
  )

  // 切换账户：弹出钱包账户选择器，让用户改连 / 选用其他账户付款。用户取消则保持原状。
  const switchAccount = useCallback(async (provider) => {
    try {
      const acc = await walletSwitchAccount(provider)
      setAccount(acc)
    } catch {
      // 用户在选择器中取消，忽略即可，不进入支付错误态。
    }
  }, [])

  return { available, wallets, status, error, txHash, account, pay, switchAccount }
}
