/** @type {import('stylelint').Config} */
export default {
  extends: ['stylelint-config-standard-vue'],
  ignoreFiles: ['.nuxt/**', '.output/**', 'node_modules/**'],
  rules: {
    // doc/04 §1.4：container_页面_功能_位置_别名，四段小写、下划线连接、段内连写
    'selector-class-pattern': [
      '^container_(dash|task|workflow|session|user|login)_[a-z]+_[a-z]+_[a-z]+$',
      {
        resolveNestedSelectors: true,
        message: (selector) =>
          `类名 "${selector}" 不符合命名规则 ^container_(dash|task|workflow|session|user|login)_[a-z]+_[a-z]+_[a-z]+$（doc/04 §1）`,
      },
    ],
  },
}
