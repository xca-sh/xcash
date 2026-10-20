import { useState, useEffect } from "react"
import en from "@/locales/en.json"
import zh from "@/locales/zh.json"
import ru from "@/locales/ru.json"

const translations = {
  en,
  zh,
  ru,
}

const SUPPORTED = ["zh", "en", "ru"]
const STORAGE_KEY = "xcash-locale"
const CHANGE_EVENT = "xcash-locale-change"

// 语言下拉框的展示顺序即标准支持语言顺序：中文、英语、俄语。
export const SUPPORTED_LOCALES = [
  { code: "zh", label: "中文", flag: "CN" },
  { code: "en", label: "English", flag: "GB" },
  { code: "ru", label: "Русский", flag: "RU" },
]

// 按浏览器语言偏好列表依次匹配，取第一个标准支持的语言（如 "uk, ru" 取 ru）；都不支持时回退英文。
function detectBrowserLocale() {
  if (typeof navigator === "undefined") return "en"
  const preferred = navigator.languages?.length ? navigator.languages : [navigator.language]
  for (const lang of preferred) {
    const code = lang?.split("-")[0].toLowerCase()
    if (SUPPORTED.includes(code)) return code
  }
  return "en"
}

function readStoredLocale() {
  try {
    const stored = window.localStorage?.getItem(STORAGE_KEY)
    return SUPPORTED.includes(stored) ? stored : null
  } catch {
    // localStorage 不可用（隐私模式等），视为未手动选择
    return null
  }
}

// 手动选择过的优先；否则跟随浏览器语言。
function detectInitialLocale() {
  if (typeof window === "undefined") return "en"
  return readStoredLocale() || detectBrowserLocale()
}

// 全局单一来源：避免多处 useI18n 各自持有独立 state，切换不同步。
let currentLocale = detectInitialLocale()
const listeners = new Set()

function applyLocale(next) {
  if (next === currentLocale) return
  currentLocale = next
  listeners.forEach((fn) => fn(next))
  window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: next }))
}

// 用户在下拉框里手动选择：记住选择，此后不再跟随浏览器语言。
function setGlobalLocale(next) {
  if (!SUPPORTED.includes(next)) return
  try {
    window.localStorage?.setItem(STORAGE_KEY, next)
  } catch {
    // 写入失败不影响内存中的切换
  }
  applyLocale(next)
}

function lookup(table, keys) {
  let translation = table
  for (const k of keys) {
    translation = translation?.[k]
    if (!translation) break
  }
  return translation
}

// 当前语言缺该 key 时回退英文，英文也没有则原样返回 key。
function translateIn(locale, key, params = {}) {
  const keys = key.split(".")
  const translation =
    lookup(translations[locale] || translations.en, keys) || lookup(translations.en, keys)

  if (!translation) {
    return key
  }

  if (typeof translation === "string" && Object.keys(params).length > 0) {
    return translation.replace(/\{\{(\w+)\}\}/g, (match, param) => {
      return params[param] !== undefined ? params[param] : match
    })
  }

  return translation
}

/** 供非组件代码（如数据加载 hook 的错误文案）按当前全局语言取翻译。 */
export function translate(key, params = {}) {
  return translateIn(currentLocale, key, params)
}

export function useI18n() {
  const [locale, setLocaleState] = useState(currentLocale)

  useEffect(() => {
    const handler = (next) => setLocaleState(next)
    listeners.add(handler)
    // locale 在挂载到订阅之间可能已被改动，做一次对齐
    if (locale !== currentLocale) setLocaleState(currentLocale)

    const onBrowserChange = () => {
      // 仅当用户没手动选过语言时，才跟随浏览器语言变化；跟随不写入存储，避免被当成手动选择
      if (readStoredLocale()) return
      applyLocale(detectBrowserLocale())
    }
    window.addEventListener("languagechange", onBrowserChange)
    return () => {
      listeners.delete(handler)
      window.removeEventListener("languagechange", onBrowserChange)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const t = (key, params = {}) => translateIn(locale, key, params)

  return {
    locale,
    setLocale: setGlobalLocale,
    t,
  }
}
