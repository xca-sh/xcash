import { AlertCircle } from "lucide-react"
import BrandHeading from "@/components/BrandHeading"
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/hooks/useI18n"

function ErrorState({ error, onRetry }) {
  const { t } = useI18n()

  return (
    <div className="min-h-svh bg-background flex flex-col items-center justify-center p-4">
      <div className="w-full max-w-md flex flex-col gap-6">
        <div className="flex justify-center">
          <BrandHeading size={36} />
        </div>
        <Alert variant="destructive">
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
