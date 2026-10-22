"""kllm: 把 Kaggle Benchmarks 的 AI 额度包装成本地 OpenAI 兼容模型后端。"""

from ..common.config import AVAILABLE_MODELS, DEFAULT_MODEL, Settings, load_settings
from ..common.errors import CredentialError
from .credentials import ProxyCredentials, TokenManager

__all__ = [
    "AVAILABLE_MODELS",
    "DEFAULT_MODEL",
    "Settings",
    "load_settings",
    "CredentialError",
    "ProxyCredentials",
    "TokenManager",
]
