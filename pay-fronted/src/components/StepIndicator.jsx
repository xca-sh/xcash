// src/components/StepIndicator.jsx
import { Check } from "lucide-react"
import { cn } from "@/lib/utils"
import { useI18n } from "@/hooks/useI18n"

const STEP_KEYS = {
  3: ["invoice.stepLabel", "payment.sendLabel", "invoice.completedLabel"],
  2: ["payment.sendLabel", "invoice.completedLabel"],
}

function StepIndicator({ activeStep, naturalStep, onStepClick, stepCount = 3, lockBack = false }) {
  const { t } = useI18n()
  const keys = STEP_KEYS[stepCount] ?? STEP_KEYS[3]
  const nodes = Array.from({ length: stepCount }, (_, i) => i + 1)
  const gridStyle = { gridTemplateColumns: `repeat(${stepCount}, minmax(0, 1fr))` }

  const isDone = (n) => n < naturalStep && n !== activeStep
  const isClickable = (n) => !lockBack && n < naturalStep && n !== activeStep

  return (
    <div className="px-6 pt-4 pb-3 max-w-lg mx-auto">
      {/* Nodes + lines */}
      <div className="grid items-center" style={gridStyle}>
        {nodes.map((n, i) => (
          <div key={n} className="relative flex justify-center">
            <button
              onClick={() => isClickable(n) && onStepClick?.(n)}
              disabled={!isClickable(n)}
              className={cn(
                "relative z-10 size-7 rounded-full flex items-center justify-center text-xs font-semibold shrink-0 transition-colors outline-none",
                n <= activeStep || isDone(n)
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground border",
                isClickable(n) && "cursor-pointer hover:opacity-90"
              )}
              aria-label={`${t("payment.step")} ${n}: ${t(keys[i])}`}
            >
              {isDone(n) ? <Check className="size-3.5" /> : n}
            </button>
            {n < stepCount && (
              <div
                className={cn(
                  "absolute top-1/2 left-[calc(50%+1rem)] right-[calc(-50%+1rem)] h-px -translate-y-1/2",
                  n < naturalStep ? "bg-primary" : "bg-border"
                )}
              />
            )}
          </div>
        ))}
      </div>

      {/* Labels */}
      <div className="grid mt-2" style={gridStyle}>
        {nodes.map((n, i) => (
          <div
            key={n}
            className={cn(
              "min-w-0 px-1 text-center text-[10px] leading-tight whitespace-nowrap",
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
