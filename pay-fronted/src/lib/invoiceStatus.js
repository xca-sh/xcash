export function getPaymentStatus(invoice) {
  return invoice?.payment?.status ?? null
}

export function getConfirmationProgress(invoice) {
  return invoice?.payment?.confirm_progress || {}
}

export function isPaymentConfirming(invoice) {
  return invoice?.status !== "completed" && getPaymentStatus(invoice) === "confirming"
}

export function getInvoiceDisplayStatus(invoice) {
  const status = invoice?.status
  if (status === "completed" || status === "expired") {
    return status
  }

  const progress = getConfirmationProgress(invoice).progress ?? 0
  if (isPaymentConfirming(invoice)) {
    return progress >= 100 ? "finalizing" : "confirming"
  }

  return status || "unknown"
}
