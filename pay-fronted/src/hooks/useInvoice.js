import { useState, useEffect, useCallback } from "react"
import { getInvoice } from "@/lib/api"
import { getRemainingMs } from "@/lib/dateTime"
import { isPaymentConfirming } from "@/lib/invoiceStatus"
import { translate } from "@/hooks/useI18n"

/**
 * 账单管理 Hook
 * 负责获取账单数据和轮询更新
 */
export function useInvoice(sysNo) {
  const [invoice, setInvoice] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [consecutiveErrors, setConsecutiveErrors] = useState(0)
  const invoiceStatus = invoice?.status
  const invoiceExpiresAt = invoice?.expires_at

  // 获取账单详情
  const fetchInvoice = useCallback(async () => {
    if (!sysNo) {
      setError(translate("error.missingInvoiceNumber"))
      setLoading(false)
      return
    }

    try {
      const data = await getInvoice(sysNo)
      setInvoice(data)
      setError("")
      setConsecutiveErrors(0)
    } catch (err) {
      setError(translate("error.fetchFailed") + ": " + err.message)
      setConsecutiveErrors((prev) => prev + 1)
    } finally {
      setLoading(false)
    }
  }, [sysNo])

  // 初始加载
  useEffect(() => {
    fetchInvoice()
  }, [fetchInvoice])

  // 自动轮询 - 在待支付(已选择支付方式)或 Transfer 确认中状态
  useEffect(() => {
    if (!invoice) return

    const hasPaymentMethod = Boolean(
      invoice.crypto && invoice.chain && invoice.pay_address && invoice.pay_amount
    )
    const shouldPoll =
      ((invoice.status === "waiting" && hasPaymentMethod) ||
        isPaymentConfirming(invoice)) &&
      invoice.status !== "expired" &&
      invoice.status !== "completed"

    if (!shouldPoll) return

    // 连续失败时指数退避：1.5s → 3s → 6s → 12s → 最高 30s
    const interval = Math.min(1500 * Math.pow(2, consecutiveErrors), 30000)
    const timer = setInterval(fetchInvoice, interval)
    return () => clearInterval(timer)
  }, [invoice, fetchInvoice, consecutiveErrors])

  // 无论用户停在哪一步，只要未支付账单到达后端给出的 expires_at，就刷新一次状态。
  // expires_at 解析统一走 dateTime 工具：带 Z/offset 按原时区解析；无时区按 UTC 兜底。
  useEffect(() => {
    if (invoiceStatus !== "waiting" || !invoiceExpiresAt) {
      return
    }

    const remainingMs = getRemainingMs(invoiceExpiresAt)
    if (remainingMs === null) return

    const timer = setTimeout(fetchInvoice, remainingMs)
    return () => clearTimeout(timer)
  }, [invoiceStatus, invoiceExpiresAt, fetchInvoice])

  return {
    invoice,
    loading,
    error,
    refetch: fetchInvoice,
  }
}
