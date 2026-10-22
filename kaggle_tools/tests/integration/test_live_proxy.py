"""真实调用 Kaggle 模型代理(消耗少量额度)。

运行: KAGGLE_TOOLS_LIVE=1 pytest -m live
"""

import pytest

from kaggle_tools.common.config import load_settings
from kaggle_tools.kllm.cli import chat_once
from kaggle_tools.kllm.credentials import TokenManager

pytestmark = pytest.mark.live


def test_credentials_can_be_fetched():
    credentials = TokenManager(load_settings()).get()
    assert credentials.base_url.endswith("/openapi")
    assert not credentials.expired(skew_seconds=0)


def test_default_model_answers():
    settings = load_settings()
    credentials = TokenManager(settings).get()
    response = chat_once(credentials, settings.default_model, "Reply with exactly: OK", settings)
    assert response.status_code == 200, response.text[:500]
    assert response.json()["choices"][0]["message"]["content"]
