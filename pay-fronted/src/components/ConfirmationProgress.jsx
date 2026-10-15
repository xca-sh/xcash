import { useI18n } from "@/hooks/useI18n"
import { cn } from "@/lib/utils"

// 所需确认数不多时按区块逐格展示，过多时格子会挤成细线，退回连续进度条。
const MAX_SEGMENTS = 32

function ConfirmationProgress({ progress, confirmed, required, live = false }) {
  const { t } = useI18n()
  const segmented = required > 0 && required <= MAX_SEGMENTS
  const filled = Math.min(confirmed, required)

  return (
    <div>
      <div className="flex items-baseline justify-between gap-4">
        <span className="text-xs text-muted-foreground">{t("confirmation.blockConfirmation")}</span>
        <span className="text-sm font-medium tabular-nums">
          {required > 0 ? (
            <>
              {filled}
              <span className="text-muted-foreground"> / {required}</span>
            </>
          ) : (
            `${progress}%`
          )}
        </span>
      </div>

      {segmented ? (
        <div className="mt-3 flex gap-1" aria-hidden="true">
          {Array.from({ length: required }, (_, i) => (
            <span
              key={i}
              className={cn(
                "h-1.5 flex-1 rounded-full transition-colors duration-500",
                i < filled ? "bg-primary-ink" : "bg-muted",
                // 下一个待确认的区块轻微闪烁，表示链上仍在推进
                live && i === filled && "animate-pulse bg-primary-ink/40"
              )}
            />
          ))}
        </div>
      ) : (
        <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
          <div
            className="h-full rounded-full bg-primary-ink transition-[width] duration-500"
            style={{ width: `${Math.max(0, Math.min(100, progress))}%` }}
          />
        </div>
      )}
    </div>
  )
}

export default ConfirmationProgress
