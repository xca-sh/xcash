// src/components/PaymentStepper.jsx
import { useState, useEffect, useMemo, useRef } from "react"
import { AlertCircle, Loader2 } from "lucide-react"
import SummaryBar from "@/components/SummaryBar"
import StepIndicator from "@/components/StepIndicator"
import StepCompleted from "@/components/StepCompleted"
import PaymentMethodSelector from "@/components/PaymentMethodSelector"
import PaymentAddress from "@/components/PaymentAddress"
import WaitingPayment from "@/components/WaitingPayment"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useI18n } from "@/hooks/useI18n"
import { isPaymentConfirming } from "@/lib/invoiceStatus"

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
  const isConfirming = isPaymentConfirming(invoice)
  const isWaiting = invoice.status === "waiting"
  const isExpired = invoice.status === "expired"
  const availableMethods = invoice.methods ?? {}

  // Detect single-method: 1 token with 1 chain → skip selection step, show 3-step flow
  const methodTokens = Object.keys(availableMethods)
  const isSingleMethod = methodTokens.length === 1 && availableMethods[methodTokens[0]]?.length === 1
  const singleToken = isSingleMethod ? methodTokens[0] : null
  const singleChain = isSingleMethod ? availableMethods[methodTokens[0]][0] : null
  const stepCount = isSingleMethod ? 2 : 3

  // Auto-select token when only one payable method exists.
  useEffect(() => {
    if (isSingleMethod && isWaiting && !hasPaymentMethod && !selectedCrypto && !isSelecting) {
      handleCryptoChange(singleToken)
    }
  }, [isSingleMethod, isWaiting, hasPaymentMethod, selectedCrypto, isSelecting, singleToken, handleCryptoChange])

  // Auto-select chain once token is set; usePaymentMethod then submits select-method.
  useEffect(() => {
    if (isSingleMethod && isWaiting && !hasPaymentMethod && selectedCrypto && !selectedChain && !isSelecting) {
      handleChainChange(singleChain)
    }
  }, [isSingleMethod, isWaiting, hasPaymentMethod, selectedCrypto, selectedChain, isSelecting, singleChain, handleChainChange])

  const naturalStep = useMemo(() => {
    if (isExpired) return 1
    if (isSingleMethod) {
      if (isCompleted) return 2
      return 1
    }
    if (isCompleted) return 3
    if (isConfirming || (hasPaymentMethod && !isEditing)) return 2
    return 1
  }, [isCompleted, isConfirming, hasPaymentMethod, isEditing, isExpired, isSingleMethod])

  // 有链上 payment 或已分配当前支付指引时直接跳到当前 naturalStep。
  const initialStep = hasPayment || hasPaymentMethod ? naturalStep : 1
  const [activeStep, setActiveStep] = useState(initialStep)
  const maxNaturalStepRef = useRef(initialStep)

  // Auto-advance only when server state moves forward.
  useEffect(() => {
    if (naturalStep > maxNaturalStepRef.current) {
      maxNaturalStepRef.current = naturalStep
      setActiveStep(naturalStep)
    } else {
      maxNaturalStepRef.current = Math.max(maxNaturalStepRef.current, naturalStep)
    }
  }, [naturalStep])

  const handleStepClick = (step) => {
    if (isCompleted) return
    if (step >= naturalStep) return
    if (!isSingleMethod && step === 1 && naturalStep >= 2) {
      resetSelection()
    }
    // 用户主动回退时重置历史最大步数，否则重选相同支付方式后
    // naturalStep 恢复到原值时不会触发自动前进（因为不大于历史最大值）。
    maxNaturalStepRef.current = step
    setActiveStep(step)
  }

  // Step index aliases
  const methodStep = 1
  const sendStep = isSingleMethod ? 1 : 2
  const completedStep = isSingleMethod ? 2 : 3

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
            lockBack={isCompleted}
          />
        </div>

        {/* Scrollable content */}
        <div className="flex-1 overflow-y-auto pb-16">
          <div className="max-w-lg mx-auto px-4 pt-5">

            {isExpired && (
              <div className="space-y-3 animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
                <Alert variant="destructive">
                  <AlertCircle />
                  <AlertTitle>{t("expired.orderExpired")}</AlertTitle>
                  <AlertDescription>{t("expired.contactMerchant")}</AlertDescription>
                </Alert>
                <Button
                  onClick={() => window.location.reload()}
                  variant="outline"
                  className="w-full"
                >
                  {t("expired.refreshPage")}
                </Button>
              </div>
            )}

            {!isExpired && !isSingleMethod && activeStep === methodStep && (
              <div className="animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
                <PaymentMethodSelector
                  invoice={invoice}
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

            {!isExpired && activeStep === sendStep && (
              <div className="space-y-4 animate-in fade-in-0 slide-in-from-bottom-2 duration-300">
                {paymentError && !hasPaymentMethod ? (
                  <Alert variant="destructive">
                    <AlertCircle />
                    <AlertTitle>{t("common.error")}</AlertTitle>
                    <AlertDescription>{paymentError}</AlertDescription>
                  </Alert>
                ) : isSelecting || (isSingleMethod && isWaiting && !hasPaymentMethod) ? (
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
                        maxNaturalStepRef.current = methodStep
                        setActiveStep(methodStep)
                      } : null}
                    />
                    {isWaiting && hasPaymentMethod && !hasPayment && !isEditing && !isExpired && (
                      <WaitingPayment invoice={invoice} onExpired={refetch} />
                    )}
                  </>
                )}
              </div>
            )}

            {!isExpired && activeStep === completedStep && (
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
