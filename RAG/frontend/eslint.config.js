import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import prettier from 'eslint-config-prettier'

export default tseslint.config(
  { ignores: ['dist', 'node_modules', '*.tsbuildinfo'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      ...reactRefresh.configs.vite.rules,
      // 本项目组件（Chat.tsx / MessageList.tsx 等）有具名导出 + 默认导出混用，
      // 该规则默认会全部报错，关掉（保持现状）
      'react-refresh/only-export-components': 'off',
      // 项目当前写法是"useEffect 里初始化数据"（loadUsers() 等常见数据加载模式，
      // React 18.3 严格模式未开启），该规则（React 19 最佳实践）要求全面重构 effect
      // 结构，37 处逐个重写风险 > 收益；关掉保持现状，仅保留其余 react-hooks 规则
      'react-hooks/set-state-in-effect': 'off',
    },
  },
  prettier,
)
