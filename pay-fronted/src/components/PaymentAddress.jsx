import { useEffect, useState } from "react"
import QRCode from "qrcode"
import { Copy, Check, Clock, CheckCircle2, ArrowLeft, Loader2 } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { getCryptoIconUrl, getChainIconUrl, getChainDisplayName } from "@/lib/cryptoIcons"
import { useI18n } from "@/hooks/useI18n"

// 复制按钮：copied 命中当前字段时切换为对勾。提到组件外，避免在 render 期间创建组件。
function CopyButton({ copied, onCopy }) {
  return (
    <Button variant="outline" size="icon" className="size-7" onClick={onCopy}>
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
    </Button>
  )
}

function PaymentAddress({ invoice, onReset }) {
  const { t } = useI18n()
  const [qrCodeUrl, setQrCodeUrl] = useState("")
  const [copiedField, setCopiedField] = useState("")

  const hasPayment = Boolean(invoice?.payment)
  const isConfirming = invoice?.status === "confirming"
  const isCompleted = invoice?.status === "completed"
  const confirmingProgress = invoice?.payment?.confirm_progress || {}
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

    // EVM 账单优先用 EIP-681 URI（含链/代币/金额），扫码即预填，
    // 大幅减少手输金额导致的「付款金额不符」；无 URI（如 Tron）时退回纯地址。
    const qrValue = invoice.payment_uri || invoice.pay_address

    // 二维码需固定深/浅对比才能被钱包扫描，这里使用静态黑白（功能性需求，非主题色）。
    QRCode.toDataURL(qrValue, {
      width: 256,
      margin: 2,
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

  const handleCopy = (value, field) => {
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopiedField(field)
        setTimeout(() => setCopiedField(""), 2000)
      })
      .catch((err) => {
        console.error("Copy failed:", err)
      })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg">
          {isCompleted
            ? t("payment.paymentCompleted")
            : isFinalizing
              ? t("payment.paymentFinalizing")
              : isConfirming
                ? t("payment.paymentConfirming")
                : t("payment.paymentInfo")}
        </CardTitle>
        <CardDescription>
          {isCompleted ? (
            <span className="flex items-center gap-1.5">
              <CheckCircle2 className="size-3.5" />
              {t("confirmation.transactionConfirmed")}
            </span>
          ) : isFinalizing ? (
            <span className="flex items-center gap-1.5">
              <Clock className="size-3.5" />
              {t("confirmation.awaitingFinalization")}
            </span>
          ) : isConfirming ? (
            <span className="flex items-center gap-1.5">
              <Clock className="size-3.5" />
              {t("confirmation.waitingConfirmation")}
            </span>
          ) : (
            <span>
              {t("payment.transferTo")}{" "}
              <span className="font-mono font-medium text-foreground">
                {invoice.pay_amount} {invoice.crypto}
              </span>{" "}
              {t("payment.toAddress")}
            </span>
          )}
        </CardDescription>
      </CardHeader>

      <CardContent className="flex flex-col gap-5">
        {/* Confirmation progress */}
        {hasPayment && (isConfirming || isCompleted) && (
          <div className="flex flex-col gap-3">
            <div className="bg-muted rounded-lg p-5 flex flex-col gap-3">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">
                  {t("confirmation.blockConfirmation")}
                </span>
                <span className="text-lg font-bold font-mono tabular-nums">{progress}%</span>
              </div>
              <Progress value={progress} />
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span>{t("confirmation.confirmed")} {hasConfirmedCount} {t("confirmation.blocks")}</span>
                <span>{t("confirmation.needs")} {needConfirmedCount} {t("confirmation.blocks")}</span>
              </div>
            </div>

            {/* Transaction hash */}
            {invoice.payment.hash && (
              <div className="flex flex-col gap-2">
                <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                  {t("payment.transactionHash")}
                </span>
                <code className="block break-all bg-muted rounded-lg p-3 text-xs font-mono text-muted-foreground leading-relaxed">
                  {invoice.payment.hash}
                </code>
              </div>
            )}
          </div>
        )}

        {/* QR Code — only when not yet paid */}
        {!hasPayment && (
          <div className="flex justify-center">
            {qrCodeUrl ? (
              <div className="inline-flex flex-col items-center gap-3">
                <div className="bg-white rounded-lg p-4 border">
                  <img src={qrCodeUrl} alt={t("payment.scanQRCode")} className="size-40" />
                </div>
                <p className="text-xs text-muted-foreground">{t("payment.scanQRCode")}</p>
              </div>
            ) : (
              <div className="flex size-48 items-center justify-center bg-muted rounded-lg">
                <Loader2 className="size-6 animate-spin text-muted-foreground" />
              </div>
            )}
          </div>
        )}

        {/* Amount and network */}
        <div className="grid grid-cols-2 gap-3">
          <div className="bg-muted rounded-lg p-3">
            <div className="flex items-center justify-between mb-2 h-7">
              <span className="text-xs font-medium text-muted-foreground">
                {t("payment.paymentAmount")}
              </span>
              <CopyButton
                copied={copiedField === "amount"}
                onCopy={() => handleCopy(invoice.pay_amount, "amount")}
              />
            </div>
            <div className="flex items-center gap-2">
              <img
                src={getCryptoIconUrl(invoice.crypto)}
                alt=""
                className="size-5 rounded-full shrink-0"
                onError={(e) => { e.target.style.visibility = "hidden" }}
              />
              <span className="font-mono font-semibold text-sm tabular-nums">
                {invoice.pay_amount} {invoice.crypto}
              </span>
            </div>
          </div>

          <div className="bg-muted rounded-lg p-3">
            <div className="text-xs font-medium text-muted-foreground mb-2 h-7 flex items-center">
              {t("payment.network")}
            </div>
            <div className="flex items-center gap-2">
              <img
                src={getChainIconUrl(invoice.chain)}
                alt=""
                className="size-5 rounded-full shrink-0"
                onError={(e) => { e.target.style.visibility = "hidden" }}
              />
              <span className="font-medium text-sm">{getChainDisplayName(invoice.chain)}</span>
            </div>
          </div>
        </div>

        {/* Payment address */}
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              {t("payment.paymentAddress")}
            </span>
            <CopyButton
              copied={copiedField === "address"}
              onCopy={() => handleCopy(invoice.pay_address, "address")}
            />
          </div>
          <code className="block break-all bg-muted rounded-lg p-3 text-xs font-mono leading-relaxed">
            {invoice.pay_address}
          </code>
        </div>

        {/* Contract address */}
        {invoice.crypto_address && (
          <div className="flex flex-col gap-2">
            <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
              {invoice.crypto} {t("payment.contractAddress")}
            </span>
            <code className="block break-all bg-muted rounded-lg p-3 text-xs font-mono text-muted-foreground leading-relaxed select-none">
              {invoice.crypto_address.slice(0, 6)}...{invoice.crypto_address.slice(-8)}
            </code>
          </div>
        )}

        {/* Reselect payment method */}
        {!hasPayment && onReset && (
          <Button variant="outline" onClick={onReset} size="sm" className="w-full">
            <ArrowLeft className="size-3.5" />
            {t("payment.reselectMethod")}
          </Button>
        )}

        {/* Return to merchant */}
        {isCompleted && invoice.return_url && (
          <Button onClick={() => window.open(invoice.return_url, "_blank")} className="w-full">
            {t("payment.returnToMerchant")}
          </Button>
        )}
      </CardContent>
    </Card>
  )
}

export default PaymentAddress
