// src/components/StepIndicator.jsx
import { Fragment } from "react"
import { Check } from "lucide-react"
import { cn } from "@/lib/utils"
import { useI18n } from "@/hooks/useI18n"

const STEP_KEYS = {
  3: ["invoice.stepLabel", "payment.sendLabel", "invoice.completedLabel"],
  2: ["payment.sendLabel", "invoice.completedLabel"],
}

// 横向步骤条：序号圆点 + 标签同行，步骤间以细线相连；已走过的步骤可点击回退。
function StepIndicator({ activeStep, naturalStep, onStepClick, stepCount = 3, lockBack = false }) {
  const { t } = useI18n()
  const keys = STEP_KEYS[stepCount] ?? STEP_KEYS[3]
  const nodes = Array.from({ length: stepCount }, (_, i) => i + 1)

  const isFlowComplete = lockBack && naturalStep >= stepCount
  const isDone = (n) => n < naturalStep && n !== activeStep
  const isSuccess = (n) => isDone(n) || (isFlowComplete && n <= naturalStep)
  const isClickable = (n) => !lockBack && n < naturalStep && n !== activeStep

  return (
    <div className="px-4 pt-6 sm:px-8 lg:pt-12">
      <ol className="mx-auto flex max-w-xl items-center gap-3">
        {nodes.map((n, i) => {
          const success = isSuccess(n)
          const active = !success && n === activeStep
          return (
            <Fragment key={n}>
              <li className="shrink-0">
                <button
                  type="button"
                  onClick={() => isClickable(n) && onStepClick?.(n)}
                  disabled={!isClickable(n)}
                  aria-current={n === activeStep ? "step" : undefined}
                  aria-label={`${t("payment.step")} ${n}: ${t(keys[i])}`}
                  className={cn(
                    "group flex items-center gap-2 rounded-md text-[13px] outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                    isClickable(n) && "cursor-pointer"
                  )}
                >
                  <span
                    className={cn(
                      "flex size-5 items-center justify-center rounded-full text-[11px] font-semibold tabular-nums transition-colors",
                      success || active
                        ? "bg-primary text-primary-foreground"
                        : "border text-muted-foreground"
                    )}
                  >
                    {success ? <Check className="size-3" strokeWidth={3} /> : n}
                  </span>
                  <span
                    className={cn(
                      "whitespace-nowrap transition-colors",
                      active ? "font-medium text-foreground" : "text-muted-foreground",
                      isClickable(n) && "group-hover:text-foreground"
                    )}
                  >
                    {t(keys[i])}
                  </span>
                </button>
              </li>
              {n < stepCount && (
                <li aria-hidden="true" className="relative h-px min-w-6 flex-1 bg-border">
                  <span
                    className={cn(
                      "absolute inset-y-0 left-0 bg-primary-ink transition-[width] duration-500",
                      n < naturalStep ? "w-full" : "w-0"
                    )}
                  />
                </li>
              )}
            </Fragment>
          )
        })}
      </ol>
    </div>
  )
}

export default StepIndicator
