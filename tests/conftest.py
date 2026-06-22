import pytest


@pytest.fixture(autouse=True)
def isolate_away_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_AWAY_HOME", str(tmp_path / "away-home"))
