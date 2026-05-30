// src/components/StepIndicator.jsx
import { Check } from "lucide-react"
import { cn } from "@/lib/utils"
import { useI18n } from "@/hooks/useI18n"

const STEP_KEYS = {
  4: ["invoice.stepLabel", "payment.stepLabel", "payment.sendLabel", "invoice.completedLabel"],
  3: ["invoice.stepLabel", "payment.sendLabel", "invoice.completedLabel"],
}

function StepIndicator({ activeStep, naturalStep, onStepClick, stepCount = 4 }) {
  const { t } = useI18n()
  const keys = STEP_KEYS[stepCount] ?? STEP_KEYS[4]
  const nodes = Array.from({ length: stepCount }, (_, i) => i + 1)

  const isDone = (n) => n < naturalStep && n !== activeStep
  const isClickable = (n) => n < naturalStep && n !== activeStep

  return (
    <div className="px-6 pt-4 pb-3 max-w-lg mx-auto">
      {/* Nodes + lines */}
      <div className="flex items-center">
        {nodes.map((n) => (
          <div key={n} className="flex items-center flex-1 last:flex-none">
            <button
              onClick={() => isClickable(n) && onStepClick(n)}
              disabled={!isClickable(n)}
              className={cn(
                "size-7 rounded-full flex items-center justify-center text-xs font-semibold shrink-0 transition-colors outline-none",
                n <= activeStep || isDone(n)
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground border",
                isClickable(n) && "cursor-pointer hover:opacity-90"
              )}
              aria-label={`Step ${n}`}
            >
              {isDone(n) ? <Check className="size-3.5" /> : n}
            </button>
            {n < stepCount && (
              <div
                className={cn(
                  "flex-1 h-px mx-1.5",
                  n < naturalStep ? "bg-primary" : "bg-border"
                )}
              />
            )}
          </div>
        ))}
      </div>

      {/* Labels */}
      <div className="flex justify-between mt-2 px-0.5">
        {nodes.map((n, i) => (
          <div
            key={n}
            className={cn(
              "text-[10px] text-center whitespace-nowrap",
              n === activeStep
                ? "text-foreground font-medium"
                : isDone(n)
                  ? "text-foreground"
                  : "text-muted-foreground"
            )}
          >
            {t(keys[i])}
          </div>
        ))}
      </div>
    </div>
  )
}

export default StepIndicator
