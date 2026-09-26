import { useEffect, useMemo, useState } from "react"
import { Clock, Moon, Sun, ShieldCheck, Globe, ChevronDown } from "lucide-react"
import LogoMark from "@/components/LogoMark"
import { useI18n, SUPPORTED_LOCALES } from "@/hooks/useI18n"
import { getInvoiceDisplayStatus } from "@/lib/invoiceStatus"
import { getRemainingMs } from "@/lib/dateTime"
import { cn } from "@/lib/utils"

// 状态 → 深色面板上的状态点颜色（面板始终是近黑底，浅色圆点保证可读性）
const STATUS_DOT = {
  waiting: "bg-[#7cb8ff]",
  confirming: "bg-[#7cb8ff]",
  finalizing: "bg-[#7cb8ff]",
  completed: "bg-[#72e857]",
  expired: "bg-[#ff5f57]",
}

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

  const countdownText = useMemo(() => formatRemainingTime(remainingMs, t), [remainingMs, t])

  if (!shouldShow) return null

  const isUrgent = remainingMs !== null && remainingMs <= 60_000

  return (
    <span
      className={cn(
        "inline-flex min-w-0 items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium tabular-nums",
        isUrgent
          ? "border-[#ff5f57]/30 bg-[#ff5f57]/10 text-[#ff8a84]"
          : "border-white/15 bg-white/5 text-[#fbbf24]"
      )}
    >
      <Clock className="size-3.5 shrink-0" />
      <span className="font-mono font-semibold">
        {remainingMs === 0 ? t("waiting.expired") : countdownText}
      </span>
    </span>
  )
}

// 深色面板上的幽灵图标按钮（语言/主题切换）
function PanelIconButton({ onClick, label, title, children }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      title={title}
      className="flex size-8 items-center justify-center rounded-full border border-white/15 bg-white/5 text-xs font-semibold text-white/80 transition-colors hover:bg-white/15 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
    >
      {children}
    </button>
  )
}

function LanguageDropdown({ locale, setLocale }) {
  const [open, setOpen] = useState(false)
  const current = SUPPORTED_LOCALES.find((l) => l.code === locale) || SUPPORTED_LOCALES[0]

  useEffect(() => {
    if (!open) return
    const close = (e) => {
      if (!e.target.closest("[data-lang-dropdown]")) setOpen(false)
    }
    document.addEventListener("click", close)
    return () => document.removeEventListener("click", close)
  }, [open])

  return (
    <div className="relative" data-lang-dropdown>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-label="Select language"
        className="flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-3 py-1.5 text-xs font-semibold text-white/80 transition-colors hover:bg-white/15 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
      >
        <Globe className="size-3.5" />
        <span>{current.label}</span>
        <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="absolute right-0 top-full z-50 mt-1.5 min-w-[10rem] overflow-hidden rounded-lg border border-white/15 bg-[#1a1a2e] shadow-xl animate-in fade-in-0 zoom-in-95">
          {SUPPORTED_LOCALES.map((l) => (
            <button
              key={l.code}
              type="button"
              onClick={() => {
                setLocale(l.code)
                setOpen(false)
              }}
              className={cn(
                "flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm transition-colors hover:bg-white/10",
                l.code === locale
                  ? "bg-white/10 text-white font-medium"
                  : "text-white/70"
              )}
            >
              <span className="text-xs font-bold leading-none w-5 opacity-70">{l.flag}</span>
              <span>{l.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function OrderSummaryPanel({ invoice, isDark, toggleTheme }) {
  const { t, locale, setLocale } = useI18n()

  const hasPayMethod = Boolean(invoice?.crypto && invoice?.pay_amount)
  const displayStatus = getInvoiceDisplayStatus(invoice)

  const detailRows = [
    invoice?.title && { label: t("invoice.subject"), value: invoice.title },
    invoice?.out_no && { label: t("invoice.orderNumber"), value: invoice.out_no, mono: true },
    invoice?.sys_no && { label: t("invoice.systemNumber"), value: invoice.sys_no, mono: true },
  ].filter(Boolean)

  return (
    <aside className="brand-panel relative overflow-hidden text-white lg:sticky lg:top-0 lg:flex lg:h-svh lg:w-[26rem] lg:shrink-0 lg:flex-col lg:border-r lg:border-white/10">
      {/* 顶部：品牌 + 语言/主题切换 */}
      <div className="flex items-center justify-between px-5 pt-5 sm:px-6 lg:px-8 lg:pt-8">
        <a
          href="https://xca.sh"
          className="flex items-center gap-2.5 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-white/40"
          aria-label="Xcash"
        >
          <span className="flex size-8 items-center justify-center rounded-lg bg-white/10 ring-1 ring-white/15">
            <LogoMark size={20} />
          </span>
          <span className="text-sm font-semibold tracking-tight">Xcash</span>
        </a>
        <div className="flex items-center gap-2">
          <LanguageDropdown locale={locale} setLocale={setLocale} />
          <PanelIconButton onClick={toggleTheme} label="Toggle theme" title="Toggle theme">
            {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </PanelIconButton>
        </div>
      </div>

      {/* 金额区 */}
      <div className="px-5 pb-6 pt-6 sm:px-6 lg:flex lg:flex-1 lg:flex-col lg:px-8 lg:pb-0 lg:pt-14">
        <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-white/50">
          {t("invoice.amountDue")}
        </p>
        <div className="mt-2.5 flex items-baseline gap-2">
          <span className="text-4xl font-bold tracking-tight tabular-nums lg:text-5xl">
            {invoice?.amount}
          </span>
          <span className="text-lg font-semibold text-white/60">{invoice?.currency}</span>
        </div>
        {hasPayMethod && (
          <p className="mt-2 font-mono text-sm tabular-nums text-white/70">
            ≈ {invoice.pay_amount} {invoice.crypto}
          </p>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-2">
          <span className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-2.5 py-1 text-xs font-medium text-white/90">
            <span
              className={cn(
                "size-1.5 rounded-full",
                STATUS_DOT[displayStatus] ?? "bg-white/60",
                PULSING.has(displayStatus) && "animate-pulse"
              )}
            />
            {t(`invoice.status.${displayStatus}`) || displayStatus}
          </span>
          <InvoiceCountdown invoice={invoice} t={t} />
        </div>

        {/* 桌面端：订单明细 */}
        {detailRows.length > 0 && (
          <dl className="mt-12 hidden space-y-5 border-t border-white/10 pt-8 lg:block">
            {detailRows.map((row) => (
              <div key={row.label} className="flex items-start justify-between gap-6">
                <dt className="shrink-0 text-xs text-white/50">{row.label}</dt>
                <dd
                  className={cn(
                    "min-w-0 text-right text-sm text-white/90",
                    row.mono ? "break-all font-mono text-xs leading-5" : "break-words font-medium"
                  )}
                >
                  {row.value}
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {/* 桌面端底部：安全背书 */}
      <div className="hidden items-center gap-2 px-8 pb-8 text-xs text-white/45 lg:flex">
        <ShieldCheck className="size-4 shrink-0" />
        <span>{t("common.tagline")}</span>
      </div>
    </aside>
  )
}

export default OrderSummaryPanel
