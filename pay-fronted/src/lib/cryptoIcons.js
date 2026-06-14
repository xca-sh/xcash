/**
 * 加密货币和区块链图标工具
 * 主要使用 cryptocurrency-icons CDN，Cryptofonts 作为备用
 */

const CDN_BASE = 'https://cdn.jsdelivr.net/npm/cryptocurrency-icons@0.18.1/svg/color'

/**
 * 特殊处理的图标映射（用于不在主 CDN 中的新币种）
 * 这些图标会使用官方 GitHub 源或 Cryptofonts 备用库
 */
const SPECIAL_ICONS = {
  'base': 'https://icons.llamao.fi/icons/chains/rsz_base.jpg',
  'zksync': "https://icons.llamao.fi/icons/chains/rsz_zksync%20era.jpg",
}

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
 * 区块链网络到图标的映射
 * 格式：链名-网络性质（如 ethereum-mainnet, ethereum-sepolia）
 */
const CHAIN_ICON_MAP = {
  // Ethereum
  'ethereum-mainnet': 'eth',
  'ethereum-sepolia': 'eth',
  'ethereum-goerli': 'eth',
  'ethereum-holesky': 'eth',

  // BNB Chain (BSC)
  'bsc-mainnet': 'bnb',
  'bsc-testnet': 'bnb',
  'bnbchain-mainnet': 'bnb',
  'bnbchain-testnet': 'bnb',

  // Polygon
  'polygon-mainnet': 'matic',
  'polygon-amoy': 'matic',
  'polygon-mumbai': 'matic',

  // Arbitrum
  'arbitrum-mainnet': 'arb',
  'arbitrum-sepolia': 'arb',
  'arbitrum-goerli': 'arb',

  // Optimism
  'optimism-mainnet': 'op',
  'optimism-sepolia': 'op',
  'optimism-goerli': 'op',

  // Avalanche
  'avalanche-mainnet': 'avax',
  'avalanche-fuji': 'avax',

  // Solana
  'solana-mainnet': 'sol',
  'solana-devnet': 'sol',
  'solana-testnet': 'sol',

  // Tron
  'tron-mainnet': 'trx',
  'tron-shasta': 'trx',
  'tron-nile': 'trx',

  // Base
  'base-mainnet': 'base',
  'base-sepolia': 'base',

  // zkSync
  'zksync-mainnet': 'zksync',
  'zksync-sepolia': 'zksync',
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
 * @param {string} chain - 链名称（如 ethereum, bsc）
 * @returns {string|null} - 图标 URL 或 null
 */
export function getChainIconUrl(chain) {
  if (!chain) return null

  const normalizedChain = chain.toLowerCase()
  const iconName = CHAIN_ICON_MAP[normalizedChain]

  if (!iconName) return null

  // 检查是否有特殊图标映射
  if (SPECIAL_ICONS[iconName]) {
    return SPECIAL_ICONS[iconName]
  }

  return `${CDN_BASE}/${iconName}.svg`
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
 * 获取链显示名称
 * @param {string} chain - 链名称（格式：链名-网络性质）
 * @returns {string}
 */
export function getChainDisplayName(chain) {
  if (!chain) return ''

  const nameMap = {
    // Ethereum
    'ethereum-mainnet': 'Ethereum',
    'ethereum-sepolia': 'Ethereum Sepolia',
    'ethereum-goerli': 'Ethereum Goerli',
    'ethereum-holesky': 'Ethereum Holesky',

    // BNB Chain
    'bsc-mainnet': 'BNB Chain',
    'bsc-testnet': 'BNB Chain Testnet',
    'bnbchain-mainnet': 'BNB Chain',
    'bnbchain-testnet': 'BNB Chain Testnet',

    // Polygon
    'polygon-mainnet': 'Polygon',
    'polygon-amoy': 'Polygon Amoy',
    'polygon-mumbai': 'Polygon Mumbai',

    // Arbitrum
    'arbitrum-mainnet': 'Arbitrum',
    'arbitrum-sepolia': 'Arbitrum Sepolia',
    'arbitrum-goerli': 'Arbitrum Goerli',

    // Optimism
    'optimism-mainnet': 'Optimism',
    'optimism-sepolia': 'Optimism Sepolia',
    'optimism-goerli': 'Optimism Goerli',

    // Avalanche
    'avalanche-mainnet': 'Avalanche',
    'avalanche-fuji': 'Avalanche Fuji',

    // Solana
    'solana-mainnet': 'Solana',
    'solana-devnet': 'Solana Devnet',
    'solana-testnet': 'Solana Testnet',

    // Tron
    'tron-mainnet': 'Tron',
    'tron-shasta': 'Tron Shasta',
    'tron-nile': 'Tron Nile',

    // Base
    'base-mainnet': 'Base',
    'base-sepolia': 'Base Sepolia',

    // zkSync
    'zksync-mainnet': 'zkSync Era',
    'zksync-sepolia': 'zkSync Era Sepolia',
  }

  return nameMap[chain.toLowerCase()] || chain
    .split('-')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

/**
 * 检查是否为测试网络
 * @param {string} chain - 链名称（格式：链名-网络性质）
 * @returns {boolean}
 */
export function isTestnet(chain) {
  if (!chain) return false
  const lower = chain.toLowerCase()

  // mainnet 为主网
  if (lower.endsWith('-mainnet')) return false

  // 其他都视为测试网（testnet, sepolia, goerli, devnet, shasta, nile, amoy, mumbai, fuji, holesky 等）
  return lower.includes('testnet') ||
         lower.includes('sepolia') ||
         lower.includes('goerli') ||
         lower.includes('holesky') ||
         lower.includes('mumbai') ||
         lower.includes('amoy') ||
         lower.includes('fuji') ||
         lower.includes('devnet') ||
         lower.includes('shasta') ||
         lower.includes('nile')
}
