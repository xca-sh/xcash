// src/components/StepInvoice.jsx
import { AlertCircle } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert"
import { useI18n } from "@/hooks/useI18n"

function StepInvoice({ invoice, onConfirm, isExpired, isSingleMethod }) {
  const { t } = useI18n()

  return (
    <div className="flex flex-col gap-4 animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">{t("invoice.title")}</CardTitle>
          <CardDescription>
            {t("invoice.orderNumber")}:{" "}
            <span className="font-mono">{invoice.out_no}</span>
          </CardDescription>
        </CardHeader>

        <CardContent className="flex flex-col gap-5">
          {/* Title */}
          <div className="pb-4 border-b">
            <h3 className="text-sm font-semibold">{invoice.title}</h3>
            <p className="text-xs text-muted-foreground mt-1 font-mono">
              {t("invoice.systemNumber")}: {invoice.sys_no}
            </p>
          </div>

          {/* Amount */}
          <div className="bg-muted rounded-lg p-5">
            <div className="text-xs font-medium text-muted-foreground uppercase tracking-wide mb-3">
              {t("invoice.amountDue")}
            </div>
            <div className="text-3xl font-bold tabular-nums">{invoice.amount}</div>
            <div className="text-sm text-muted-foreground mt-1">{invoice.currency}</div>
          </div>
        </CardContent>
      </Card>

      {/* CTA — hidden when expired */}
      {!isExpired && (
        <>
          <Button onClick={onConfirm} className="w-full">
            {isSingleMethod
              ? (t("payment.confirmAndPay") || "确认并支付账单 →")
              : (t("payment.confirmAndSelectMethod") || "确认并选择支付方式 →")
            }
          </Button>
          <p className="text-xs text-muted-foreground text-center px-4">
            {t("invoice.paymentIrreversible") || "请仔细核对金额与订单信息，支付后无法撤销"}
          </p>
        </>
      )}

      {/* Expired */}
      {isExpired && (
        <>
          <Alert variant="destructive">
            <AlertCircle />
            <AlertTitle>{t("expired.orderExpired")}</AlertTitle>
            <AlertDescription>{t("expired.contactMerchant")}</AlertDescription>
          </Alert>
          <Button
            onClick={() => window.location.reload()}
            variant="outline"
            className="w-full"
          >
            {t("expired.refreshPage")}
          </Button>
        </>
      )}
    </div>
  )
}

export default StepInvoice
