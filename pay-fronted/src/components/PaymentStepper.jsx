// src/components/PaymentStepper.jsx
import { useState, useEffect, useMemo, useRef } from "react"
import { Loader2 } from "lucide-react"
import SummaryBar from "@/components/SummaryBar"
import StepIndicator from "@/components/StepIndicator"
import StepInvoice from "@/components/StepInvoice"
import StepCompleted from "@/components/StepCompleted"
import PaymentMethodSelector from "@/components/PaymentMethodSelector"
import PaymentAddress from "@/components/PaymentAddress"
import WaitingPayment from "@/components/WaitingPayment"
import { useI18n } from "@/hooks/useI18n"

function PaymentStepper({
  invoice,
  selectedCrypto,
  selectedChain,
  isSelecting,
  isEditing,
  paymentError,
  handleCryptoChange,
  handleChainChange,
  resetSelection,
  cancelEdit,
  refetch,
  isDark,
  toggleTheme,
}) {
  const { t } = useI18n()
  const hasPaymentMethod = Boolean(
    invoice.crypto && invoice.chain && invoice.pay_address && invoice.pay_amount
  )
  const hasPayment = Boolean(invoice.payment)
  const isCompleted = invoice.status === "completed"
  const isConfirming = invoice.status === "confirming"
  const isWaiting = invoice.status === "waiting"
  const isExpired = invoice.status === "expired"
  const availableMethods = invoice.methods ?? {}

  // Detect single-method: 1 token with 1 chain → skip selection step, show 3-step flow
  const methodTokens = Object.keys(availableMethods)
  const isSingleMethod = methodTokens.length === 1 && availableMethods[methodTokens[0]]?.length === 1
  const singleToken = isSingleMethod ? methodTokens[0] : null
  const singleChain = isSingleMethod ? availableMethods[methodTokens[0]][0] : null
  const stepCount = isSingleMethod ? 3 : 4

  // 如果账单已有 payment（链上已付款 / 确认中 / 已完成），跳过账单确认步骤
  const [invoiceConfirmed, setInvoiceConfirmed] = useState(hasPayment)

  // Auto-select token when single-method and invoice confirmed
  useEffect(() => {
    if (isSingleMethod && invoiceConfirmed && !selectedCrypto && !isSelecting) {
      handleCryptoChange(singleToken)
    }
  }, [isSingleMethod, invoiceConfirmed, selectedCrypto, isSelecting, singleToken])

  // Auto-select chain once token is set (requires invoice confirmed to prevent firing on page load)
  useEffect(() => {
    if (isSingleMethod && invoiceConfirmed && selectedCrypto && !selectedChain && !isSelecting) {
      handleChainChange(singleChain)
    }
  }, [isSingleMethod, invoiceConfirmed, selectedCrypto, selectedChain, isSelecting, singleChain])

  const naturalStep = useMemo(() => {
    if (isExpired) return 1
    if (isSingleMethod) {
      if (isCompleted) return 3
      if (isConfirming || (hasPaymentMethod && !isEditing)) return 2
      return 1
    }
    if (isCompleted) return 4
    if (isConfirming || (hasPaymentMethod && !isEditing)) return 3
    if (invoiceConfirmed || isEditing || hasPaymentMethod) return 2
    return 1
  }, [isCompleted, isConfirming, hasPaymentMethod, isEditing, isExpired, invoiceConfirmed, isSingleMethod])

  // 有 payment 时直接跳到当前 naturalStep，否则从第 1 步开始
  const initialStep = hasPayment ? naturalStep : 1
  const [activeStep, setActiveStep] = useState(initialStep)
  const maxNaturalStepRef = useRef(initialStep)

  // Auto-advance only after user confirms invoice, and only when server state moves forward
  useEffect(() => {
    if (!invoiceConfirmed) return
    if (naturalStep > maxNaturalStepRef.current) {
      maxNaturalStepRef.current = naturalStep
      setActiveStep(naturalStep)
    } else {
      maxNaturalStepRef.current = Math.max(maxNaturalStepRef.current, naturalStep)
    }
  }, [naturalStep, invoiceConfirmed])

  const handleStepClick = (step) => {
    if (step >= naturalStep) return
    if (!isSingleMethod && step === 2 && naturalStep >= 3) {
      resetSelection()
    }
    // 用户主动回退时重置历史最大步数，否则重选相同支付方式后
    // naturalStep 恢复到原值时不会触发自动前进（因为不大于历史最大值）。
    maxNaturalStepRef.current = step
    setActiveStep(step)
  }

  const handleConfirmInvoice = () => {
    setInvoiceConfirmed(true)
  }

  // Step index aliases
  const sendStep = isSingleMethod ? 2 : 3
  const completedStep = isSingleMethod ? 3 : 4

  return (
    <div className="min-h-svh bg-background">
      <div className="flex flex-col min-h-svh">
        {/* Fixed top: summary + step indicator */}
        <div className="sticky top-0 z-20 bg-background/95 backdrop-blur border-b">
          <SummaryBar invoice={invoice} isDark={isDark} toggleTheme={toggleTheme} />
          <StepIndicator
            activeStep={activeStep}
            naturalStep={naturalStep}
            onStepClick={handleStepClick}
            stepCount={stepCount}
          />
        </div>

        {/* Scrollable content */}
        <div className="flex-1 overflow-y-auto pb-16">
          <div className="max-w-lg mx-auto px-4 pt-5">

            {activeStep === 1 && (
              <StepInvoice
                invoice={invoice}
                onConfirm={handleConfirmInvoice}
                isExpired={isExpired}
                isSingleMethod={isSingleMethod}
              />
            )}

            {!isSingleMethod && activeStep === 2 && (
              <div className="animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
                <PaymentMethodSelector
                  availableMethods={availableMethods}
                  selectedCrypto={selectedCrypto}
                  selectedChain={selectedChain}
                  onCryptoChange={handleCryptoChange}
                  onChainChange={handleChainChange}
                  isSelecting={isSelecting}
                  isEditing={isEditing}
                  error={paymentError}
                  onCancelEdit={cancelEdit}
                />
              </div>
            )}

            {activeStep === sendStep && (
              <div className="space-y-4 animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
                {isSelecting ? (
                  <div className="flex flex-col items-center gap-4 py-16">
                    <Loader2 className="size-10 animate-spin text-muted-foreground" />
                    <p className="text-sm text-muted-foreground">{t("payment.gettingPaymentInfo")}</p>
                  </div>
                ) : (
                  <>
                    <PaymentAddress
                      invoice={invoice}
                      onReset={isWaiting && !hasPayment && !isSingleMethod ? () => {
                        resetSelection()
                        maxNaturalStepRef.current = 2
                        setActiveStep(2)
                      } : null}
                    />
                    {isWaiting && hasPaymentMethod && !hasPayment && !isEditing && !isExpired && (
                      <WaitingPayment invoice={invoice} onExpired={refetch} />
                    )}
                  </>
                )}
              </div>
            )}

            {activeStep === completedStep && (
              <StepCompleted invoice={invoice} />
            )}

          </div>
        </div>

        {/* Footer */}
        <div className="border-t py-3 px-4">
          <div className="max-w-lg mx-auto flex items-center justify-center gap-2 text-xs text-muted-foreground">
            <span>Powered by</span>
            <a href="https://xca.sh" className="font-semibold text-foreground hover:underline">
              Xcash
            </a>
            <span>•</span>
            <span>Secure Crypto Payments</span>
          </div>
        </div>
      </div>
    </div>
  )
}

export default PaymentStepper
