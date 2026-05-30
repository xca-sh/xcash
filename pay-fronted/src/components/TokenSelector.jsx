import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { getCryptoIconUrl, getCryptoDisplayName } from "@/lib/cryptoIcons"
import { useI18n } from "@/hooks/useI18n"

function TokenSelector({ availableMethods, selectedCrypto, onCryptoChange, disabled = false }) {
  const { t } = useI18n()

  if (!availableMethods || Object.keys(availableMethods).length === 0) {
    return (
      <div className="p-4 border border-dashed rounded-md text-center">
        <p className="text-sm text-muted-foreground">{t("selector.noTokens")}</p>
      </div>
    )
  }

  const tokenOptions = Object.keys(availableMethods)

  return (
    <Select value={selectedCrypto} onValueChange={onCryptoChange} disabled={disabled}>
      <SelectTrigger className="w-full">
        <SelectValue placeholder={t("selector.selectToken")} />
      </SelectTrigger>
      <SelectContent>
        {tokenOptions.map((token) => (
          <SelectItem key={token} value={token}>
            <img
              src={getCryptoIconUrl(token)}
              alt=""
              className="size-5 rounded-full"
              onError={(e) => { e.target.style.visibility = "hidden" }}
            />
            <span className="font-medium">{getCryptoDisplayName(token)}</span>
            <span className="text-muted-foreground">
              · {availableMethods[token].length} {t("selector.networks")}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

export default TokenSelector
