import { useEffect, useState } from "react"
import QRCode from "qrcode"
import { ArrowLeft, Info, Loader2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import CopyButton from "@/components/CopyButton"
import ConfirmationProgress from "@/components/ConfirmationProgress"
import { useMetadataContext } from "@/context/MetadataContext"
import { getConfirmationProgress, isPaymentConfirming } from "@/lib/invoiceStatus"
import { useI18n } from "@/hooks/useI18n"
import { cn } from "@/lib/utils"
import WalletPay from "@/components/WalletPay"
import TronWalletPay from "@/components/TronWalletPay"

// 地址首尾高亮、中段弱化：用户核对地址时习惯比对首尾几位，
// 这样能更快发现被剪贴板劫持替换的地址。
const ADDRESS_EDGE = 6

function HighlightedAddress({ address }) {
  if (address.length <= ADDRESS_EDGE * 2) {
    return address
  }
  return (
    <>
      <span className="text-foreground">{address.slice(0, ADDRESS_EDGE)}</span>
      <span className="text-muted-foreground">{address.slice(ADDRESS_EDGE, -ADDRESS_EDGE)}</span>
      <span className="text-foreground">{address.slice(-ADDRESS_EDGE)}</span>
    </>
  )
}

// 字段行：标签 + 右侧动作 + 内容，多行之间以细分隔线隔开
function Field({ label, action, children }) {
  return (
    <div className="py-4 first:pt-0 last:pb-0">
      <div className="flex h-7 items-center justify-between gap-3">
        <dt className="text-xs text-muted-foreground">{label}</dt>
        {action}
      </div>
      <dd className="mt-1">{children}</dd>
    </div>
  )
}

function TokenIcon({ src, className }) {
  return (
    <img
      src={src || undefined}
      alt=""
      className={cn("shrink-0 rounded-full", className)}
      onError={(e) => { e.target.style.visibility = "hidden" }}
    />
  )
}

function PaymentAddress({ invoice, onReset, onBroadcast }) {
  const { t } = useI18n()
  const { getChain, getCrypto } = useMetadataContext()
  const [qrCodeUrl, setQrCodeUrl] = useState("")

  const hasPayment = Boolean(invoice?.payment)
  const isConfirming = isPaymentConfirming(invoice)
  const isCompleted = invoice?.status === "completed"
  const confirmingProgress = getConfirmationProgress(invoice)
  const progress = confirmingProgress.progress || 0
  const hasConfirmedCount = confirmingProgress.has_confirmed_count || 0
  const needConfirmedCount = confirmingProgress.need_confirmed_count || 0
  // 区块层已达到目标确认数（按链高度即时判定，与后端 transfer.status 解耦）。
  // 后端 invoice.status 切到 completed 还要 worker 跑 RPC 二次校验，存在时延，
  // 这段窗口内把标题/描述切到「最终化中」，让用户知道在等什么，避免误以为卡住。
  const isFinalizing = isConfirming && progress >= 100

  useEffect(() => {
    if (!invoice?.pay_address) {
      return
    }

    // EVM 账单优先用 EIP-681 URI（含链/加密货币/金额），扫码即预填，
    // 大幅减少手输金额导致的「付款金额不符」；无 URI（如 Tron）时退回纯地址。
    const qrValue = invoice.payment_uri || invoice.pay_address

    // 二维码需固定深/浅对比才能被钱包扫描，这里使用静态黑白（功能性需求，非主题色）。
    QRCode.toDataURL(qrValue, {
      width: 320,
      margin: 0,
      color: { dark: "#000000", light: "#ffffff" },
    })
      .then(setQrCodeUrl)
      .catch((err) => {
        console.error("QR code generation failed:", err)
      })
  }, [invoice?.payment_uri, invoice?.pay_address])

  if (!invoice?.pay_address) {
    return null
  }

  const cryptoMeta = getCrypto(invoice.crypto)
  const chainMeta = getChain(invoice.chain)

  const headerTitle = isCompleted
    ? t("payment.paymentCompleted")
    : isFinalizing
      ? t("payment.paymentFinalizing")
      : isConfirming
        ? t("payment.paymentConfirming")
        : t("payment.paymentInfo")

  const headerDescription = isCompleted
    ? t("confirmation.transactionConfirmed")
    : isFinalizing
      ? t("confirmation.awaitingFinalization")
      : isConfirming
        ? t("confirmation.waitingConfirmation")
        : t("payment.transferHint")

  const amountField = (
    <Field
      label={t("payment.paymentAmount")}
      action={!hasPayment && <CopyButton value={invoice.pay_amount} />}
    >
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="truncate text-[1.75rem] font-semibold leading-tight tracking-tight tabular-nums">
          {invoice.pay_amount}
        </span>
        <span className="shrink-0 text-sm font-medium text-muted-foreground">{invoice.crypto}</span>
      </div>
    </Field>
  )

  return (
    <section className="overflow-hidden rounded-xl border bg-card shadow-xs">
      {/* 头部：加密货币图标 + 标题/说明 + 网络标识 */}
      <header className="flex items-start gap-3.5 px-5 pt-5 sm:px-6 sm:pt-6">
        <TokenIcon src={cryptoMeta.icon} className="mt-0.5 size-9 ring-1 ring-border" />
        <div className="min-w-0 flex-1">
          <h2 className="text-[15px] font-medium leading-6 tracking-tight">{headerTitle}</h2>
          <p className="mt-0.5 text-[13px] leading-5 text-muted-foreground">{headerDescription}</p>
        </div>
        <span
          className={cn(
            "inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium",
            chainMeta.isTestnet && "border-warning-border bg-warning-soft text-warning"
          )}
        >
          <TokenIcon src={chainMeta.icon} className="size-3.5" />
          {chainMeta.name}
          {chainMeta.isTestnet && ` · ${t("selector.testNetwork")}`}
        </span>
      </header>

      {hasPayment ? (
        // 已观测到链上付款：确认进度 + 金额 + 交易哈希
        <div className="px-5 py-6 sm:px-6">
          <ConfirmationProgress
            progress={progress}
            confirmed={hasConfirmedCount}
            required={needConfirmedCount}
            live={isConfirming && !isFinalizing}
          />
          <dl className="mt-6 divide-y border-t pt-4">
            {amountField}
            {invoice.payment.hash && (
              <Field
                label={t("payment.transactionHash")}
                action={<CopyButton value={invoice.payment.hash} />}
              >
                <code className="block break-all font-mono text-xs leading-5 text-muted-foreground">
                  {invoice.payment.hash}
                </code>
              </Field>
            )}
          </dl>
        </div>
      ) : (
        // 待支付：二维码 + 金额 / 地址
        <div className="grid gap-6 px-5 py-6 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-center sm:gap-7 sm:px-6">
          <div className="justify-self-center rounded-lg bg-white p-2.5 ring-1 ring-black/[0.06]">
            {qrCodeUrl ? (
              <img src={qrCodeUrl} alt={t("payment.scanQRCode")} className="size-40" />
            ) : (
              <div className="flex size-40 items-center justify-center">
                <Loader2 className="size-5 animate-spin text-neutral-400" />
              </div>
            )}
          </div>

          <dl className="min-w-0 divide-y">
            {amountField}
            <Field
              label={t("payment.paymentAddress")}
              action={<CopyButton value={invoice.pay_address} />}
            >
              <code className="block break-all font-mono text-[13px] leading-5">
                <HighlightedAddress address={invoice.pay_address} />
              </code>
            </Field>
          </dl>
        </div>
      )}

      {/* 转账须知：网络 / 合约 / 金额一致性，转错网络是最常见的丢币原因 */}
      {!hasPayment && (
        <div className="space-y-2.5 border-t bg-muted/40 px-5 py-4 text-xs sm:px-6">
          {invoice.crypto_address && (
            <div className="flex items-center justify-between gap-4">
              <span className="text-muted-foreground">{t("payment.tokenContract")}</span>
              <code className="select-none font-mono text-muted-foreground">
                {invoice.crypto_address.slice(0, 6)}…{invoice.crypto_address.slice(-8)}
              </code>
            </div>
          )}
          <p className="flex gap-2 leading-5 text-muted-foreground">
            <Info className="mt-0.5 size-3.5 shrink-0" />
            <span>
              {t("payment.networkNotice", { chain: chainMeta.name, crypto: invoice.crypto })}
            </span>
          </p>
        </div>
      )}

      {/* 操作区：钱包一键支付（检测到钱包才渲染）、重选支付方式、返回商户；无内容时整块隐藏 */}
      <div className="flex flex-col gap-2 border-t px-5 py-4 empty:hidden sm:px-6">
        {!hasPayment && <WalletPay invoice={invoice} onBroadcast={onBroadcast} />}
        {!hasPayment && <TronWalletPay invoice={invoice} onBroadcast={onBroadcast} />}
        {!hasPayment && onReset && (
          <Button variant="ghost" onClick={onReset} size="sm" className="w-full text-muted-foreground">
            <ArrowLeft className="size-3.5" />
            {t("payment.reselectMethod")}
          </Button>
        )}
        {isCompleted && invoice.return_url && (
          <Button size="lg" onClick={() => window.open(invoice.return_url, "_blank")} className="w-full">
            {t("payment.returnToMerchant")}
          </Button>
        )}
      </div>
    </section>
  )
}

export default PaymentAddress
