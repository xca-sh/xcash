import { useEffect, useState } from "react"
import QRCode from "qrcode"
import { ArrowLeft, Clock, Loader2, TriangleAlert } from "lucide-react"
import { Button } from "@/components/ui/button"
import CopyButton from "@/components/CopyButton"
import ConfirmationProgress from "@/components/ConfirmationProgress"
import { useMetadataContext } from "@/context/MetadataContext"
import { getConfirmationProgress, isPaymentConfirming } from "@/lib/invoiceStatus"
import { useI18n } from "@/hooks/useI18n"
import { useCountdown, formatRemainingTime } from "@/hooks/useCountdown"
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
      <span className="font-bold text-primary-ink">{address.slice(0, ADDRESS_EDGE)}</span>
      <span className="text-foreground">{address.slice(ADDRESS_EDGE, -ADDRESS_EDGE)}</span>
      <span className="font-bold text-primary-ink">{address.slice(-ADDRESS_EDGE)}</span>
    </>
  )
}

// 字段行：标签 + 右侧动作 + 内容，多行之间以细分隔线隔开
function Field({ label, action, children }) {
  return (
    <div className="py-4 first:pt-0 last:pb-0">
      <div className="flex h-7 items-center justify-between gap-3">
        <dt className="text-sm font-medium text-muted-foreground">{label}</dt>
        {action}
      </div>
      <dd className="mt-1">{children}</dd>
    </div>
  )
}

// 币种 / 网络图标；无图标或加载失败时退回首字母圆标，避免留下空白占位
function TokenIcon({ src, name, className }) {
  const [failed, setFailed] = useState(false)
  if (!src || failed) {
    return (
      <span
        aria-hidden="true"
        className={cn(
          "flex shrink-0 items-center justify-center rounded-full bg-muted-foreground/20 text-[0.55em] font-semibold uppercase text-muted-foreground",
          className
        )}
      >
        {name?.[0]}
      </span>
    )
  }
  return (
    <img src={src} alt="" className={cn("shrink-0 rounded-full", className)} onError={() => setFailed(true)} />
  )
}

// 剩余支付时间：琥珀色胶囊与左侧面板倒计时同色，最后 1 分钟转为危险色
const DANGER_MS = 60_000

function CardCountdown({ expiresAt }) {
  const { t } = useI18n()
  const remainingMs = useCountdown(expiresAt)
  if (remainingMs === null) return null

  return (
    <div className="flex shrink-0 flex-col items-end gap-1">
      <span className="text-[11px] leading-4 text-muted-foreground">{t("payment.timeLeft")}</span>
      <span
        className={cn(
          "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[13px] font-semibold leading-5 tabular-nums",
          remainingMs <= DANGER_MS
            ? "border-destructive/40 bg-destructive/10 text-destructive"
            : "border-warning-border bg-warning-soft text-warning"
        )}
      >
        <Clock className="size-3.5 shrink-0" />
        {remainingMs === 0 ? t("waiting.expired") : formatRemainingTime(remainingMs, t)}
      </span>
    </div>
  )
}

// 把含 {{name}} 占位符的文案渲染成节点，占位符替换为强调样式的值
function RichText({ template, values }) {
  return template.split(/(\{\{\w+\}\})/).map((part, i) => {
    const name = part.match(/^\{\{(\w+)\}\}$/)?.[1]
    if (!name || values[name] === undefined) return part
    return (
      <span key={i} className="whitespace-nowrap font-medium text-foreground">
        {values[name]}
      </span>
    )
  })
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
        : t("payment.sendCrypto", { crypto: invoice.crypto })

  // 已观测到付款后才需要说明当前在等什么；待支付时标题 + 网络已足够
  const headerDescription = isCompleted
    ? t("confirmation.transactionConfirmed")
    : isFinalizing
      ? t("confirmation.awaitingFinalization")
      : isConfirming
        ? t("confirmation.waitingConfirmation")
        : null
  const showCountdown = invoice.status === "waiting" && !hasPayment && Boolean(invoice.expires_at)

  const amountField = (
    <Field
      label={t("payment.paymentAmount")}
      action={!hasPayment && <CopyButton value={invoice.pay_amount} />}
    >
      {/* 金额是用户最需要逐位核对的值：放大加粗并用品牌色突出 */}
      <div className="flex min-w-0 items-baseline gap-2">
        <span className="truncate text-4xl font-bold leading-tight tracking-tight text-primary-ink tabular-nums">
          {invoice.pay_amount}
        </span>
        <span className="shrink-0 text-lg font-semibold text-foreground">{invoice.crypto}</span>
      </div>
    </Field>
  )

  return (
    <section className="overflow-hidden rounded-xl border bg-card shadow-xs">
      {/* 头部：两行身份信息 + 右侧倒计时。
          标题「发送 USDT」是用户要做的事；其下两行小号次级文字是这笔转账的技术约束（网络 / 合约），
          不与标题争抢注意力。「仅限此网络」由下方警示框负责，这里不重复 */}
      {/* 三列网格：第一行「图标 / 标题 / 倒计时」垂直居中；元信息从标题列起跨到最右，
          窄屏时不被倒计时挤压 */}
      <header className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3.5 px-5 pt-5 sm:px-6 sm:pt-6">
        <TokenIcon src={cryptoMeta.icon} name={invoice.crypto} className="size-11 text-lg" />
        <h2 className="truncate text-lg font-semibold leading-6 tracking-tight">{headerTitle}</h2>
        {showCountdown ? <CardCountdown expiresAt={invoice.expires_at} /> : <span />}
        <div className="col-span-2 col-start-2 mt-1 flex flex-col gap-1 text-[13px] leading-6 text-muted-foreground">
          <p className="flex flex-wrap items-center gap-x-2">
            <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
              <TokenIcon src={chainMeta.icon} name={chainMeta.name} className="size-4" />
              {chainMeta.name}
            </span>
            {chainMeta.isTestnet && (
              <span className="whitespace-nowrap rounded border border-warning-border bg-warning-soft px-1.5 text-[11px] font-medium leading-4 text-warning">
                {t("selector.testNetwork")}
              </span>
            )}
          </p>
          {invoice.crypto_address && (
            <p className="flex items-center gap-1.5 whitespace-nowrap">
              {t("payment.tokenContract")}
              <code className="font-mono text-foreground/80" title={invoice.crypto_address}>
                {invoice.crypto_address.slice(0, 6)}…{invoice.crypto_address.slice(-6)}
              </code>
              <CopyButton value={invoice.crypto_address} iconOnly className="-my-1" />
            </p>
          )}
          {headerDescription && <p className="mt-1 leading-5">{headerDescription}</p>}
        </div>
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
        // 待支付：二维码 + 金额 / 地址，下方紧跟转账警示
        <div className="px-5 py-6 sm:px-6">
          {/* 二维码与右侧「金额 / 地址」等高：右栏上下撑满并两端对齐，
              首个标签顶边、地址框底边分别对齐二维码的上下边 */}
          <div className="grid gap-6 sm:grid-cols-[auto_minmax(0,1fr)] sm:items-stretch sm:gap-7">
            <div className="self-center justify-self-center rounded-lg bg-white p-2.5 ring-1 ring-black/[0.06]">
              {qrCodeUrl ? (
                <img src={qrCodeUrl} alt={t("payment.scanQRCode")} className="size-48" />
              ) : (
                <div className="flex size-48 items-center justify-center">
                  <Loader2 className="size-5 animate-spin text-neutral-400" />
                </div>
              )}
            </div>

            <dl className="flex min-w-0 flex-col justify-between divide-y">
              {amountField}
              <Field
                label={t("payment.paymentAddress")}
                action={<CopyButton value={invoice.pay_address} />}
              >
                {/* 地址放进底色框内并加大字号，与金额同为卡片视觉焦点 */}
                <code className="block break-all rounded-lg border bg-muted px-3 py-2.5 font-mono text-[15px] font-medium leading-6">
                  <HighlightedAddress address={invoice.pay_address} />
                </code>
              </Field>
            </dl>
          </div>

          {/* 转账警示：标题一句话点明「金额必须完全一致」（系统按金额匹配账单），
              下方按「金额 / 网络」两项对齐列出细则（每项拆成两条短句避免折行），关键值高亮 */}
          <div role="note" className="mt-6 overflow-hidden rounded-lg border border-warning-border bg-warning-soft">
            <p className="flex items-center gap-2 px-4 py-2.5 text-[13px] font-semibold text-warning">
              <TriangleAlert className="size-4 shrink-0" />
              {t("payment.exactAmountTitle")}
            </p>
            <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 border-t border-warning-border px-4 py-3 text-[13px] leading-5">
              <dt className="font-medium text-warning">{t("payment.warnAmountLabel")}</dt>
              <dd className="text-muted-foreground">
                <p>{t("payment.warnAmountMismatch")}</p>
                <p>
                  <RichText
                    template={t("payment.warnAmountExchange")}
                    values={{ amount: `${invoice.pay_amount} ${invoice.crypto}` }}
                  />
                </p>
              </dd>
              <dt className="font-medium text-warning">{t("payment.warnNetworkLabel")}</dt>
              <dd className="text-muted-foreground">
                <p>
                  <RichText
                    template={t("payment.warnNetworkOnly")}
                    values={{ chain: chainMeta.name, crypto: invoice.crypto }}
                  />
                </p>
                <p>{t("payment.warnNetworkLoss")}</p>
              </dd>
            </dl>
          </div>
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
