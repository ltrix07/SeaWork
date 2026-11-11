from pathlib import Path

import pytest

from seawork.config import Settings


def test_settings_ignore_the_developer_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("SEAWORK_LLM_API_KEY=live\nSEAWORK_TELEGRAM_BOT_TOKEN=live\n")
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.llm_api_key is None
    assert settings.telegram_bot_token is None


def test_settings_still_read_what_a_test_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEAWORK_DIGEST_HOUR_UTC", "5")

    assert Settings().digest_hour_utc == 5
