import { Loader2 } from "lucide-react"
import BrandHeading from "@/components/BrandHeading"
import { useI18n } from "@/hooks/useI18n"

function LoadingState() {
  const { t } = useI18n()

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-10 text-center">
        <BrandHeading size={44} />
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="size-5 animate-spin text-muted-foreground" />
          <p className="text-sm text-muted-foreground">{t("common.loading")}</p>
        </div>
      </div>
    </div>
  )
}

export default LoadingState
