import json
from types import SimpleNamespace

import pytest
import requests

from garmin_intervals_bridge import config
from garmin_intervals_bridge.notify import addresses, events, new_activities, notify


class Tool:
    def __init__(self, status=202, fail=None):
        self.posts, self.status, self.fail = [], status, fail

    def post(self, url, json, timeout):
        if self.fail:
            raise self.fail
        self.posts.append((url, json))
        response = requests.Response()
        response.status_code, response.url = self.status, url
        return response


def test_counts_what_is_new_in_watch_upload_and_sync_records():
    assert new_activities({"apply": True, "polled": 3, "new": 2, "enriched": 1}) == 2
    assert new_activities({"apply": True, "uploaded": 1, "would_upload": 0}) == 1
    assert new_activities({"apply": True, "activities": {"seen": 9, "enriched": 3}, "wellness": {"writes": 5}}) == 3
    assert new_activities({"apply": False, "polled": 3, "new": 0, "planned": 2}) == 0
    assert new_activities({"skipped": "quiet hours"}) == 0


def test_events_for_activities_and_the_wellness_days_written():
    record = {"new": 1, "wellness": {"writes": 2, "written_days": ["2026-11-01", "2026-10-31", "2026-11-01"]}}
    assert events(record) == [{"event": "activities", "new": 1},
                              {"event": "wellness", "days": ["2026-10-31", "2026-11-01"]}]
    assert events({"activities": {"enriched": 0}, "wellness": {"writes": 0, "days_with_changes": 3}}) == []
    assert events({"new": 0, "wellness": {"intervals_checks": 1}}) == []


def test_notify_posts_each_event_to_each_address_and_never_raises():
    settings = SimpleNamespace(notify_url="http://127.0.0.1:8766/sync, http://agent.local/hook")
    tool = Tool()
    record = {"new": 2, "wellness": {"writes": 1, "written_days": ["2026-11-01"]}}
    assert notify(settings, record, session=tool) == 4
    assert [(u, b["event"]) for u, b in tool.posts] == [
        ("http://127.0.0.1:8766/sync", "activities"), ("http://agent.local/hook", "activities"),
        ("http://127.0.0.1:8766/sync", "wellness"), ("http://agent.local/hook", "wellness")]
    body = tool.posts[0][1]
    assert body["source"] == "garmin-intervals-bridge" and body["new"] == 2
    assert set(json.loads(json.dumps(tool.posts[2][1]))) == {"source", "version", "event", "days"}  # no values
    assert notify(settings, {"new": 0}, session=tool) == 0 and len(tool.posts) == 4
    assert notify(SimpleNamespace(notify_url=None), {"new": 2}, session=tool) == 0
    assert notify(settings, {"new": 1}, session=Tool(fail=requests.ConnectionError("down"))) == 0
    assert notify(settings, {"new": 1}, session=Tool(status=500)) == 0


def test_notify_url_from_env_and_home_assistant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setenv("BRIDGE_NOTIFY_URL", "http://summits:8766/sync,https://agent.example/hook")
    loaded = config.Settings.from_env().notify_url
    assert addresses(loaded) == ["http://summits:8766/sync", "https://agent.example/hook"]
    monkeypatch.setenv("BRIDGE_NOTIFY_URL", "http://summits:8766/sync, agent:9000")
    with pytest.raises(ValueError, match="BRIDGE_NOTIFY_URL must be web addresses"):
        config.Settings.from_env()
    assert config.OPTIONS["notify_url"] == "BRIDGE_NOTIFY_URL"
