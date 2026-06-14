import { useState, useEffect, useCallback } from "react"
import { selectPayMethod } from "@/lib/api"

/**
 * 支付方式选择 Hook
 * 负责管理加密货币和公链的选择及自动提交
 */
export function usePaymentMethod(invoice, sysNo, onInvoiceUpdate) {
  const [selectedCrypto, setSelectedCrypto] = useState("")
  const [selectedChain, setSelectedChain] = useState("")
  const [isSelecting, setIsSelecting] = useState(false)
  const [isEditing, setIsEditing] = useState(false)
  const [error, setError] = useState("")

  // 同步账单中已选择的支付方式
  useEffect(() => {
    if (!invoice || isEditing) return
    setSelectedCrypto(invoice.crypto ?? "")
    setSelectedChain(invoice.chain ?? "")
  }, [invoice, isEditing])

  // 选择加密货币时清空公链并进入编辑模式
  const handleCryptoChange = useCallback((value) => {
    setSelectedCrypto(value)
    setSelectedChain("")
    setIsEditing(true)  // 开始选择时进入编辑模式
  }, [])

  // 提交支付方式选择
  const submitPayMethod = useCallback(async () => {
    if (!selectedCrypto || !selectedChain || isSelecting) return null

    setIsSelecting(true)
    setError("")
    try {
      const data = await selectPayMethod(sysNo, selectedCrypto, selectedChain)
      if (typeof onInvoiceUpdate === "function") {
        try {
          await onInvoiceUpdate(data)
        } catch (refreshError) {
          console.error("刷新账单失败:", refreshError)
        }
      }
      setIsEditing(false)
      return data
    } catch (err) {
      setError("选择账单收款方式失败: " + err.message)
      return null
    } finally {
      setIsSelecting(false)
    }
  }, [sysNo, selectedCrypto, selectedChain, isSelecting, onInvoiceUpdate])

  // 自动提交 - 当选择完加密货币和公链后
  useEffect(() => {
    if (!invoice || !selectedCrypto || !selectedChain || isSelecting) return
    if (invoice.status !== "waiting") return

    const hasPaymentMethod = Boolean(
      invoice.crypto && invoice.chain && invoice.pay_address && invoice.pay_amount
    )

    // 如果账单中已有相同的选择,则不需要提交
    if (invoice.crypto === selectedCrypto && invoice.chain === selectedChain && hasPaymentMethod) {
      return
    }

    // 只有在编辑模式下才自动提交
    if (isEditing) {
      submitPayMethod()
    }
    // isSelecting 故意不加入 deps：提交中状态变化不应触发重试，
    // 否则提交失败后 isSelecting 回到 false 会导致无限循环。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [invoice, selectedCrypto, selectedChain, isEditing])

  // 当选择与账单当前设置一致时，直接退出编辑以展示已有支付信息
  useEffect(() => {
    if (!invoice || !isEditing) return

    const hasPaymentMethod = Boolean(
      invoice.crypto && invoice.chain && invoice.pay_address && invoice.pay_amount
    )
    if (!hasPaymentMethod) return

    if (invoice.crypto === selectedCrypto && invoice.chain === selectedChain) {
      setIsEditing(false)
    }
  }, [invoice, selectedCrypto, selectedChain, isEditing])

  // 重置选择 - 进入编辑模式
  const resetSelection = useCallback(() => {
    if (invoice?.status !== "waiting") return
    setSelectedCrypto("")
    setSelectedChain("")
    setIsEditing(true)
  }, [invoice?.status])

  // 取消编辑
  const cancelEdit = useCallback(() => {
    setSelectedCrypto(invoice?.crypto ?? "")
    setSelectedChain(invoice?.chain ?? "")
    setIsEditing(false)
  }, [invoice])

  return {
    selectedCrypto,
    selectedChain,
    isSelecting,
    isEditing,
    error,
    handleCryptoChange,
    handleChainChange: setSelectedChain,
    resetSelection,
    cancelEdit,
  }
}
