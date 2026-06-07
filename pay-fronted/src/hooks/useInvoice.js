import { useState, useEffect, useCallback } from "react"
import { getInvoice } from "@/lib/api"
import { isPaymentConfirming } from "@/lib/invoiceStatus"
import en from "@/locales/en.json"
import zh from "@/locales/zh.json"

/**
 * 从浏览器获取语言偏好
 */
function getBrowserLanguage() {
  const browserLang = navigator.language || navigator.userLanguage || "en"
  const langCode = browserLang.split("-")[0]
  return langCode === "zh" ? "zh" : "en"
}

/**
 * 获取翻译文本
 */
function getTranslation(key) {
  const locale = getBrowserLanguage()
  const translations = locale === "zh" ? zh : en
  const keys = key.split(".")
  let translation = translations

  for (const k of keys) {
    translation = translation?.[k]
    if (!translation) break
  }

  return translation || key
}

/**
 * 账单管理 Hook
 * 负责获取账单数据和轮询更新
 */
export function useInvoice(sysNo) {
  const [invoice, setInvoice] = useState(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [consecutiveErrors, setConsecutiveErrors] = useState(0)

  // 获取账单详情
  const fetchInvoice = useCallback(async () => {
    if (!sysNo) {
      setError(getTranslation("error.missingInvoiceNumber"))
      setLoading(false)
      return
    }

    try {
      const data = await getInvoice(sysNo)
      setInvoice(data)
      setError("")
      setConsecutiveErrors(0)
    } catch (err) {
      setError(getTranslation("error.fetchFailed") + ": " + err.message)
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

  return {
    invoice,
    loading,
    error,
    refetch: fetchInvoice,
  }
}
