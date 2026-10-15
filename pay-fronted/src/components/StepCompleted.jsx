// src/components/StepCompleted.jsx
import { Check } from "lucide-react"
import { Button } from "@/components/ui/button"
import CopyButton from "@/components/CopyButton"
import { useMetadataContext } from "@/context/MetadataContext"
import { useI18n } from "@/hooks/useI18n"
import { cn } from "@/lib/utils"

// 收据行：左标签右数值，长值（单号、哈希）右对齐折行
function ReceiptRow({ label, children, mono }) {
  return (
    <div className="grid grid-cols-[6rem_minmax(0,1fr)] items-start gap-4 py-1.5 text-[13px] leading-5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className={cn("min-w-0 break-words text-right", mono && "break-all font-mono text-xs leading-5")}>
        {children}
      </dd>
    </div>
  )
}

function StepCompleted({ invoice }) {
  const { t } = useI18n()
  const { getChain } = useMetadataContext()

  const confirmingProgress = invoice?.payment?.confirm_progress || {}
  const hasConfirmedCount = confirmingProgress.has_confirmed_count || 0
  const needConfirmedCount = confirmingProgress.need_confirmed_count || 0
  const chainMeta = invoice?.chain ? getChain(invoice.chain) : null
  const orderAmount = [invoice?.amount, invoice?.currency].filter(Boolean).join(" ")
  const hash = invoice?.payment?.hash

  return (
    <section className="overflow-hidden rounded-xl border bg-card shadow-xs animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
      {/* 成功头部：实心品牌色对勾 + 实付金额 */}
      <div className="flex flex-col items-center px-6 pb-7 pt-10 text-center">
        <span className="flex size-11 items-center justify-center rounded-full bg-primary text-primary-foreground animate-in zoom-in-50 duration-500">
          <Check className="size-5" strokeWidth={3} />
        </span>
        <h2 className="mt-4 text-base font-medium tracking-tight">{t("payment.paymentCompleted")}</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">{t("confirmation.transactionConfirmed")}</p>
        {invoice?.pay_amount && (
          <p className="mt-6 flex items-baseline gap-2">
            <span className="text-[2rem] font-semibold leading-none tracking-tight tabular-nums">
              {invoice.pay_amount}
            </span>
            <span className="text-sm font-medium text-muted-foreground">{invoice.crypto}</span>
          </p>
        )}
      </div>

      {/* 收据明细 */}
      <dl className="border-t px-5 py-3 sm:px-6">
        {orderAmount && <ReceiptRow label={t("payment.orderAmount")}>{orderAmount}</ReceiptRow>}
        {chainMeta && (
          <ReceiptRow label={t("payment.network")}>
            <span className="inline-flex items-center gap-1.5">
              <img
                src={chainMeta.icon || undefined}
                alt=""
                className="size-3.5 rounded-full"
                onError={(e) => { e.target.style.visibility = "hidden" }}
              />
              {chainMeta.name}
            </span>
          </ReceiptRow>
        )}
        {needConfirmedCount > 0 && (
          <ReceiptRow label={t("confirmation.blockConfirmation")}>
            <span className="tabular-nums">
              {Math.min(hasConfirmedCount, needConfirmedCount)} / {needConfirmedCount}
            </span>
          </ReceiptRow>
        )}
        {invoice?.title && <ReceiptRow label={t("invoice.subject")}>{invoice.title}</ReceiptRow>}
        {invoice?.out_no && <ReceiptRow label={t("invoice.orderNumber")} mono>{invoice.out_no}</ReceiptRow>}
        {invoice?.sys_no && <ReceiptRow label={t("invoice.systemNumber")} mono>{invoice.sys_no}</ReceiptRow>}
      </dl>

      {hash && (
        <div className="border-t px-5 py-4 sm:px-6">
          <div className="flex h-7 items-center justify-between gap-3">
            <span className="text-[13px] text-muted-foreground">{t("payment.transactionHash")}</span>
            <CopyButton value={hash} />
          </div>
          <code className="mt-1 block break-all font-mono text-xs leading-5 text-muted-foreground">{hash}</code>
        </div>
      )}

      {invoice?.return_url && (
        <div className="border-t px-5 py-4 sm:px-6">
          <Button size="lg" onClick={() => window.open(invoice.return_url, "_blank")} className="w-full">
            {t("payment.returnToMerchant")}
          </Button>
        </div>
      )}
    </section>
  )
}

export default StepCompleted
