import { Loader2, AlertCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { cn } from "@/lib/utils"
import TokenSelector from "@/components/TokenSelector"
import ChainSelector from "@/components/ChainSelector"
import { useI18n } from "@/hooks/useI18n"

// 小节标题：序号 + 标题 + 说明；未轮到的小节整体弱化
function SectionHeading({ number, title, desc, dimmed }) {
  return (
    <div className={cn("mb-3 flex items-baseline gap-2.5 transition-opacity", dimmed && "opacity-50")}>
      <span className="font-mono text-xs tabular-nums text-muted-foreground">0{number}</span>
      <div className="min-w-0">
        <h3 className="text-sm font-medium">{title}</h3>
        <p className="mt-0.5 text-xs text-muted-foreground">{desc}</p>
      </div>
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
    <section className="overflow-hidden rounded-xl border bg-card shadow-xs">
      <header className="px-5 pt-5 sm:px-6 sm:pt-6">
        <h2 className="text-[15px] font-medium leading-6 tracking-tight">{t("payment.selectMethod")}</h2>
        <p className="mt-0.5 text-[13px] leading-5 text-muted-foreground">{t("payment.selectMethodDesc")}</p>
      </header>

      <div className="px-5 py-6 sm:px-6">
        <SectionHeading number={1} title={t("payment.selectToken")} desc={t("payment.selectTokenDesc")} />
        <TokenSelector
          availableMethods={availableMethods}
          selectedCrypto={selectedCrypto}
          onCryptoChange={onCryptoChange}
          disabled={isSelecting}
        />
      </div>

      <div className="border-t px-5 py-6 sm:px-6">
        <SectionHeading
          number={2}
          title={t("payment.selectNetwork")}
          desc={t("payment.selectNetworkDesc")}
          dimmed={!selectedCrypto}
        />
        <ChainSelector
          availableMethods={availableMethods}
          selectedCrypto={selectedCrypto}
          selectedChain={selectedChain}
          onChainChange={onChainChange}
          disabled={isSelecting}
        />
      </div>

      {(isSelecting || error || isEditing) && (
        <div className="flex flex-col gap-3 border-t px-5 py-4 sm:px-6">
          {isSelecting && (
            <div className="flex items-center justify-center gap-2 py-1 text-[13px] text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              {t("payment.gettingPaymentInfo")}
            </div>
          )}

          {error && !isSelecting && (
            <Alert variant="destructive">
              <AlertCircle />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          {isEditing && !isSelecting && (
            <Button variant="ghost" onClick={onCancelEdit} size="sm" className="w-full text-muted-foreground">
              {t("common.cancel")}
            </Button>
          )}
        </div>
      )}
    </section>
  )
}

export default PaymentMethodSelector
