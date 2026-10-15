import { useI18n } from "@/hooks/useI18n"

/**
 * 等待状态条 - waiting 状态
 * 支付卡片下方的一行轻提示，不再单独成框，避免与支付卡片争夺视觉焦点。
 * broadcasted 为 true 表示用户已通过钱包广播交易，
 * 文案切到「等待区块确认」，与支付卡片内「交易已提交」保持一致。
 */
function WaitingPayment({ broadcasted }) {
  const { t } = useI18n()

  return (
    <div
      role="status"
      className="flex items-start justify-center gap-2.5 px-2 text-[13px] leading-5 animate-in fade-in-0 duration-500"
    >
      <span className="relative mt-1.5 flex size-2 shrink-0">
        <span className="absolute inline-flex size-full animate-ping rounded-full bg-primary-ink opacity-50" />
        <span className="relative inline-flex size-2 rounded-full bg-primary-ink" />
      </span>
      <p>
        <span className="font-medium">
          {t(broadcasted ? "waiting.broadcastTitle" : "waiting.title")}
        </span>
        <span className="text-muted-foreground">
          {" · "}
          {t(broadcasted ? "waiting.broadcastDescription" : "waiting.description")}
        </span>
      </p>
    </div>
  )
}

export default WaitingPayment
