import { Check, Loader2, AlertCircle } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Separator } from "@/components/ui/separator"
import { cn } from "@/lib/utils"
import TokenSelector from "@/components/TokenSelector"
import ChainSelector from "@/components/ChainSelector"
import { useI18n } from "@/hooks/useI18n"

// 步骤序号圆点：已选 → primary + 勾，未选 → muted + 数字
function StepDot({ active, done, number, disabled }) {
  return (
    <div
      className={cn(
        "mt-0.5 size-6 rounded-full flex items-center justify-center shrink-0 transition-colors",
        done || active
          ? "bg-primary text-primary-foreground"
          : "bg-muted text-muted-foreground border",
        disabled && "opacity-40"
      )}
    >
      {done ? <Check className="size-3.5" /> : <span className="text-[11px] font-bold">{number}</span>}
    </div>
  )
}

function PaymentMethodSelector({
  availableMethods,
  selectedCrypto,
  selectedChain,
  onCryptoChange,
  onChainChange,
  isSelecting,
  isEditing,
  error,
  onCancelEdit,
}) {
  const { t } = useI18n()

  return (
    <div className="flex flex-col gap-1">
      {/* Title */}
      <div className="mb-3">
        <h2 className="text-base font-semibold">{t("payment.selectMethod")}</h2>
        <p className="text-xs text-muted-foreground mt-0.5">{t("payment.selectMethodDesc")}</p>
      </div>

      {/* Step 1: Token */}
      <Card>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-start gap-3">
            <StepDot done={Boolean(selectedCrypto)} number={1} />
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold">{t("payment.selectToken")}</span>
                {selectedCrypto && <Badge variant="secondary">{selectedCrypto}</Badge>}
              </div>
              <p className="text-xs text-muted-foreground mt-0.5">{t("payment.selectTokenDesc")}</p>
            </div>
          </div>
          <TokenSelector
            availableMethods={availableMethods}
            selectedCrypto={selectedCrypto}
            onCryptoChange={onCryptoChange}
            disabled={isSelecting}
          />
        </CardContent>
      </Card>

      {/* Connector */}
      <div className="flex justify-center py-1">
        <Separator orientation="vertical" className="h-5" />
      </div>

      {/* Step 2: Network */}
      <Card className={cn(!selectedCrypto && "opacity-60")}>
        <CardContent className="flex flex-col gap-3">
          <div className="flex items-start gap-3">
            <StepDot
              done={Boolean(selectedChain)}
              number={2}
              disabled={!selectedCrypto && !selectedChain}
            />
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold">{t("payment.selectNetwork")}</span>
                {selectedChain && <Badge variant="secondary">{selectedChain}</Badge>}
              </div>
              <p className="text-xs text-muted-foreground mt-0.5">{t("payment.selectNetworkDesc")}</p>
            </div>
          </div>
          <ChainSelector
            availableMethods={availableMethods}
            selectedCrypto={selectedCrypto}
            selectedChain={selectedChain}
            onChainChange={onChainChange}
            disabled={isSelecting}
          />
        </CardContent>
      </Card>

      {/* Loading */}
      {isSelecting && (
        <div className="mt-2 flex items-center justify-center gap-2.5 py-4 bg-muted rounded-lg">
          <Loader2 className="size-4 animate-spin text-muted-foreground" />
          <p className="text-sm text-muted-foreground">{t("payment.gettingPaymentInfo")}</p>
        </div>
      )}

      {/* Error */}
      {error && !isSelecting && (
        <Alert variant="destructive" className="mt-2">
          <AlertCircle />
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {/* Cancel edit */}
      {isEditing && !isSelecting && (
        <div className="flex justify-end pt-1">
          <Button variant="ghost" onClick={onCancelEdit} size="sm">
            {t("common.cancel")}
          </Button>
        </div>
      )}
    </div>
  )
}

export default PaymentMethodSelector
