import { useEffect, useState, useMemo } from "react"
import { Loader2 } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { cn } from "@/lib/utils"
import { useI18n } from "@/hooks/useI18n"

/**
 * 格式化剩余时间
 */
const formatRemainingTime = (remainingMs, t) => {
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

/**
 * 获取剩余时间(毫秒)
 */
const getRemainingMs = (expiresAt) => {
  if (!expiresAt) return null
  const expireTimestamp = new Date(expiresAt).getTime()
  if (Number.isNaN(expireTimestamp)) return null
  return Math.max(0, expireTimestamp - Date.now())
}

/**
 * 等待支付组件 - waiting 状态
 * 用户还未付款,显示倒计时
 */
function WaitingPayment({ invoice, onExpired }) {
  const { t } = useI18n()
  const [remainingMs, setRemainingMs] = useState(() => getRemainingMs(invoice?.expires_at))

  // 更新倒计时
  useEffect(() => {
    if (!invoice?.expires_at) {
      setRemainingMs(null)
      return
    }

    const updateRemaining = () => {
      setRemainingMs(getRemainingMs(invoice.expires_at))
    }

    updateRemaining()
    const timer = setInterval(updateRemaining, 1000)

    return () => clearInterval(timer)
  }, [invoice?.expires_at])

  // 倒计时归零时立即刷新账单状态，避免用户看到 "expired" 文字却页面不变
  useEffect(() => {
    if (remainingMs === 0 && onExpired) {
      onExpired()
    }
  }, [remainingMs, onExpired])

  // 剩余不足一分钟时用 destructive 语义色提示紧迫，其余用常规前景色
  const countdownTone = useMemo(() => {
    if (remainingMs !== null && remainingMs <= 60_000) return "text-destructive"
    return "text-foreground"
  }, [remainingMs])

  const countdownText = useMemo(() => formatRemainingTime(remainingMs, t), [remainingMs, t])

  return (
    <Card className="animate-in fade-in-0 slide-in-from-bottom-4 duration-500">
      <CardContent>
        <div className="flex flex-col items-center gap-4">
          <Loader2 className="size-10 animate-spin text-muted-foreground" />

          <div className="text-center">
            <p className="font-medium">{t("waiting.title")}</p>
            <p className="text-sm text-muted-foreground mt-1">{t("waiting.description")}</p>
          </div>

          {/* Countdown */}
          {invoice?.expires_at && (
            <div className="w-full bg-muted rounded-lg p-3">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">
                  {t("waiting.timeRemaining")}
                </span>
                <span className={cn("font-mono font-bold text-base tabular-nums", countdownTone)}>
                  {remainingMs === 0 ? t("waiting.expired") : countdownText}
                </span>
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  )
}

export default WaitingPayment
