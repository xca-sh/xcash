import { Loader2 } from "lucide-react"
import BrandHeading from "@/components/BrandHeading"
import { useI18n } from "@/hooks/useI18n"

function LoadingState() {
  const { t } = useI18n()

  return (
    <div className="min-h-svh bg-background flex flex-col items-center justify-center">
      <div className="flex flex-col items-center gap-8 text-center">
        <BrandHeading size={48} />
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="size-8 animate-spin text-muted-foreground" />
          <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
        </div>
      </div>
    </div>
  )
}

export default LoadingState
