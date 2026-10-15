import { useMetadataContext } from "@/context/MetadataContext"
import { sortCryptoOptions } from "@/lib/paymentMethodSort"
import { useI18n } from "@/hooks/useI18n"
import MethodOption from "@/components/MethodOption"

function TokenSelector({ availableMethods, selectedCrypto, onCryptoChange, disabled = false }) {
  const { t } = useI18n()
  const { getCrypto } = useMetadataContext()

  if (!availableMethods || Object.keys(availableMethods).length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-4 text-center">
        <p className="text-sm text-muted-foreground">{t("selector.noTokens")}</p>
      </div>
    )
  }

  const tokenOptions = sortCryptoOptions(Object.keys(availableMethods))

  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2" role="radiogroup" aria-label={t("selector.selectToken")}>
      {tokenOptions.map((token) => {
        const cryptoMeta = getCrypto(token)
        return (
          <MethodOption
            key={token}
            icon={cryptoMeta.icon}
            title={token}
            subtitle={`${availableMethods[token].length} ${t("selector.networks")}`}
            selected={token === selectedCrypto}
            disabled={disabled}
            onSelect={() => onCryptoChange(token)}
          />
        )
      })}
    </div>
  )
}

export default TokenSelector
