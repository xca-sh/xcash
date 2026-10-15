import { AlertCircle } from "lucide-react"
import BrandHeading from "@/components/BrandHeading"
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/hooks/useI18n"

function ErrorState({ error, onRetry }) {
  const { t } = useI18n()

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-background p-4">
      <div className="flex w-full max-w-md flex-col gap-6">
        <div className="flex justify-center">
          <BrandHeading size={36} />
        </div>
        <Alert variant="destructive" className="rounded-xl shadow-sm">
          <AlertCircle />
          <AlertTitle>{t("error.title")}</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
        <Button onClick={onRetry} className="w-full">
          {t("common.retry")}
        </Button>
      </div>
    </div>
  )
}

export default ErrorState
