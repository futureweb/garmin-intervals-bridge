"""Forum #210/#212: blood pressure typed into Garmin Connect (or from an Index BPM, one averaged entry per
three-reading session) into Intervals' systolic and diastolic. The bridge read it from Garmin already and
kept it in the archive; it wrote nothing."""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from test_health import cfg, write_snapshot
from test_mapping import sample
from test_sync import GarminFake, IntervalsFake, settings

from garmin_intervals_bridge import config
from garmin_intervals_bridge.health import silent_error_checks
from garmin_intervals_bridge.mapping import blood_pressure, map_wellness, merge_wellness
from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import sync_wellness

DAY = date(2026, 11, 27)
VIENNA = ZoneInfo("Europe/Vienna")


def reading(local, systolic, diastolic, gmt=None):
    return {"systolic": systolic, "diastolic": diastolic, "pulse": 61, "measurementTimestampLocal": local,
            "measurementTimestampGMT": gmt or local, "sourceType": "MANUAL", "category": 1, "version": 1}


def answer(*readings, day=DAY):
    """Garmin's get_blood_pressure() answer for one day, in the shape the archive holds."""
    return {"from": day.isoformat(), "until": day.isoformat(), "categoryStats": None,
            "measurementSummaries": [{"startDate": day.isoformat(), "endDate": day.isoformat(),
                                      "numOfMeasurements": len(readings), "measurements": list(readings)}]
            if readings else []}


def final(snapshot, final_read=True):
    """A snapshot as the day's final read leaves it (per-endpoint provenance since 0.12.0)."""
    return {**snapshot, "final": final_read, "provenance": {"blood_pressure": {"source": "direct", "final": final_read}}}


MORNING, EVENING = reading("2026-11-27T07:12:00.123", 122, 79), reading("2026-11-27T19:05:00.0", 132, 85)


# ---- the day's reading ----

def test_the_first_reading_of_the_day_counts_by_default():
    assert blood_pressure(answer(EVENING, MORNING), DAY) == (122, 79)          # by time, not by list order


def test_the_average_of_the_day():
    assert blood_pressure(answer(MORNING, EVENING), DAY, "average") == (127, 82)


def test_first_goes_by_gmt_when_the_clock_goes_back():
    # 26 Oct 2025, Vienna: 02:50 CEST (00:50 UTC) was taken before 02:10 CET (01:10 UTC)
    early = reading("2025-10-26T02:50:00.0", 121, 78, gmt="2025-10-26T00:50:00.0")
    late = reading("2025-10-26T02:10:00.0", 135, 88, gmt="2025-10-26T01:10:00.0")
    assert blood_pressure(answer(late, early, day=date(2025, 10, 26)), date(2025, 10, 26)) == (121, 78)


def test_without_gmt_a_local_offset_tells_the_order_when_the_clock_goes_back():
    # review 0.12.22 round 3: local times with offsets and no GMT - 02:50+02:00 came before 02:10+01:00
    early = reading("2025-10-26T02:50:00+02:00", 121, 78)
    late = reading("2025-10-26T02:10:00+01:00", 135, 88)
    for r in (early, late):
        r.pop("measurementTimestampGMT")
    assert blood_pressure(answer(late, early, day=date(2025, 10, 26)), date(2025, 10, 26)) == (121, 78)


def test_without_gmt_on_every_reading_all_go_by_local_time():
    # review 0.12.22 round 2: a local 08:00 without GMT lost to 08:30 whose GMT read 07:30
    eight = reading("2026-11-27T08:00:00.0", 121, 78)
    eight.pop("measurementTimestampGMT")
    half_past = reading("2026-11-27T08:30:00.0", 135, 88, gmt="2026-11-27T07:30:00.0")
    assert blood_pressure(answer(half_past, eight), DAY) == (121, 78)


def test_readings_of_another_day_without_a_time_or_out_of_range_are_left_out():
    raw = answer(reading("2026-11-26T23:50:00.0", 140, 90),                   # the evening before
                 reading("2026-11-27", 101, 61),                              # no time: not "midnight"
                 reading("2026-11-27Tnoon", 102, 62),                         # no readable time
                 reading("2026-11-27T06:00:00.0", 80, 95),                    # diastolic not below systolic
                 reading("2026-11-27T06:30:00.0", 999, 80),                   # a typo
                 reading("2026-11-27T07:00:00.0", 118, 76))
    assert blood_pressure(raw, DAY) == (118, 76)
    assert blood_pressure(raw, DAY, "average") == (118, 76)
    assert blood_pressure(answer(), DAY) is None and blood_pressure(None, DAY) is None
    assert blood_pressure(raw, DAY, "off") is None


def test_an_average_that_rounds_to_equal_values_is_left_out():
    raw = answer(reading("2026-11-27T07:00:00.0", 100, 99), reading("2026-11-27T19:00:00.0", 101, 100))
    assert blood_pressure(raw, DAY, "average") is None                         # 100.5/99.5 would be 100/100


@pytest.mark.parametrize("broken", [1, "x", None, {"a": 1}, [1, None, "x"]])
def test_an_odd_answer_is_no_reading_and_no_error(broken):
    assert blood_pressure({"measurementSummaries": [{"measurements": broken}, broken]}, DAY) is None
    assert blood_pressure({"measurementSummaries": broken}, DAY) is None


# ---- into the wellness record ----

def test_only_a_read_after_the_day_ended_fills_the_native_fields():
    snap = sample()
    snap["data"]["blood_pressure"] = answer(MORNING, EVENING)
    tomorrow = DAY + timedelta(days=1)
    native, custom = map_wellness(final(snap), DAY, tomorrow)
    assert (native["systolic"], native["diastolic"]) == (122, 79)
    assert not [k for k in custom if "ystolic" in k or "iastolic" in k]       # Intervals' own fields only
    assert map_wellness(final(snap), DAY, tomorrow, "recommended", "average")[0]["systolic"] == 127
    assert "systolic" not in map_wellness(final(snap), DAY, tomorrow, "recommended", "off")[0]
    # a midday read, or one kept from it after a failed final read: the fields are filled once only
    assert "systolic" not in map_wellness(final(snap, False), DAY, tomorrow)[0]
    assert "systolic" not in map_wellness({**final(snap), "errors": {"blood_pressure": "HTTPError"}}, DAY, tomorrow)[0]
    assert "systolic" not in map_wellness(final(snap), DAY, DAY)[0]                     # the day still runs


def test_one_half_typed_in_gets_no_other_half():
    native = {"systolic": 127, "diastolic": 82, "restingHR": 50}
    assert merge_wellness({"systolic": 75, "diastolic": None}, native, {}) == {"restingHR": 50}
    assert merge_wellness({"systolic": None, "diastolic": 70}, native, {}) == {"restingHR": 50}
    assert merge_wellness({"systolic": None, "diastolic": None}, native, {}) == native


def _replay(tmp_path, snapshot, mode="first"):
    st = Store(tmp_path)
    intervals = IntervalsFake()
    intervals.current_wellness = {"locked": False}
    st.save_snapshot(DAY, snapshot)
    s = settings(tmp_path)
    s.blood_pressure = mode
    sync_wellness(s, None, intervals, st, apply=True, days=[DAY], today=DAY + timedelta(days=1), from_archive=True)
    st.close()
    return intervals.wellness_writes[-1][1] if intervals.wellness_writes else {}


def test_the_setting_reaches_the_written_day(tmp_path):
    snap = sample()
    snap["data"]["blood_pressure"] = answer(MORNING, EVENING)
    written = _replay(tmp_path, final(snap), "average")
    assert (written["systolic"], written["diastolic"]) == (127, 82)


def test_a_midday_snapshot_in_the_archive_writes_nothing(tmp_path):
    # review 0.12.22: replaying a snapshot read at noon wrote 122/79; the complete day's 127/82 never came
    snap = sample()
    snap["data"]["blood_pressure"] = answer(MORNING)
    assert "systolic" not in _replay(tmp_path, final(snap, False), "average")


class BloodPressureGarmin(GarminFake):
    def __init__(self):
        super().__init__()
        self.asked = []

    def snapshot(self, day, endpoints=None):
        self.asked.append(endpoints)
        return {"date": day.isoformat(), "errors": {}, "data": {"blood_pressure": answer(MORNING, EVENING)}}


def test_an_archive_from_before_0_12_needs_the_blood_pressure_read_again(tmp_path):
    # review 0.12.22 round 2: such a snapshot cannot show whether its list came with a read after the day
    # ended (0.11.7 kept a midday list when a later backfill read only some endpoints)
    snap = sample()
    snap["data"]["blood_pressure"] = answer(MORNING, EVENING)
    st = Store(tmp_path)
    intervals = IntervalsFake()
    intervals.current_wellness = {"locked": False}
    st.save_snapshot(DAY, {"date": DAY.isoformat(), "errors": {}, "data": snap["data"]})     # no provenance
    s = settings(tmp_path)
    s.blood_pressure = "first"
    today = DAY + timedelta(days=1)
    sync_wellness(s, None, intervals, st, apply=True, days=[DAY], today=today, from_archive=True)
    assert not any("systolic" in w[1] for w in intervals.wellness_writes)
    garmin = BloodPressureGarmin()                     # backfill --endpoints blood_pressure --force-wellness
    sync_wellness(s, garmin, intervals, st, apply=True, days=[DAY], today=today, force=True,
                  endpoints=("blood_pressure",))
    assert garmin.asked == [("blood_pressure",)]
    written = intervals.wellness_writes[-1][1]
    assert (written["systolic"], written["diastolic"]) == (122, 79)
    assert st.load_snapshot(DAY)["provenance"]["blood_pressure"]["final"] is True
    st.close()


def test_an_old_archive_counts_when_the_user_vouches_for_it(tmp_path):
    # backfill --from-archive --trust-old-archive: the author's 77 days from the October 2026 mirror
    def old(day):
        snap = sample()
        snap["data"]["blood_pressure"] = answer(reading(f"{day.isoformat()}T07:12:00.0", 122, 79),
                                                reading(f"{day.isoformat()}T19:05:00.0", 132, 85), day=day)
        return {"date": day.isoformat(), "errors": {}, "data": snap["data"]}     # saved by 0.11 or older
    june, november = date(2026, 6, 3), DAY
    st = Store(tmp_path)
    intervals = IntervalsFake()
    intervals.current_wellness = {"locked": False}
    s = settings(tmp_path)
    s.blood_pressure = "first"
    for day, snapshot, expected in (
            (june, old(june), (122, 79)),
            (november, old(november), None),     # review rounds 4/5: a later day's direct read looks the same
            (june, {**old(june), "errors": {"blood_pressure": "HTTPError"}}, None),        # its read failed
            (june, {**old(june), "final": False}, None),
            (june, final(old(june), False), None)):         # saved by 0.12.0 or newer: provenance decides
        intervals.wellness_writes.clear()
        st.save_snapshot(day, snapshot)
        sync_wellness(s, None, intervals, st, apply=True, days=[day], today=DAY + timedelta(days=1),
                      from_archive=True, trust_old_archive=True)
        written = intervals.wellness_writes[-1][1] if intervals.wellness_writes else {}
        assert (written.get("systolic"), written.get("diastolic")) == (expected or (None, None)), (day, snapshot.keys())
    st.close()


@pytest.mark.parametrize("args", [["--scope", "wellness"], ["--scope", "activities", "--from-archive"],
                                  ["--scope", "wellness", "--from-archive", "--wellness-files"],
                                  ["--scope", "wellness", "--from-archive", "--archive-only"]])
def test_trust_old_archive_goes_with_the_archive_replay_only(args, capsys):
    from garmin_intervals_bridge import cli
    with pytest.raises(SystemExit) as stop:                    # refused before any Garmin sign-in
        cli.main(["backfill", "--from", "2026-01-01", "--trust-old-archive", *args])
    assert stop.value.code == 2 and "--trust-old-archive goes with" in capsys.readouterr().err


def test_the_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVALS_API_KEY", "k")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("BRIDGE_BLOOD_PRESSURE", raising=False)
    assert config.Settings.from_env().blood_pressure == "first"
    monkeypatch.setenv("BRIDGE_BLOOD_PRESSURE", " Average ")
    assert config.Settings.from_env().blood_pressure == "average"
    monkeypatch.setenv("BRIDGE_BLOOD_PRESSURE", "morning")
    with pytest.raises(ValueError, match="BRIDGE_BLOOD_PRESSURE"):
        config.Settings.from_env()
    assert config.OPTIONS["blood_pressure"] == "BRIDGE_BLOOD_PRESSURE"       # the Home Assistant app's form


def test_days_without_a_reading_are_no_dropout(tmp_path):
    # measured every day for a week, then not for two days: nobody measures every day, no alert
    st = Store(tmp_path)
    today = date(2026, 10, 10)
    now = datetime(2026, 10, 10, 8, 44, tzinfo=VIENNA).timestamp()
    for n in range(9, -1, -1):
        day = today - timedelta(days=n)
        write_snapshot(tmp_path, day)
        if n > 2:
            path = tmp_path / "raw" / f"{day.isoformat()}.json"
            snap = json.loads(path.read_text())
            snap["data"]["blood_pressure"] = answer(reading(f"{day.isoformat()}T07:00:00.0", 120, 80), day=day)
            path.write_text(json.dumps(final(snap)))          # read after the day: the mapping would take it
    assert not [x for x in silent_error_checks(cfg(tmp_path), st, now=now) if x["alert"]]
    st.close()
