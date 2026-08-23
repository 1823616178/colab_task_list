// @ts-check
import { createConfigForNuxt } from '@nuxt/eslint-config/flat'
import noScopedStyle from './eslint-rules/no-scoped-style.mjs'

export default createConfigForNuxt()
  .append({
    ignores: ['.nuxt/**', '.output/**', 'node_modules/**', 'types/events.d.ts'],
  })
  .append({
    plugins: { colab: { rules: { 'no-scoped-style': noScopedStyle } } },
    rules: {
      // doc/04 §1.3：禁用 <style scoped>，作用域由 container_* 命名规则保证
      'colab/no-scoped-style': 'error',
    },
  })
