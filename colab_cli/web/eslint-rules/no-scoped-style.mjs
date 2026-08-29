// 自定义 ESLint 规则：禁止 <style scoped>（doc/04 §1.3）
// 理由：container_页面_功能_位置_别名 命名规则已保证全局唯一，
// scoped 会额外注入 data-v-* 属性选择器，两套定位机制互相打架。
//
// JSDoc 类型标注是必需的：没有它 `meta.type` 会被推断成宽泛的 string，
// `nuxi typecheck` 在 eslint.config.mjs 处报 TS2345（不满足 Plugin.rules）。
/** @type {import('eslint').Rule.RuleModule} */
export default {
  meta: {
    type: 'problem',
    docs: { description: '禁用 <style scoped>，样式作用域由 CSS 命名规则保证' },
    schema: [],
    messages: {
      scoped:
        '禁止使用 <style scoped>：CSS 命名规则（container_页面_功能_位置_别名）已保证全局唯一，见 doc/04 §1.3。',
    },
  },
  create(context) {
    const services = context.sourceCode?.parserServices ?? context.parserServices
    const fragment = services?.getDocumentFragment?.()
    if (!fragment) return {}
    const scopedTags = []
    const visit = (node) => {
      if (node?.type === 'VElement' && node.name === 'style') {
        const hasScoped = node.startTag.attributes.some(
          (attr) => attr.type === 'VAttribute' && !attr.directive && attr.key.name === 'scoped',
        )
        if (hasScoped) scopedTags.push(node.startTag)
      }
      for (const child of node?.children ?? []) visit(child)
    }
    visit(fragment)
    return {
      Program() {
        for (const tag of scopedTags) context.report({ node: tag, messageId: 'scoped' })
      },
    }
  },
}
