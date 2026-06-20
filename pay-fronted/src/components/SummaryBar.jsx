// src/components/SummaryBar.jsx
import { useEffect, useMemo, useState } from "react"
import { Clock, Moon, Sun } from "lucide-react"
import LogoMark from "@/components/LogoMark"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/hooks/useI18n"
import { getInvoiceDisplayStatus } from "@/lib/invoiceStatus"
import { getRemainingMs } from "@/lib/dateTime"
import { cn } from "@/lib/utils"

// 状态 → Badge 语义变体。只改变用户可见状态色，不影响后端状态值。
const STATUS_VARIANT = {
  waiting: "info",
  confirming: "info",
  finalizing: "info",
  completed: "success",
  expired: "destructive",
}

// 进行中的状态展示一个脉冲点（继承 badge 文字色，非自定义颜色）。
const PULSING = new Set(["waiting", "confirming", "finalizing"])

function formatRemainingTime(remainingMs, t) {
  if (remainingMs === null || typeof remainingMs === "undefined") {
    return "--:--:--"
  }

  const totalSeconds = Math.floor(remainingMs / 1000)
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const pad = (value) => value.toString().padStart(2, "0")

  if (days > 0) {
    return `${days}${t("waiting.days")} ${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
  }

  return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`
}

function InvoiceCountdown({ invoice, t }) {
  const shouldShow = invoice?.status === "waiting" && Boolean(invoice?.expires_at)
  const [remainingMs, setRemainingMs] = useState(() =>
    shouldShow ? getRemainingMs(invoice.expires_at) : null
  )

  useEffect(() => {
    if (!shouldShow) {
      setRemainingMs(null)
      return
    }

    const updateRemaining = () => {
      setRemainingMs(getRemainingMs(invoice.expires_at))
    }

    updateRemaining()
    const timer = setInterval(updateRemaining, 1000)
    return () => clearInterval(timer)
  }, [invoice?.expires_at, shouldShow])

  const countdownTone = useMemo(() => {
    if (remainingMs !== null && remainingMs <= 60_000) return "text-destructive"
    return "text-amber-600 dark:text-amber-400"
  }, [remainingMs])

  const countdownText = useMemo(() => formatRemainingTime(remainingMs, t), [remainingMs, t])

  if (!shouldShow) return null

  return (
    <div
      className={cn(
        "inline-flex min-w-0 items-center justify-center gap-1.5 text-xs font-medium tabular-nums",
        countdownTone
      )}
    >
      <Clock className="size-3.5 shrink-0" />
      <span className="truncate">{t("waiting.timeRemaining")}</span>
      <span className="font-mono font-semibold">
        {remainingMs === 0 ? t("waiting.expired") : countdownText}
      </span>
    </div>
  )
}

function SummaryBar({ invoice, isDark, toggleTheme }) {
  const { t, locale, setLocale } = useI18n()
  const toggleLocale = () => setLocale(locale === "zh" ? "en" : "zh")

  const hasPayMethod = Boolean(invoice?.crypto && invoice?.pay_amount)
  const displayStatus = getInvoiceDisplayStatus(invoice)
  const variant = STATUS_VARIANT[displayStatus] ?? "outline"

  return (
    <div className="border-b px-5 py-3">
      <div className="mx-auto grid max-w-5xl grid-cols-[minmax(0,1fr)_minmax(0,2fr)_minmax(0,1fr)] items-center gap-3">
        {/* Brand */}
        <a
          href="https://xca.sh"
          className="flex min-w-0 items-center gap-2 justify-self-start rounded-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          aria-label="Xcash"
        >
          <LogoMark size={20} />
          <span className="hidden font-semibold text-sm tracking-tight sm:inline">Xcash</span>
        </a>

        {/* Amount */}
        <div className="min-w-0 justify-self-center text-center">
          <div className="flex items-baseline justify-center gap-2 flex-wrap">
            <span className="text-lg font-semibold tabular-nums sm:text-xl">
              {invoice?.amount} {invoice?.currency}
            </span>
            {hasPayMethod && (
              <span className="text-xs font-mono text-muted-foreground tabular-nums">
                ≈ {invoice.pay_amount} {invoice.crypto}
              </span>
            )}
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap items-center justify-center gap-x-2 gap-y-1">
            <Badge variant={variant} className="shrink-0">
              {PULSING.has(displayStatus) && (
                <span className="size-1.5 rounded-full bg-current animate-pulse" />
              )}
              {t(`invoice.status.${displayStatus}`) || displayStatus}
            </Badge>
            <InvoiceCountdown invoice={invoice} t={t} />
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 justify-self-end">
          {/* Locale toggle */}
          <Button
            variant="outline"
            size="icon"
            onClick={toggleLocale}
            className="shrink-0 text-xs font-semibold"
            aria-label="Switch language"
            title={locale === "zh" ? "Switch to English" : "切换到中文"}
          >
            {locale === "zh" ? "EN" : "中"}
          </Button>

          {/* Theme toggle */}
          <Button
            variant="outline"
            size="icon"
            onClick={toggleTheme}
            className="shrink-0"
            aria-label="Toggle theme"
          >
            {isDark ? <Sun /> : <Moon />}
          </Button>
        </div>
      </div>
    </div>
  )
}

export default SummaryBar
