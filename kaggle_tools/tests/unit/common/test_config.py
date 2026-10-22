"""集中配置的读取、.env 合并与校验。"""

from pathlib import Path

import pytest

from kaggle_tools.common.config import DEFAULT_MODEL, Settings, load_settings, parse_env_file
from kaggle_tools.common.errors import ConfigError


def test_defaults_without_any_environment(tmp_path):
    settings = load_settings(environ={}, env_file=tmp_path / "missing.env")
    assert settings.port == 8799
    assert settings.default_model == DEFAULT_MODEL
    assert settings.max_retries == 3
    assert settings.local_key is None


def test_environment_values_are_parsed(tmp_path):
    settings = load_settings(
        environ={"KLLM_PORT": "9000", "KLLM_TIMEOUT": "12.5", "KLLM_LOCAL_KEY": "k",
                 "KLLM_LOG_LEVEL": "debug", "KCLOUD_RUNS_DIR": str(tmp_path / "r")},
        env_file=tmp_path / "missing.env",
    )
    assert settings.port == 9000
    assert settings.request_timeout_seconds == 12.5
    assert settings.local_key == "k"
    assert settings.log_level == "DEBUG"
    assert settings.runs_dir == tmp_path / "r"


def test_env_file_is_merged_and_real_environment_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nexport KLLM_PORT=9100\nKLLM_LOCAL_KEY='from-file'\n\nKLLM_MAX_RETRIES=5\n",
        encoding="utf-8",
    )
    settings = load_settings(environ={"KLLM_MAX_RETRIES": "1"}, env_file=env_file)
    assert settings.port == 9100
    assert settings.local_key == "from-file"
    assert settings.max_retries == 1


@pytest.mark.parametrize(
    "name, value",
    [("KLLM_PORT", "abc"), ("KLLM_PORT", "70000"), ("KLLM_TIMEOUT", "0"),
     ("KLLM_MAX_RETRIES", "-1"), ("KLLM_LOG_LEVEL", "LOUD"), ("KAGGLE_CLI_TIMEOUT", "x")],
)
def test_invalid_values_name_the_variable(tmp_path, name, value):
    with pytest.raises(ConfigError, match=name):
        load_settings(environ={name: value}, env_file=tmp_path / "missing.env")


def test_malformed_env_file_line_is_reported(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("NOT_A_PAIR\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=":1"):
        parse_env_file(env_file)


def test_legacy_positional_settings_still_work(tmp_path):
    settings = Settings("127.0.0.1", 8799, None, Path(tmp_path), 60.0)
    assert settings.credentials_path == tmp_path / "credentials.json"
    assert settings.usage_path == tmp_path / "usage.jsonl"
