import { useEffect } from "react"
import { Wallet, Loader2, CheckCircle2, AlertCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/hooks/useI18n"
import { useTronWalletPayment } from "@/hooks/useTronWalletPayment"

// 进行中的状态：禁用按钮并展示对应阶段文案 + spinner。
const BUSY_STATUS = ["connecting", "sending"]

// 把 normalizeTronError 的 kind 映射到 i18n 文案 key。
function errorTextKey(error) {
  if (error?.kind === "rejected") return "wallet.userRejected"
  if (error?.kind === "networkMismatch") return "wallet.tronWrongNetwork"
  return "wallet.failed"
}

/**
 * TronLink 钱包一键支付入口。
 *
 * 定位：作为「手动转账（扫码 / 复制地址）」之外的备选支付方式，放在支付卡片底部。
 * 与 EVM 版 WalletPay 同风格，但 Tron 是单钱包：无多钱包选择、无法自动切网络。
 * 渲染条件：账单支持 tron_payment、仍处于 waiting、尚未观测到链上付款，且确实
 * 检测到了 TronLink——没装则整体不展示，不做无效引导。
 */
function TronWalletPay({ invoice, onBroadcast }) {
  const { t } = useI18n()
  const { hasWallet, status, error, pay } = useTronWalletPayment(invoice)

  // 广播成功后通知上层，让底部总状态卡同步为「等待区块确认」。
  useEffect(() => {
    if (status === "submitted") onBroadcast?.()
  }, [status, onBroadcast])

  const shouldRender =
    Boolean(invoice?.tron_payment) &&
    invoice?.status === "waiting" &&
    !invoice?.payment
  // 未检测到 TronLink 时不展示入口（异步注入，检测到后自然出现）。
  if (!shouldRender || !hasWallet) {
    return null
  }

  // 已提交：只提示等待确认，绝不显示「已支付」；此时已不是备选项，独立展示。
  if (status === "submitted") {
    return (
      <div className="flex items-center justify-center gap-2 rounded-lg border border-success-border bg-success-soft px-3 py-2.5 text-[13px] font-medium text-success">
        <CheckCircle2 className="size-4 shrink-0" />
        <span>{t("wallet.submitted")}</span>
      </div>
    )
  }

  const isBusy = BUSY_STATUS.includes(status)

  return (
    <div className="flex flex-col gap-2.5">
      {isBusy ? (
        <Button size="lg" className="w-full" disabled>
          <Loader2 className="size-4 animate-spin" />
          {t(`wallet.${status}`)}
        </Button>
      ) : (
        <Button size="lg" className="w-full" onClick={pay}>
          <Wallet className="size-4" />
          {t("wallet.payWithWallet")}
        </Button>
      )}

      {/* 错误提示：用户取消/网络不匹配/通用失败，文案随 kind 切换，按钮本身即可重试。 */}
      {status === "error" && (
        <p className="flex items-center justify-center gap-2 text-xs text-destructive">
          <AlertCircle className="size-3.5 shrink-0" />
          <span>{t(errorTextKey(error))}</span>
        </p>
      )}
    </div>
  )
}

export default TronWalletPay
