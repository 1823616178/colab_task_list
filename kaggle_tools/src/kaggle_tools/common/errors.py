"""统一异常层级。消息面向使用者, CLI 捕获后直接打印, 不打调用栈。"""


class KaggleToolsError(Exception):
    """所有可预期错误的基类。"""


class ConfigError(KaggleToolsError, ValueError):
    """配置值缺失或非法。"""


class CredentialError(KaggleToolsError, RuntimeError):
    """模型代理凭据获取失败。"""


class KaggleCliError(KaggleToolsError, RuntimeError):
    """kaggle 命令执行失败或超时。"""


class KaggleCliNotFoundError(KaggleCliError):
    """找不到 kaggle 可执行文件。"""
