import { Check } from "lucide-react"
import { cn } from "@/lib/utils"

// 加密货币 / 网络选择共用的单选项：图标 + 名称/副标题 + 右侧单选圆点。
// 选中态用主色文字色描边，单选圆点为品牌实心填充配深色对勾（与 saas 选中项一致）。
function MethodOption({ icon, title, subtitle, selected, disabled, onSelect }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      disabled={disabled}
      onClick={onSelect}
      className={cn(
        "group flex min-h-14 w-full items-center gap-3 rounded-lg border bg-card px-3.5 py-2.5 text-left transition-colors",
        "hover:border-foreground/25 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        selected && "border-primary-ink bg-primary-ink/[0.04] ring-1 ring-primary-ink hover:border-primary-ink",
        disabled && "cursor-not-allowed opacity-60"
      )}
    >
      <img
        src={icon || undefined}
        alt=""
        className="size-8 shrink-0 rounded-full"
        onError={(e) => { e.target.style.visibility = "hidden" }}
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium">{title}</span>
        {subtitle && <span className="mt-0.5 block truncate text-xs text-muted-foreground">{subtitle}</span>}
      </span>
      <span
        className={cn(
          "flex size-[18px] shrink-0 items-center justify-center rounded-full border transition-colors",
          selected
            ? "border-transparent bg-primary text-primary-foreground"
            : "text-transparent group-hover:border-foreground/30"
        )}
      >
        <Check className="size-3" strokeWidth={3} />
      </span>
    </button>
  )
}

export default MethodOption
