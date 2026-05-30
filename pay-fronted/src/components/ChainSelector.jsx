import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { getChainIconUrl, getChainDisplayName, isTestnet } from "@/lib/cryptoIcons"
import { useI18n } from "@/hooks/useI18n"

function ChainSelector({ availableMethods, selectedCrypto, selectedChain, onChainChange, disabled = false }) {
  const { t } = useI18n()

  if (!availableMethods || !selectedCrypto || !availableMethods[selectedCrypto]) {
    return (
      <div className="p-4 border border-dashed rounded-md text-center">
        <p className="text-sm text-muted-foreground">
          {!selectedCrypto ? t("selector.selectTokenFirst") : t("selector.noNetworks")}
        </p>
      </div>
    )
  }

  const chainOptions = availableMethods[selectedCrypto]

  return (
    <Select value={selectedChain} onValueChange={onChainChange} disabled={disabled}>
      <SelectTrigger className="w-full">
        <SelectValue placeholder={t("selector.selectNetwork")} />
      </SelectTrigger>
      <SelectContent>
        {chainOptions.map((chain) => (
          <SelectItem key={chain} value={chain}>
            <img
              src={getChainIconUrl(chain)}
              alt=""
              className="size-5 rounded-full"
              onError={(e) => { e.target.style.visibility = "hidden" }}
            />
            <span className="font-medium">{getChainDisplayName(chain)}</span>
            <span className="text-muted-foreground">
              · {isTestnet(chain) ? t("selector.testNetwork") : t("selector.mainNetwork")}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

export default ChainSelector
