// API 基础地址：根据当前页面地址动态拼接
const API_BASE_URL = `${window.location.origin}/v1`

const handleResponse = async (response, defaultMessage) => {
  if (response.ok) {
    return response.json()
  }

  let message = `${defaultMessage}: HTTP ${response.status}`
  try {
    const data = await response.json()
    if (data?.message) {
      message = data.message
    }
  } catch {
    // ignore json parse error, fall back to default message
  }

  throw new Error(message)
}

// 获取账单详情
export const getInvoice = async (sysNo) => {
  try {
    const response = await fetch(`${API_BASE_URL}/invoice/${sysNo}`, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
    })
    return await handleResponse(response, '获取账单信息失败')
  } catch (error) {
    console.error('获取账单详情失败:', error)
    throw error
  }
}

// 设置支付方式
export const selectPayMethod = async (sysNo, crypto, chain) => {
  try {
    const response = await fetch(`${API_BASE_URL}/invoice/${sysNo}/select-method`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
      },
      body: JSON.stringify({
        crypto,
        chain,
      }),
    })
    return await handleResponse(response, '设置支付方式失败')
  } catch (error) {
    console.error('选择加密货币和公链失败:', error)
    throw error
  }
}

// 获取 URL 参数
export const getUrlParam = (name) => {
  if (typeof window === 'undefined') return null

  const urlParams = new URLSearchParams(window.location.search)
  const queryValue = urlParams.get(name)
  if (queryValue) {
    return queryValue
  }

  if (name === 'sys_no') {
    // 使用 VITE_PAY_MOUNT 作为页面挂载路径，与资源 base URL（/static/pay/）解耦
    const mount = (import.meta?.env?.VITE_PAY_MOUNT ?? '/pay').replace(/^\/|\/$/g, '')
    const mountSegments = mount ? mount.split('/').filter(Boolean) : []

    const pathSegments = window.location.pathname
      .split('/')
      .filter(Boolean)
      .slice(mountSegments.length)

    if (pathSegments.length > 0) {
      return decodeURIComponent(pathSegments[pathSegments.length - 1])
    }

    const hash = window.location.hash.replace(/^#\/?/, '')
    if (hash) {
      const hashSegments = hash.split('/').filter(Boolean)
      if (hashSegments.length > 0) {
        return decodeURIComponent(hashSegments[hashSegments.length - 1])
      }
    }
  }

  return null
}
