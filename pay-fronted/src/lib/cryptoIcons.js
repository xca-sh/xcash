/**
 * 加密货币和区块链图标工具
 * 代币图标用 cryptocurrency-icons CDN；链图标用 DefiLlama 链图标 CDN，
 * 与后端 chains/constants.py 的 CHAIN_SPECS.icon 保持同一来源、同一 slug。
 */

const CDN_BASE = 'https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color'

/**
 * 加密货币符号到图标文件名的映射
 */
const CRYPTO_ICON_MAP = {
  'ETH': 'eth',
  'USDT': 'usdt',
  'USDC': 'usdc',
  'BNB': 'bnb',
  'BUSD': 'busd',
  'DAI': 'dai',
  'MATIC': 'matic',
  'TRX': 'trx',
  'SOL': 'sol',
  'DOGE': 'doge',
  'LTC': 'ltc',
  'XRP': 'xrp',
  'ADA': 'ada',
  'DOT': 'dot',
  'AVAX': 'avax',
  'SHIB': 'shib',
  'WBTC': 'wbtc',
  'LINK': 'link',
  'UNI': 'uni',
  'ARB': 'arb',
  'OP': 'op',
}

/**
 * 链 code 到 DefiLlama 图标 slug 的映射。
 * 键为后端 Chain.code（裸链名，见 chains/constants.py 的 ChainCode），slug 与 code
 * 不机械对应（bsc→binance、arbitrum-one→arbitrum），故逐条显式指定，与后端 _icon() 一致。
 * 测试网/本地链复用其母网图标，便于支付页统一展示。
 */
const CHAIN_ICON_SLUG_MAP = {
  ethereum: 'ethereum',
  bsc: 'binance',
  polygon: 'polygon',
  'arbitrum-one': 'arbitrum',
  optimism: 'optimism',
  base: 'base',
  avalanche: 'avalanche',
  linea: 'linea',
  scroll: 'scroll',
  tron: 'tron',

  // 测试网 / 本地链：复用母网图标
  sepolia: 'ethereum',
  anvil: 'ethereum',
  nile: 'tron',
}

/**
 * 获取加密货币图标 URL
 * @param {string} crypto - 加密货币符号（如 ETH, USDT）
 * @returns {string|null} - 图标 URL 或 null
 */
export function getCryptoIconUrl(crypto) {
  if (!crypto) return null

  const normalizedCrypto = crypto.toUpperCase()
  const iconName = CRYPTO_ICON_MAP[normalizedCrypto] || crypto.toLowerCase()

  return `${CDN_BASE}/${iconName}.svg`
}

/**
 * 获取区块链图标 URL
 * @param {string} chain - 链 code（后端裸链名，如 ethereum、bsc、tron、base）
 * @returns {string|null} - 图标 URL 或 null
 */
export function getChainIconUrl(chain) {
  if (!chain) return null

  const slug = CHAIN_ICON_SLUG_MAP[chain.toLowerCase()]
  if (!slug) return null

  return `https://icons.llamao.fi/icons/chains/rsz_${slug}.jpg`
}

/**
 * 获取加密货币显示名称
 * @param {string} crypto - 加密货币符号
 * @returns {string}
 */
export function getCryptoDisplayName(crypto) {
  if (!crypto) return ''
  return crypto.toUpperCase()
}

/**
 * 链 code 到显示名的映射，与后端 ChainCode 标签保持一致。
 */
const CHAIN_NAME_MAP = {
  ethereum: 'Ethereum',
  bsc: 'BNB Chain',
  polygon: 'Polygon',
  'arbitrum-one': 'Arbitrum One',
  optimism: 'Optimism',
  base: 'Base',
  avalanche: 'Avalanche',
  linea: 'Linea',
  scroll: 'Scroll',
  tron: 'Tron',
  sepolia: 'Ethereum Sepolia',
  nile: 'Tron Nile',
  anvil: 'Anvil Local',
}

/**
 * 测试网 / 本地链的 code 集合，与后端 CHAIN_SPECS.is_testnet 一致。
 */
const TESTNET_CHAINS = new Set(['sepolia', 'nile', 'anvil'])

/**
 * 获取链显示名称
 * @param {string} chain - 链 code（后端裸链名）
 * @returns {string}
 */
export function getChainDisplayName(chain) {
  if (!chain) return ''

  const lower = chain.toLowerCase()
  return CHAIN_NAME_MAP[lower] || chain
    .split('-')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

/**
 * 检查是否为测试网络
 * @param {string} chain - 链 code（后端裸链名）
 * @returns {boolean}
 */
export function isTestnet(chain) {
  if (!chain) return false
  return TESTNET_CHAINS.has(chain.toLowerCase())
}
