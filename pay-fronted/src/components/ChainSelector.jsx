import { useMetadataContext } from "@/context/MetadataContext"
import { sortChainOptions } from "@/lib/paymentMethodSort"
import { useI18n } from "@/hooks/useI18n"
import MethodOption from "@/components/MethodOption"

function ChainSelector({ availableMethods, selectedCrypto, selectedChain, onChainChange, disabled = false }) {
  const { t } = useI18n()
  const { getChain } = useMetadataContext()

  if (!availableMethods || !selectedCrypto || !availableMethods[selectedCrypto]) {
    return (
      <div className="rounded-lg border border-dashed p-4 text-center">
        <p className="text-sm text-muted-foreground">
          {!selectedCrypto ? t("selector.selectTokenFirst") : t("selector.noNetworks")}
        </p>
      </div>
    )
  }

  const chainOptions = sortChainOptions(availableMethods[selectedCrypto])

  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label={t("selector.selectNetwork")}>
      {chainOptions.map((chain) => {
        const chainMeta = getChain(chain)
        return (
          <MethodOption
            key={chain}
            icon={chainMeta.icon}
            title={chainMeta.name}
            subtitle={
              chainMeta.isTestnet ? (
                <span className="text-warning">{t("selector.testNetwork")}</span>
              ) : (
                t("selector.mainNetwork")
              )
            }
            selected={chain === selectedChain}
            disabled={disabled}
            onSelect={() => onChainChange(chain)}
          />
        )
      })}
    </div>
  )
}

export default ChainSelector
