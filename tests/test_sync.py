from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from garmin_intervals_bridge.fit import validate_fit
from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import activity_match, sync_activities, sync_wellness
from test_fit import minimal_fit
from test_mapping import sample


def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"),
                           activity_days=3, wellness_days=3, wellness_refresh_hours=8)


class GarminFake:
    def __init__(self):
        self.acts = [{"activityId": 42, "startTimeGMT": "2026-10-07 08:00:00",
                      "activityName": "Ride"}]
        self.download_count = 0

    def activities(self, start, end):
        return self.acts

    def original_fit(self, activity_id):
        self.download_count += 1
        return minimal_fit()

    def snapshot(self, day):
        return sample()


class IntervalsFake:
    def __init__(self, remote=None):
        self.remote = remote or []
        self.uploads = []
        self.wellness_writes = []
        self.provision_calls = []
        self.current_wellness = {}
        self.fail_upload = False

    def activities(self, start, end):
        return self.remote

    def nearby_activities(self, day):
        return self.remote

    def upload_fit(self, gid, path):
        if self.fail_upload:
            raise TimeoutError("Connection failed after server may have accepted upload")
        self.uploads.append((gid, path))
        return {"created": True, "items": [{"id": "i88"}]}

    def wellness(self, day):
        return self.current_wellness

    def write_wellness(self, day, patch):
        self.wellness_writes.append((day, patch))

    def provision_fields(self, apply, needed):
        self.provision_calls.append((apply, needed))
        return list(needed)


def test_match_by_external_id_or_start_and_fail_closed():
    a = {"activityId": 42, "startTimeGMT": "2026-10-07T08:00:00"}
    assert activity_match(a, [{"id": "i1", "external_id": "garmin:42"}])["id"] == "i1"
    assert activity_match(a, [{"id": "i2", "start_date": "2026-10-07T08:01:00Z"}])["id"] == "i2"
    assert activity_match(a, [{"id": "i3", "start_date": "2026-10-07T12:00:00Z"}]) is None
    with pytest.raises(ValueError):
        activity_match({"activityId": 42}, [])


def test_dry_run_archives_but_does_not_upload(tmp_path):
    st = Store(tmp_path)
    g, i = GarminFake(), IntervalsFake()
    result = sync_activities(settings(tmp_path), g, i, st, apply=False, allow_upload=False,
                             today=date(2026, 10, 8))
    assert result["downloaded"] == 1 and result["would_upload"] == 1
    assert i.uploads == []
    validate_fit(st.fit_path("42").read_bytes())
    st.close()


def test_remote_existing_activity_never_overwritten(tmp_path):
    st = Store(tmp_path)
    g = GarminFake()
    i = IntervalsFake(remote=[{"id": "i42", "source": "GARMIN_CONNECT",
                              "start_date": "2026-10-07T08:00:20Z"}])
    result = sync_activities(settings(tmp_path), g, i, st, apply=True, allow_upload=True,
                             today=date(2026, 10, 8))
    assert result["remote_exists"] == 1 and not i.uploads
    assert st.activity_status("42") == "remote_exists"
    st.close()


def test_post_pending_after_timeout_prevents_retry(tmp_path):
    st = Store(tmp_path)
    g, i = GarminFake(), IntervalsFake()
    i.fail_upload = True
    with pytest.raises(TimeoutError):
        sync_activities(settings(tmp_path), g, i, st, apply=True, allow_upload=True,
                        today=date(2026, 10, 8))
    assert st.activity_status("42") == "pending"
    i.fail_upload = False
    result = sync_activities(settings(tmp_path), g, i, st, apply=True, allow_upload=True,
                             today=date(2026, 10, 8))
    assert result["pending"] == 1 and i.uploads == []
    st.close()


def test_successful_upload_is_only_once(tmp_path):
    st = Store(tmp_path)
    g, i = GarminFake(), IntervalsFake()
    result = sync_activities(settings(tmp_path), g, i, st, apply=True, allow_upload=True,
                             today=date(2026, 10, 8))
    assert result["uploaded"] == 1
    assert st.activity_status("42") == "uploaded"
    sync_activities(settings(tmp_path), g, i, st, apply=True, allow_upload=True,
                    today=date(2026, 10, 8))
    assert len(i.uploads) == 1
    st.close()


def test_wellness_dryrun_does_not_write(tmp_path):
    st = Store(tmp_path)
    g, i = GarminFake(), IntervalsFake()
    i.current_wellness = {"locked": False, "restingHR": 44,
                          "customFields": {"BodyBatteryMax": 95, "TrainingAdvice": 2}}
    result = sync_wellness(settings(tmp_path), g, i, st, apply=False, wellness_days=2,
                           today=date(2026, 10, 8))
    assert result["days_checked"] == 2
    assert result["days_with_changes"] == 2
    assert not i.wellness_writes
    assert i.provision_calls and all(not apply for apply, _ in i.provision_calls)
    assert st.wellness_recent(date(2026, 10, 8), 8) is False
    st.close()


def test_wellness_apply_never_overwrites_existing_and_respects_lock(tmp_path):
    st = Store(tmp_path)
    g, i = GarminFake(), IntervalsFake()
    i.current_wellness = {"locked": False, "hrv": 55, "customFields": {"BodyBatteryMax": 99}}
    result = sync_wellness(settings(tmp_path), g, i, st, apply=True, wellness_days=1,
                           today=date(2026, 10, 8))
    assert result["writes"] == 1
    day, patch = i.wellness_writes[0]
    assert "hrv" not in patch
    assert "BodyBatteryMax" not in patch.get("customFields", {}) or patch["customFields"]["BodyBatteryMax"] == 99
    assert st.wellness_recent(date(2026, 10, 8), 8)
    i.current_wellness = {"locked": True}
    result = sync_wellness(settings(tmp_path), g, i, st, apply=True, wellness_days=1,
                           force=True, today=date(2026, 10, 8))
    assert result["days_locked"] == 1
    assert len(i.wellness_writes) == 1
    st.close()
