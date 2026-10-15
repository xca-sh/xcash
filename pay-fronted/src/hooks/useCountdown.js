import { useEffect, useState } from "react"
import { getRemainingMs } from "@/lib/dateTime"

// 账单剩余支付时间（毫秒），每秒刷新；enabled 为 false 或无过期时间时返回 null。
export function useCountdown(expiresAt, enabled = true) {
  const active = enabled && Boolean(expiresAt)
  const [remainingMs, setRemainingMs] = useState(() => (active ? getRemainingMs(expiresAt) : null))

  useEffect(() => {
    if (!active) {
      setRemainingMs(null)
      return
    }

    const update = () => setRemainingMs(getRemainingMs(expiresAt))
    update()
    const timer = setInterval(update, 1000)
    return () => clearInterval(timer)
  }, [expiresAt, active])

  return remainingMs
}

// 剩余时间格式化为 HH:MM:SS，超过一天时前置天数。
export function formatRemainingTime(remainingMs, t) {
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
