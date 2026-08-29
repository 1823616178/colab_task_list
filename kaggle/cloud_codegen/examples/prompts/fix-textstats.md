# 目标

修复 `textstats` 包中的实现，让仓库的全部测试通过。

# 约束

- 只修改实现，不修改 `tests/` 下的文件和 `pytest.ini`。
- 保持公开函数签名不变。
- 保留类型注解和 docstring。
- 不引入新的第三方依赖。

# 验收

运行 `python -m pytest -q`，退出码必须为 0。
