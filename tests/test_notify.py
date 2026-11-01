import json
from types import SimpleNamespace

import pytest
import requests

from garmin_intervals_bridge import config
from garmin_intervals_bridge.notify import new_activities, notify


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


def test_notify_posts_a_count_and_never_raises():
    settings = SimpleNamespace(notify_url="http://127.0.0.1:8766/sync")
    tool = Tool()
    assert notify(settings, {"new": 2}, session=tool)
    url, body = tool.posts[0]
    assert url == "http://127.0.0.1:8766/sync" and body["new"] == 2 and body["source"] == "garmin-intervals-bridge"
    assert json.dumps(body).count(":") == 3                    # source, version, new – nothing else
    assert not notify(settings, {"new": 0}, session=tool) and len(tool.posts) == 1
    assert not notify(SimpleNamespace(notify_url=None), {"new": 2}, session=tool) and len(tool.posts) == 1
    assert not notify(settings, {"new": 1}, session=Tool(fail=requests.ConnectionError("down")))
    assert not notify(settings, {"new": 1}, session=Tool(status=500))


def test_notify_url_from_env_and_home_assistant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setenv("BRIDGE_NOTIFY_URL", "http://summits:8766/sync")
    assert config.Settings.from_env().notify_url == "http://summits:8766/sync"
    monkeypatch.setenv("BRIDGE_NOTIFY_URL", "summits:8766/sync")
    with pytest.raises(ValueError, match="BRIDGE_NOTIFY_URL must be a web address"):
        config.Settings.from_env()
    assert config.OPTIONS["notify_url"] == "BRIDGE_NOTIFY_URL"
