import { useEffect, useState } from "react"
import { Wallet, Loader2, CheckCircle2, AlertCircle } from "lucide-react"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/hooks/useI18n"
import { useWalletPayment } from "@/hooks/useWalletPayment"

// 进行中的状态：禁用按钮并展示对应阶段文案 + spinner。
const BUSY_STATUS = ["connecting", "switching", "sending"]

// 把 normalizeError 的 kind 映射到 i18n 文案 key。
function errorTextKey(error) {
  if (error?.kind === "rejected") return "wallet.userRejected"
  if (error?.kind === "chainNotAdded") return "wallet.switchFailed"
  return "wallet.failed"
}

// 地址缩略展示：0x1234...abcd。
function shortAddress(addr) {
  return addr ? `${addr.slice(0, 6)}...${addr.slice(-4)}` : ""
}

/**
 * 注入式 EVM 钱包一键支付入口。
 *
 * 定位：作为「手动转账（扫码 / 复制地址）」之外的备选支付方式，放在支付卡片底部。
 * 渲染条件：账单支持 evm_payment、仍处于 waiting、尚未观测到链上付款，且确实
 * 检测到了注入式钱包——没装钱包则整体不展示，不做无效引导。
 */
function WalletPay({ invoice, onBroadcast }) {
  const { t } = useI18n()
  const { wallets, status, error, account, pay, switchAccount } =
    useWalletPayment(invoice)
  // 多钱包时点击主按钮先展开选择列表，再对选中的 provider 发起支付。
  const [choosing, setChoosing] = useState(false)

  // 广播成功后通知上层，让底部总状态卡同步为「等待区块确认」。
  useEffect(() => {
    if (status === "submitted") onBroadcast?.()
  }, [status, onBroadcast])

  const shouldRender =
    Boolean(invoice?.evm_payment) &&
    invoice?.status === "waiting" &&
    !invoice?.payment
  // 未检测到任何注入式钱包时不展示入口（EIP-6963 异步发现，检测到后自然出现）。
  if (!shouldRender || wallets.length === 0) {
    return null
  }

  // 已提交：只提示等待确认，绝不显示「已支付」；此时已不是备选项，独立展示。
  if (status === "submitted") {
    return (
      <div className="flex items-center justify-center gap-2 rounded-lg bg-success-soft p-3 text-sm text-success">
        <CheckCircle2 className="size-4 shrink-0" />
        <span>{t("wallet.submitted")}</span>
      </div>
    )
  }

  const isBusy = BUSY_STATUS.includes(status)

  const handlePrimary = () => {
    // 单钱包直接付；多钱包展开选择。
    if (wallets.length === 1) {
      pay(wallets[0].provider)
    } else {
      setChoosing(true)
    }
  }

  const handlePick = (wallet) => {
    setChoosing(false)
    pay(wallet.provider)
  }

  return (
    <div className="flex flex-col gap-3">
      {/* 「或」分隔线：与上方手动转账区隔开，标明这是另一种支付方式。 */}
      <div className="flex items-center gap-3">
        <span className="h-px flex-1 bg-border" />
        <span className="text-xs text-muted-foreground">{t("wallet.or")}</span>
        <span className="h-px flex-1 bg-border" />
      </div>

      {isBusy ? (
        <Button className="w-full" disabled>
          <Loader2 className="size-4 animate-spin" />
          {t(`wallet.${status}`)}
        </Button>
      ) : choosing ? (
        // 多钱包内联选择：每个钱包一行（图标 + 名称）。
        <div className="flex flex-col gap-2">
          <span className="text-xs font-medium text-muted-foreground">
            {t("wallet.chooseWallet")}
          </span>
          {wallets.map((wallet) => (
            <Button
              key={wallet.info.rdns}
              variant="outline"
              className="w-full justify-start"
              onClick={() => handlePick(wallet)}
            >
              {wallet.info.icon ? (
                <img
                  src={wallet.info.icon}
                  alt=""
                  className="size-5 rounded"
                  onError={(e) => {
                    e.target.style.visibility = "hidden"
                  }}
                />
              ) : (
                <Wallet className="size-4" />
              )}
              {wallet.info.name}
            </Button>
          ))}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <Button className="w-full" onClick={handlePrimary}>
            <Wallet className="size-4" />
            {t("wallet.payWithWallet")}
          </Button>
          {/* 单钱包时展示「将用哪个账户付款」并提供切换入口；默认跟随钱包当前账户，
              切换账户经 wallet_requestPermissions 弹出钱包账户选择器。 */}
          {wallets.length === 1 && (
            <div className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
              {account && <span className="font-mono">{shortAddress(account)}</span>}
              <button
                type="button"
                className="underline underline-offset-2 hover:text-foreground"
                onClick={() => switchAccount(wallets[0].provider)}
              >
                {t("wallet.switchAccount")}
              </button>
            </div>
          )}
        </div>
      )}

      {/* 错误提示：用户取消/切链失败/通用失败，文案随 kind 切换，按钮本身即可重试。 */}
      {status === "error" && (
        <p className="flex items-center justify-center gap-2 text-xs text-destructive">
          <AlertCircle className="size-3.5 shrink-0" />
          <span>{t(errorTextKey(error))}</span>
        </p>
      )}
    </div>
  )
}

export default WalletPay
