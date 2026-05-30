// src/components/StepCompleted.jsx
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Progress } from "@/components/ui/progress"
import { CheckCircle2 } from "lucide-react"
import { useI18n } from "@/hooks/useI18n"

function StepCompleted({ invoice }) {
  const { t } = useI18n()

  const confirmingProgress = invoice?.payment?.confirm_progress || {}
  const progress = confirmingProgress.progress || 0
  const hasConfirmedCount = confirmingProgress.has_confirmed_count || 0
  const needConfirmedCount = confirmingProgress.need_confirmed_count || 0

  return (
    <div className="animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
      <Card>
        <CardContent className="flex flex-col gap-4">
          {/* Success header */}
          <div className="text-center flex flex-col items-center gap-3">
            <div className="size-16 bg-muted rounded-full flex items-center justify-center animate-in zoom-in-50 duration-500">
              <CheckCircle2 className="size-8" />
            </div>
            <div>
              <h2 className="text-xl font-bold">
                {t("payment.paymentCompleted") || "支付成功！"}
              </h2>
              <p className="text-sm text-muted-foreground mt-1">
                {t("confirmation.transactionConfirmed") || "区块链交易已确认"}
              </p>
            </div>
          </div>

          {/* Block confirmation progress */}
          <div className="bg-muted rounded-lg p-4 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium">
                {t("confirmation.blockConfirmation") || "区块确认"}
              </span>
              <span className="text-sm font-bold font-mono tabular-nums">{progress}%</span>
            </div>
            <Progress value={progress} />
            <div className="flex justify-between text-xs text-muted-foreground">
              <span>{t("confirmation.confirmed") || "已确认"} {hasConfirmedCount} {t("confirmation.blocks") || "区块"}</span>
              <span>{t("confirmation.needs") || "需要"} {needConfirmedCount} {t("confirmation.blocks") || "区块"}</span>
            </div>
          </div>

          {/* Amount summary */}
          <div className="bg-muted rounded-lg p-4 flex justify-between items-center">
            <div>
              <div className="text-xs text-muted-foreground mb-1">{t("invoice.amountDue") || "实付金额"}</div>
              <div className="font-mono font-bold tabular-nums text-lg">
                {invoice?.pay_amount} {invoice?.crypto}
              </div>
            </div>
            <div className="text-right">
              <div className="text-xs text-muted-foreground mb-1">{invoice?.currency}</div>
              <div className="font-bold text-lg">{invoice?.amount}</div>
            </div>
          </div>

          {/* Transaction hash */}
          {invoice?.payment?.hash && (
            <div className="flex flex-col gap-2">
              <span className="text-xs font-medium text-muted-foreground uppercase tracking-wide">
                {t("payment.transactionHash") || "交易哈希"}
              </span>
              <code className="block break-all bg-muted rounded-lg p-3 text-xs font-mono text-muted-foreground leading-relaxed">
                {invoice.payment.hash}
              </code>
            </div>
          )}

          {/* Return to merchant */}
          {invoice?.return_url && (
            <Button onClick={() => window.open(invoice.return_url, "_blank")} className="w-full">
              {t("payment.returnToMerchant") || "返回商户"}
            </Button>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

export default StepCompleted
