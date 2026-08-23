"""colab_core 的异常类型。

`TaskError` 对应旧 run.py 中 `sys.exit("中文提示")` 的用户级错误：
CLI 薄壳捕获后以相同文案退出（exit 1）；意外异常仍是 RuntimeError 等，
保持原有 traceback 行为。
"""


class TaskError(RuntimeError):
    """用户可读的 task 失败（参数、输入缺失、会话忙、子命令失败等）。"""
