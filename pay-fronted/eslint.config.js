import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{js,jsx}'],
    extends: [
      js.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
      parserOptions: {
        ecmaVersion: 'latest',
        ecmaFeatures: { jsx: true },
        sourceType: 'module',
      },
    },
    rules: {
      'no-unused-vars': ['error', { varsIgnorePattern: '^[A-Z_]' }],
      // react-hooks v7 新增的 React Compiler 级规则。本项目多处 hook（数据拉取、
      // i18n 订阅、倒计时）刻意用 effect 内 setState 与外部系统同步，属于该规则
      // 自身说明认可的合法用法，强行改写会牵动支付状态逻辑，故关闭此条。
      'react-hooks/set-state-in-effect': 'off',
    },
  },
  {
    // shadcn/ui 原语为官方原样源码：button/badge 等会同时具名导出 *Variants，
    // 触发 react-refresh 的「单文件只导出组件」规则。这是 shadcn 官方推荐的豁免范围，
    // 仅对 ui 目录关闭该规则，不影响业务组件。
    files: ['src/components/ui/**/*.{js,jsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
])
