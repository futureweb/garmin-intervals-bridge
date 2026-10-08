from pathlib import Path

from garmin_intervals_bridge.garmin import GarminSource


def test_client_logs_in_lazily_and_only_once(tmp_path, monkeypatch):
    src = GarminSource(Path(tmp_path), 0.25)
    calls = []

    def fake_login(interactive=True):
        calls.append(interactive)
        src.client = object()

    monkeypatch.setattr(src, "login", fake_login)
    assert calls == []                       # constructing costs nothing
    src._client()
    src._client()
    assert calls == [False]                  # one non-interactive login on first use
