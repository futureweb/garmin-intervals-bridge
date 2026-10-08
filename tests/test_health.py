import json
import time
from datetime import date, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_mapping import sample

from garmin_intervals_bridge.health import probe, silent_error_checks
from garmin_intervals_bridge.store import Store


def cfg(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), wellness_profile="recommended",
                           stale_hours=24)


class Garmin:
    def __init__(self, ok=True):
        self.ok = ok

    def login(self, interactive=False):
        if not self.ok:
            raise RuntimeError("Garmin login needed")


class Intervals:
    def __init__(self, ok=True):
        self.ok = ok

    def activities(self, start, end, fields=None, limit=None):
        if not self.ok:
            raise ConnectionError("down")
        return []


def test_healthy_probe_records_success_and_exits_zero(tmp_path):
    st = Store(tmp_path)
    r = probe(cfg(tmp_path), Garmin(), Intervals(), st)
    assert r["exit"] == 0 and r["services"]["garmin"]["ok"] and r["services"]["intervals"]["ok"]
    assert st.meta_updated("garmin_last_ok") is not None
    st.close()


def test_failure_inside_the_window_is_a_warning_not_an_alert(tmp_path):
    st = Store(tmp_path)
    st.set_meta("garmin_last_ok")                       # succeeded just now
    r = probe(cfg(tmp_path), Garmin(ok=False), Intervals(), st, now=time.time() + 3 * 3600)
    assert r["exit"] == 0 and r["services"]["garmin"]["ok"] is False
    assert "stale" not in r["services"]["garmin"] and r["services"]["garmin"]["last_ok_age_hours"] == 3.0
    st.close()


def test_failure_beyond_the_window_or_never_ok_exits_two(tmp_path):
    st = Store(tmp_path)
    r = probe(cfg(tmp_path), Garmin(ok=False), Intervals(), st)
    assert r["exit"] == 2 and r["services"]["garmin"]["stale"] is True          # never succeeded
    st.set_meta("garmin_last_ok")
    r = probe(cfg(tmp_path), Garmin(ok=False), Intervals(), st, now=time.time() + 30 * 3600)
    assert r["exit"] == 2 and r["services"]["garmin"]["last_ok_age_hours"] == 30.0
    st.close()






def write_snapshot(tmp_path, day, *, errors=None, drop_hrv=False):
    snap = sample()
    snap["date"] = day.isoformat()
    snap["errors"] = errors or {}
    if drop_hrv:
        snap["data"].pop("hrv")
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "raw" / f"{day.isoformat()}.json").write_text(json.dumps(snap))


def test_endpoint_failing_three_days_and_field_dropout_are_alerts(tmp_path):
    st = Store(tmp_path)
    today = date(2026, 10, 8)
    for n in range(9, 0, -1):
        d = today - timedelta(days=n)
        write_snapshot(tmp_path, d, errors={"spo2": "HTTPError"} if n <= 3 else {}, drop_hrv=n <= 2)
    f = {x["what"]: x for x in silent_error_checks(cfg(tmp_path), st, now=time.time())}
    assert f["Garmin endpoint 'spo2' failing"]["alert"] is True
    drop = next(v for k, v in f.items() if "stopped arriving" in k)
    assert "hrv" in drop["detail"] and "GarminHRV7DayAvg" in drop["detail"] and drop["alert"] is True
    st.close()


def test_a_single_bad_day_is_not_an_alert(tmp_path):
    st = Store(tmp_path)
    today = date(2026, 10, 8)
    for n in range(9, 0, -1):
        write_snapshot(tmp_path, today - timedelta(days=n), errors={"spo2": "Timeout"} if n == 1 else {},
                       drop_hrv=(n == 1))
    assert not [x for x in silent_error_checks(cfg(tmp_path), st) if x["alert"]]
    st.close()


def test_dead_timers_and_repeated_failures_alert(tmp_path):
    st = Store(tmp_path)
    st.record_run("watch", {"new": 0})
    st.db.execute("UPDATE runs SET started = started - 5 * 3600")
    st.db.commit()
    st.record_failure("77", "boom")
    st.record_failure("77", "boom again")
    f = {x["what"]: x for x in silent_error_checks(cfg(tmp_path), st)}
    assert f["one-minute watch not running"]["alert"] is True
    assert f["1 activities failing repeatedly"]["alert"] is True
    assert "30-minute sync not running" not in f            # never ran before: nothing to miss
    st.close()
