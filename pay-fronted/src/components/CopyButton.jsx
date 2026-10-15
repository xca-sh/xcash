import { useEffect, useRef, useState } from "react"
import { Check, Copy } from "lucide-react"
import { useI18n } from "@/hooks/useI18n"
import { cn } from "@/lib/utils"

// 带文字的轻量复制按钮：复制成功后 2 秒内显示「已复制」，自管状态，各字段互不干扰。
function CopyButton({ value, className }) {
  const { t } = useI18n()
  const [copied, setCopied] = useState(false)
  const timerRef = useRef(null)

  useEffect(() => () => clearTimeout(timerRef.current), [])

  const handleCopy = () => {
    navigator.clipboard
      .writeText(value)
      .then(() => {
        setCopied(true)
        clearTimeout(timerRef.current)
        timerRef.current = setTimeout(() => setCopied(false), 2000)
      })
      .catch((err) => {
        console.error("Copy failed:", err)
      })
  }

  return (
    <button
      type="button"
      onClick={handleCopy}
      className={cn(
        "-mr-2 inline-flex h-7 shrink-0 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
        copied && "text-primary-ink hover:text-primary-ink",
        className
      )}
    >
      {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
      {copied ? t("common.copied") : t("common.copy")}
    </button>
  )
}

export default CopyButton
