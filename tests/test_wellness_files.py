"""The device's original wellness files, Garmin's own splits and the account series."""
import io
import json
import zipfile
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from garmin_fit_sdk import Encoder
from test_fit import MESG, build_fit

from garmin_intervals_bridge.fit import wellness_bundle_index
from garmin_intervals_bridge.garmin import EXTRA_KEYS, GarminSource
from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import archive_activity, mirror_wellness_files, sync_wellness_files, wellness_check

DAY = date(2026, 10, 9)


def snapshot_fit(start=1160555508, rmssd=37):
    enc = Encoder()
    enc.write_mesg({"mesg_num": MESG["FILE_ID"], "type": "activity", "manufacturer": "garmin", "product": 4536,
                    "serial_number": 1, "time_created": start})
    enc.write_mesg({"mesg_num": MESG["USER_PROFILE"], "friendly_name": "Somebody Private"})
    enc.write_mesg({"mesg_num": MESG["SESSION"], "timestamp": start + 120, "start_time": start, "sport": 60,
                    "sport_profile_name": "Health Snapshot", "total_elapsed_time": 120.175, "avg_heart_rate": 63,
                    "min_heart_rate": 59, "max_heart_rate": 72, "rmssd_hrv": rmssd, "sdrr_hrv": 51, "avg_spo2": 99,
                    "avg_stress": 28, "enhanced_avg_respiration_rate": 17.94})
    return enc.close()


def bundle(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def test_index_counts_kinds_checks_every_file_and_summarises_snapshots():
    data = bundle([("1_WELLNESS.fit", build_fit()), ("2_WELLNESS.fit", build_fit()), ("3_METRICS.fit", build_fit()),
                   ("4_ACTIVITY.fit", snapshot_fit()), ("5_SLEEP_DATA.fit", b"broken")])
    index = wellness_bundle_index(data)
    assert index["files"] == 5 and index["kinds"] == {"ACTIVITY": 1, "METRICS": 1, "SLEEP_DATA": 1, "WELLNESS": 2}
    assert index["invalid"] == ["5_SLEEP_DATA.fit"] and len(index["sha256"]) == 64
    (snap,) = index["health_snapshots"]
    assert snap["hrv_rmssd"] == 37 and snap["hrv_sdrr"] == 51 and snap["hr_avg"] == 63 and snap["spo2_avg"] == 99
    assert snap["respiration_avg"] == 17.94 and snap["duration_s"] == pytest.approx(120.18, abs=0.02)
    assert snap["start_utc"] == "2026-10-10T08:31:48+00:00"
    assert "Somebody" not in json.dumps(index)                   # nothing personal leaves the files


def test_index_refuses_what_is_not_a_plausible_download():
    with pytest.raises(ValueError, match="not a ZIP"):
        wellness_bundle_index(b"<html>error</html>")
    from garmin_intervals_bridge import fit
    with pytest.raises(ValueError, match="implausibly large"):
        old = fit.MAX_WELLNESS_MEMBERS
        fit.MAX_WELLNESS_MEMBERS = 2
        try:
            wellness_bundle_index(bundle([(f"{i}_WELLNESS.fit", build_fit()) for i in range(3)]))
        finally:
            fit.MAX_WELLNESS_MEMBERS = old


class FilesGarmin:
    def __init__(self, payload=None):
        self.payload = payload
        self.asked = []

    def wellness_files(self, day):
        self.asked.append(day)
        return self.payload


def test_mirror_keeps_the_zip_as_delivered_and_asks_once(tmp_path):
    st = Store(tmp_path)
    data = bundle([("1_WELLNESS.fit", build_fit()), ("2_ACTIVITY.fit", snapshot_fit())])
    g = FilesGarmin(data)
    assert mirror_wellness_files(DAY, g, st) == "saved"
    assert st.wellness_files_path(DAY).read_bytes() == data
    assert st.load_wellness_index(DAY)["health_snapshots"][0]["hrv_rmssd"] == 37
    assert mirror_wellness_files(DAY, g, st) == "on_disk" and g.asked == [DAY]
    st.close()


def test_a_day_without_files_is_recorded_and_not_asked_again(tmp_path):
    st = Store(tmp_path)
    g = FilesGarmin(None)
    assert mirror_wellness_files(DAY, g, st) == "none"
    assert not st.wellness_files_path(DAY).exists() and st.load_wellness_index(DAY)["files"] == 0
    assert mirror_wellness_files(DAY, g, st) == "on_disk" and len(g.asked) == 1
    st.close()


def test_files_backfill_skips_days_on_disk_and_pauses_only_after_a_request(tmp_path, monkeypatch):
    from garmin_intervals_bridge import sync
    slept = []
    monkeypatch.setattr(sync.time, "sleep", lambda s: slept.append(s))
    st = Store(tmp_path)
    g = FilesGarmin(bundle([("1_ACTIVITY.fit", snapshot_fit())]))
    mirror_wellness_files(DAY, g, st)
    out = sync_wellness_files(None, g, st, days=[DAY, date(2026, 10, 10)], pause_seconds=3)
    assert out["on_disk"] == 1 and out["saved"] == 1 and out["health_snapshots"] == 1 and slept == [3]
    st.close()


def test_the_scheduled_run_mirrors_the_files_of_a_finished_day(tmp_path):
    from test_wellness_schedule import Garmin, Intervals, at
    st, g, i = Store(tmp_path), Garmin(), Intervals()
    st.set_meta("wellness:since", "test")
    st.set_meta("wellness:official_sleep", "2026-10-09")
    st.set_meta(f"wellness:final:{DAY}", "test")
    st.set_meta("wellness:morning:2026-10-10", "ok")
    s = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), wellness_days=2,
                        wellness_profile="all")
    m = wellness_check(s, g, i, st, apply=True, now=at(10, 0), throttle=True)
    assert g.files == [DAY] and m["files"] == 0                    # Garmin had none: recorded
    wellness_check(s, g, i, st, apply=True, now=at(10, 30), throttle=True)
    assert g.files == [DAY] and len(i.gets) == 1                   # nothing left to do
    st.close()


def test_an_older_archive_entry_gets_only_the_splits_it_lacks(tmp_path):
    st = Store(tmp_path)
    st.atomic_save(st.fit_path("42"), build_fit())
    st.save_activity_json("42", {"activityId": 42})
    st.save_activity_extras("42", {"weather": {"temp": 9}, "gear": None, "exercise_sets": None, "errors": {}})

    class G:
        asked = None

        def activity_extras(self, activity, keys=None):
            G.asked = keys
            return {"typed_splits": {"splits": [1]}, "split_summaries": {"splitSummaries": []}, "errors": {}}
    old = datetime(2025, 1, 1, tzinfo=timezone.utc)
    activity = {"activityId": 42, "startTimeGMT": "2024-06-01 08:00:00"}
    assert archive_activity("42", activity, G(), st, now=old) == "archived"
    assert G.asked == ("typed_splits", "split_summaries")
    saved = json.loads(st.activity_extras_path("42").read_text())
    assert saved["weather"] == {"temp": 9} and saved["typed_splits"] == {"splits": [1]}
    assert set(EXTRA_KEYS) <= set(saved)
    assert archive_activity("42", activity, G(), st, now=old) == "on_disk"
    st.close()


class AccountClient:
    """Answers every account call; records what was asked."""
    profile_id = 7

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "get_training_plans":
                return {"trainingPlanList": [{"trainingPlanId": 5, "trainingPlanCategory": "FBT_ADAPTIVE"},
                                             {"trainingPlanId": 6, "trainingPlanCategory": "PHASED"}]}
            return {}
        return call


def test_account_snapshot_reaches_back_in_chunks_only_when_asked(tmp_path):
    src = GarminSource(tmp_path, 0)
    src.client = AccountClient()
    recent = src.account_snapshot(today=date(2026, 10, 10))
    assert set(recent["data"]["scheduled_workouts"]) == {"2026-08", "2026-09", "2026-10", "2026-11"}
    assert len(recent["data"]["ftp_history_cycling"]) == 1 and len(recent["data"]["running_tolerance"]) == 1
    names = [c[0] for c in src.client.calls]
    assert names.count("get_goals") == 3 and "get_gear_defaults" in names
    assert ("get_adaptive_training_plan_by_id", ("5",), {}) in src.client.calls
    assert ("get_training_plan_by_id", ("6",), {}) in src.client.calls
    src.client.calls.clear()
    full = src.account_snapshot(history_start=date(2020, 1, 1), today=date(2026, 10, 10))
    chunks = full["data"]["ftp_history_cycling"]
    assert chunks[0]["start"] == "2020-01-01" and chunks[-1]["end"] == "2026-10-10" and len(chunks) == 14
    assert all(c[2].get("sport") == "CYCLING" for c in src.client.calls
               if c[0] == "get_functional_threshold_power_range" and c[2].get("sport") == "CYCLING")
    assert len(full["data"]["scheduled_workouts"]) == 83 and full["history_start"] == "2020-01-01"
