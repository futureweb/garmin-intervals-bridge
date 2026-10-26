"""When the scheduled runs ask Garmin for wellness: on evidence, not on a clock."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import MORNING_ENDPOINTS, sync_enrich, wellness_check

TZ = ZoneInfo("Europe/Vienna")
TODAY = date(2026, 10, 10)
YESTERDAY = TODAY - timedelta(days=1)


def at(hour, minute=0, day=TODAY):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def settings(tmp_path, days=2):
    return SimpleNamespace(data_dir=tmp_path, timezone=TZ, wellness_days=days, wellness_profile="all",
                           activity_days=3)


class Garmin:
    """Answers like Garmin Connect: last night's sleep only once the watch has synced."""

    def __init__(self):
        self.reads = []
        self.files = []
        self.sleep = False
        self.readiness = False
        self.last_sync = None            # naive GMT string, as Garmin sends it

    def wellness_files(self, day):
        self.files.append(day)
        return None                                  # no files that day (recorded, not asked again)

    def snapshot(self, day, endpoints=None):
        self.reads.append((day, endpoints))
        names = endpoints or ("stats", "sleep", "hrv", "training_readiness", "body_composition")
        data = {}
        for name in names:
            if name == "sleep":
                has = self.sleep or day < TODAY
                data["sleep"] = {"dailySleepDTO": {"sleepTimeSeconds": 28080, "averageSpO2Value": 95,
                                                   "averageRespirationValue": 14.5} if has else {},
                                 **({"restingHeartRate": 49} if has else {})}
            elif name == "hrv":
                data["hrv"] = {"hrvSummary": {"lastNightAvg": 38}}
            elif name == "training_readiness":
                data["training_readiness"] = ([{"inputContext": "AFTER_WAKEUP_RESET", "score": 81,
                                                "timestampLocal": f"{day}T07:00:00"}]
                                              if self.readiness or day < TODAY else [])
            elif name == "stats":
                data["stats"] = {"totalSteps": 9001, "lastSyncTimestampGMT": self.last_sync if day == TODAY else None}
            else:
                data[name] = {}
        return {"date": day.isoformat(), "data": data, "errors": {}}


class Intervals:
    def __init__(self):
        self.records = {}
        self.gets = []
        self.writes = []
        self.ranges = []

    def wellness(self, day):
        self.gets.append(day)
        return dict(self.records.get(day, {"id": day.isoformat()}))

    def write_wellness(self, day, changes):
        self.writes.append((day, changes))
        self.records.setdefault(day, {}).update(changes)

    def wellness_range(self, oldest, newest, cols=None):
        self.ranges.append((oldest, newest))
        return [{"id": d.isoformat(), **r} for d, r in sorted(self.records.items()) if oldest <= d <= newest]

    def provision_fields(self, apply, needed):
        return list(needed)


def fresh(tmp_path, days=2):
    st, g, i = Store(tmp_path), Garmin(), Intervals()
    st.set_meta("wellness:since", "test")                              # skip the first-run migration
    st.set_meta("wellness:official_sleep", YESTERDAY.isoformat())       # the official sync delivers sleep
    for day in [TODAY - timedelta(days=o) for o in range(1, days)]:
        st.set_meta(f"wellness:final:{day}", "test")                   # past days already done
    return st, g, i


def run(tmp_path, st, g, i, now, apply=True, throttle=False, days=2):
    return wellness_check(settings(tmp_path, days), g, i, st, apply=apply, now=now, throttle=throttle)


def test_nothing_is_asked_of_garmin_before_the_night_arrives(tmp_path):
    st, g, i = fresh(tmp_path)
    for hour in (1, 3, 5, 7, 9):
        run(tmp_path, st, g, i, at(hour))
    assert g.reads == [] and len(i.gets) == 5                  # one small Intervals request per check, no Garmin


def test_official_sleep_triggers_exactly_one_morning_read(tmp_path):
    st, g, i = fresh(tmp_path)
    run(tmp_path, st, g, i, at(9, 40))
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080, "hrv": 38}           # the official integration delivered the night
    m = run(tmp_path, st, g, i, at(9, 50))
    assert g.reads == [(TODAY, MORNING_ENDPOINTS)] and m["morning"] == 1
    day, changes = i.writes[-1]
    assert day == TODAY and changes["spO2"] == 95 and changes["respiration"] == 14.5 and changes["readiness"] == 81
    assert "sleepSecs" not in changes and "hrv" not in changes  # what the official sync brought stays untouched
    run(tmp_path, st, g, i, at(10, 30))
    run(tmp_path, st, g, i, at(23, 0))
    assert len(g.reads) == 1                                   # done with Garmin; Intervals is still watched
    st.close()


def test_a_snapshot_import_during_the_day_is_put_back_the_same_day(tmp_path):
    import io
    import zipfile

    from test_wellness_files import snapshot_fit
    st, g, i = fresh(tmp_path)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080, "hrv": 38, "restingHR": 49}
    run(tmp_path, st, g, i, at(8, 0))                          # morning: the night is HRV 38, resting HR 49
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1_ACTIVITY.fit", snapshot_fit())           # RMSSD 37, HR 63
    asked = []
    g.wellness_files = lambda day: asked.append(day) or buf.getvalue()
    run(tmp_path, st, g, i, at(10, 0))
    assert asked == []                                         # nothing moved: Garmin not asked
    i.records[TODAY].update({"hrv": 37, "restingHR": 63})      # the official integration imported the snapshot
    m = run(tmp_path, st, g, i, at(10, 40))
    assert asked == [TODAY] and m["snapshot_fixes"] == 1
    assert i.records[TODAY]["hrv"] == 38 and i.records[TODAY]["restingHR"] == 49
    assert i.records[TODAY]["GarminSnapshotRMSSD"] == 37 and i.records[TODAY]["GarminSnapshotHR"] == 63
    assert st.load_wellness_index(TODAY) is None               # the running day's files are not kept yet
    run(tmp_path, st, g, i, at(11, 0))
    assert asked == [TODAY]                                    # back to the night's values: nothing to do
    st.close()


def test_the_check_is_throttled_when_called_every_minute(tmp_path):
    st, g, i = fresh(tmp_path)
    for minute in range(0, 10):
        run(tmp_path, st, g, i, at(6, minute), throttle=True)
    assert len(i.gets) == 1 and g.reads == []
    for minute in range(10, 30):                                # ... and asks again every ten minutes
        run(tmp_path, st, g, i, at(6, minute), throttle=True)
    assert len(i.gets) == 3
    st.close()


def test_a_lagging_morning_readiness_gets_two_more_tries(tmp_path):
    st, g, i = fresh(tmp_path)
    g.sleep = True
    i.records[TODAY] = {"sleepSecs": 28080}
    run(tmp_path, st, g, i, at(7, 0))
    assert st.get_meta(f"wellness:morning:{TODAY}") == "no-readiness"
    run(tmp_path, st, g, i, at(7, 10))                         # too early for the retry
    assert len(g.reads) == 1
    run(tmp_path, st, g, i, at(7, 31))
    assert g.reads[-1] == (TODAY, ("training_readiness",))
    g.readiness = True
    run(tmp_path, st, g, i, at(8, 2))
    assert st.get_meta(f"wellness:morning:{TODAY}") == "ok" and i.writes[-1][1]["readiness"] == 81
    run(tmp_path, st, g, i, at(9, 0))
    assert len(g.reads) == 3
    st.close()


def test_without_the_official_signal_garmin_is_checked_at_a_few_fixed_times(tmp_path):
    st, g, i = fresh(tmp_path)
    st.set_meta("wellness:official_sleep", (TODAY - timedelta(days=5)).isoformat())   # no official sleep lately
    run(tmp_path, st, g, i, at(6, 0))
    assert g.reads == []
    run(tmp_path, st, g, i, at(6, 31))                         # first slot: Garmin has no night yet
    run(tmp_path, st, g, i, at(6, 45))
    assert len(g.reads) == 1
    g.sleep = g.readiness = True
    run(tmp_path, st, g, i, at(7, 15))                         # backoff over, but no new slot yet
    assert len(g.reads) == 1
    run(tmp_path, st, g, i, at(7, 31))                         # next slot
    assert len(g.reads) == 2 and st.get_meta(f"wellness:morning:{TODAY}") == "ok"
    st.close()


def test_with_the_signal_garmin_is_still_asked_late_if_the_official_sync_stays_silent(tmp_path):
    st, g, i = fresh(tmp_path)
    g.sleep = True
    run(tmp_path, st, g, i, at(11, 59))
    assert g.reads == []
    run(tmp_path, st, g, i, at(12, 1))
    assert g.reads == [(TODAY, MORNING_ENDPOINTS)]
    st.close()


def test_yesterday_is_read_in_full_once_the_device_synced_after_midnight(tmp_path):
    st, g, i = fresh(tmp_path)
    st.delete_meta(f"wellness:final:{YESTERDAY}", 1e12)
    run(tmp_path, st, g, i, at(0, 20))
    assert g.reads == []                                       # nothing new in Intervals: no reason to ask
    i.records[TODAY] = {"steps": 120}                          # a night-time sync reached the official integration
    g.last_sync = "2026-10-09T23:53:46.931"                    # 01:53 local
    run(tmp_path, st, g, i, at(2, 0))
    assert g.reads == [(TODAY, ("stats",)), (YESTERDAY, None)]
    assert st.get_meta(f"wellness:final:{YESTERDAY}") == "synced"
    assert any(day == YESTERDAY and "steps" in changes for day, changes in i.writes)
    run(tmp_path, st, g, i, at(2, 30))
    assert len(g.reads) == 2
    st.close()


def test_a_touch_without_a_device_sync_backs_off(tmp_path):
    st, g, i = fresh(tmp_path)
    st.delete_meta(f"wellness:final:{YESTERDAY}", 1e12)
    i.records[TODAY] = {"weight": 82.5}                        # a scale, not the watch
    g.last_sync = "2026-10-09T20:00:00"                        # 22:00 local, before midnight
    run(tmp_path, st, g, i, at(1, 0))
    run(tmp_path, st, g, i, at(1, 30))
    assert g.reads == [(TODAY, ("stats",))]                    # verified once, then wait an hour
    run(tmp_path, st, g, i, at(2, 1))
    assert len(g.reads) == 2 and st.get_meta(f"wellness:final:{YESTERDAY}") is None
    st.close()


def test_the_morning_read_also_finishes_yesterday(tmp_path):
    st, g, i = fresh(tmp_path)
    st.delete_meta(f"wellness:final:{YESTERDAY}", 1e12)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080}
    run(tmp_path, st, g, i, at(8, 0))
    assert g.reads == [(TODAY, MORNING_ENDPOINTS), (YESTERDAY, None)]
    st.close()


def test_a_day_without_any_sync_is_read_anyway_the_evening_after(tmp_path):
    st, g, i = fresh(tmp_path)
    st.delete_meta(f"wellness:final:{YESTERDAY}", 1e12)
    st.set_meta("wellness:official_sleep", (TODAY - timedelta(days=5)).isoformat())
    run(tmp_path, st, g, i, at(19, 59))
    assert (YESTERDAY, None) not in g.reads
    run(tmp_path, st, g, i, at(20, 1))
    assert (YESTERDAY, None) in g.reads and st.get_meta(f"wellness:final:{YESTERDAY}") == "anyway"
    st.close()


def test_first_run_counts_days_read_after_they_ended_as_final(tmp_path):
    st, g, i = Store(tmp_path), Garmin(), Intervals()
    st.mark_wellness(YESTERDAY)                                # read "now", i.e. after yesterday ended
    run(tmp_path, st, g, i, at(10, 0))
    assert st.get_meta(f"wellness:final:{YESTERDAY}") == "migrated"
    assert all(day != YESTERDAY for day, _ in g.reads)
    st.close()


def test_a_dry_run_reads_once_and_apply_writes_from_the_archive(tmp_path):
    st, g, i = fresh(tmp_path)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080}
    run(tmp_path, st, g, i, at(8, 0), apply=False)
    assert len(g.reads) == 1 and i.writes == []
    m = run(tmp_path, st, g, i, at(8, 1), apply=True)
    assert len(g.reads) == 1 and m["days_from_archive"] == 1 and i.writes[0][0] == TODAY
    run(tmp_path, st, g, i, at(8, 2), apply=True)
    assert len(i.writes) == 1
    st.close()


# ---- the Garmin activity scan of the scheduled runs ----

class ListingGarmin:
    def __init__(self):
        self.listings = 0

    def activities(self, start, end):
        self.listings += 1
        return []


class QuietIntervals:
    def activities(self, *a, **k):
        return []

    def custom_items(self):
        return []


def test_the_activity_scan_runs_every_two_hours_unless_a_retry_is_due(tmp_path):
    st, g, i = Store(tmp_path), ListingGarmin(), QuietIntervals()
    s = settings(tmp_path)
    sync_enrich(s, g, i, st, apply=True, scan_interval_minutes=120)
    m = sync_enrich(s, g, i, st, apply=True, scan_interval_minutes=120)
    assert g.listings == 1 and m["scan_skipped"] == 1
    sync_enrich(s, g, i, st, apply=True)                       # a person's sync always scans
    assert g.listings == 2
    st.record_failure("42", "ValueError: no file")
    st.db.execute("UPDATE activity SET next_retry = 0")
    st.db.commit()
    sync_enrich(s, g, i, st, apply=True, scan_interval_minutes=120)
    assert g.listings == 3
    st.close()


def test_this_mornings_recovery_is_replaced_by_the_evenings_at_the_final_read(tmp_path):
    st, g, i = fresh(tmp_path)
    readings = {"morning": [{"inputContext": "AFTER_WAKEUP_RESET", "timestampLocal": f"{TODAY}T07:00:00",
                             "score": 77, "recoveryTime": 1088, "acuteLoad": 507}]}
    original = g.snapshot

    def snapshot(day, endpoints=None):
        raw = original(day, endpoints)
        if "training_readiness" in raw["data"]:
            raw["data"]["training_readiness"] = list(readings["morning"]) + readings.get(str(day), [])
        return raw
    g.snapshot = snapshot
    g.sleep = True
    i.records[TODAY] = {"sleepSecs": 28080}
    run(tmp_path, st, g, i, at(8, 0))
    assert i.records[TODAY]["GarminAcuteLoad"] == 507 and i.records[TODAY]["GarminRecoveryTimeMinutes"] == 1088
    readings[str(TODAY)] = [{"inputContext": "AFTER_POST_EXERCISE_RESET", "timestampLocal": f"{TODAY}T19:00:00",
                             "score": 40, "recoveryTime": 2900, "acuteLoad": 640}]
    tomorrow = TODAY + timedelta(days=1)
    i.records[tomorrow] = {"sleepSecs": 27000}                 # tomorrow's morning sync: today is final
    run(tmp_path, st, g, i, at(7, 30, day=tomorrow))
    assert i.records[TODAY]["GarminAcuteLoad"] == 640 and i.records[TODAY]["GarminRecoveryTimeHours"] == 48.3
    assert i.records[TODAY]["GarminTrainingReadiness"] == 77   # the morning's readiness stays
    st.close()


# ---- what a failure must not cost (second review) ----

TOMORROW = TODAY + timedelta(days=1)


def _morning_written(tmp_path):
    st, g, i = fresh(tmp_path)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080}
    run(tmp_path, st, g, i, at(8, 0))
    return st, g, i


def test_a_failed_endpoint_in_the_final_read_keeps_the_archive_and_reads_again(tmp_path):
    st, g, i = _morning_written(tmp_path)
    assert st.load_snapshot(TODAY)["data"]["hrv"]
    real = g.snapshot

    def flaky(day, endpoints=None):
        raw = real(day, endpoints)
        if endpoints is None and day == TODAY:
            raw["data"].pop("hrv", None)
            raw["errors"]["hrv"] = "GarminConnectConnectionError 503"
        return raw
    g.snapshot = flaky
    i.records[TOMORROW] = {"sleepSecs": 27000}
    run(tmp_path, st, g, i, at(7, 30, day=TOMORROW))
    assert st.load_snapshot(TODAY)["data"]["hrv"]                      # the morning's HRV stays
    assert st.get_meta(f"wellness:final:{TODAY}") is None              # not final on a passing failure
    for hour in (9, 12, 16, 21):
        run(tmp_path, st, g, i, at(hour, 0, day=TOMORROW))
    finals = [r for r in g.reads if r == (TODAY, None)]
    assert len(finals) == 3 and st.get_meta(f"wellness:final:{TODAY}")   # three tries, then final anyway
    st.close()


def test_a_final_write_that_failed_is_written_again_with_the_evening_values(tmp_path):
    st, g, i = _morning_written(tmp_path)
    real = i.write_wellness
    failing = {"on": True}

    def write(day, changes):
        if failing["on"] and day == TODAY:
            raise RuntimeError("HTTP 502")
        return real(day, changes)
    i.write_wellness = write
    i.records[TOMORROW] = {"sleepSecs": 27000}
    run(tmp_path, st, g, i, at(7, 30, day=TOMORROW))
    assert st.get_meta(f"wellness:final:{TODAY}") and st.wellness_state(TODAY)[1] is None
    failing["on"] = False
    run(tmp_path, st, g, i, at(8, 0, day=TOMORROW))
    assert i.records[TODAY].get("steps") and st.wellness_state(TODAY)[1]     # the day's totals arrived
    assert not st.get_meta(f"wellness:rewrite:{TODAY}")
    st.close()


def test_a_health_snapshot_check_that_failed_is_done_again(tmp_path):
    st, g, i = _morning_written(tmp_path)
    i.records[TODAY]["hrv"] = 55                                         # a snapshot moved Intervals' HRV
    asked = []

    def broken(day):
        asked.append(day)
        raise ConnectionError("reset")
    g.wellness_files = broken
    run(tmp_path, st, g, i, at(10, 0))                                   # does not end the run
    assert st.get_meta(f"wellness:snapshot:{TODAY}") is None
    g.wellness_files = lambda day: asked.append(day) or None
    run(tmp_path, st, g, i, at(10, 30))
    assert len(asked) == 2 and st.get_meta(f"wellness:snapshot:{TODAY}") == "55"
    st.close()


# ---- today's resting HR: the night's in the morning, the day's at the final read ----

def _days_resting_hr(g, value):
    original = g.snapshot

    def snapshot(day, endpoints=None):
        raw = original(day, endpoints)
        if "stats" in raw["data"] and day == TODAY:
            raw["data"]["stats"]["restingHeartRate"] = value
        return raw
    g.snapshot = snapshot


def test_the_mornings_resting_hr_gives_way_to_the_days_at_the_final_read(tmp_path):
    st, g, i = _morning_written(tmp_path)
    assert i.records[TODAY]["restingHR"] == 49                 # the night's, so ImReady4 need not wait
    _days_resting_hr(g, 47)
    i.records[TOMORROW] = {"sleepSecs": 27000}
    run(tmp_path, st, g, i, at(7, 30, day=TOMORROW))
    assert i.records[TODAY]["restingHR"] == 47
    assert st.get_meta(f"wellness:provisional:{TODAY}") is None
    st.close()


def test_a_resting_hr_someone_else_set_stays(tmp_path):
    st, g, i = _morning_written(tmp_path)
    i.records[TODAY]["restingHR"] = 52                         # typed in, or the official import's
    _days_resting_hr(g, 47)
    i.records[TOMORROW] = {"sleepSecs": 27000}
    run(tmp_path, st, g, i, at(7, 30, day=TOMORROW))
    assert i.records[TODAY]["restingHR"] == 52
    st.close()


def test_the_official_imports_resting_hr_is_neither_written_nor_replaced(tmp_path):
    st, g, i = fresh(tmp_path)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080, "restingHR": 51}
    run(tmp_path, st, g, i, at(8, 0))
    assert st.get_meta(f"wellness:provisional:{TODAY}") is None
    _days_resting_hr(g, 47)
    i.records[TOMORROW] = {"sleepSecs": 27000}
    run(tmp_path, st, g, i, at(7, 30, day=TOMORROW))
    assert i.records[TODAY]["restingHR"] == 51
    st.close()


def _final_read(tmp_path, st, g, i):
    i.records[TOMORROW] = {"sleepSecs": 27000}
    run(tmp_path, st, g, i, at(7, 30, day=TOMORROW))


def test_only_exactly_the_bridges_value_is_replaced(tmp_path):
    for typed in (49.25, 52):
        st, g, i = _morning_written(tmp_path / str(typed))
        i.records[TODAY]["restingHR"] = typed                  # someone else's value, however close
        from garmin_intervals_bridge.sync import _write_wellness_day
        metrics = {"days_locked": 0, "days_with_changes": 0, "writes": 0, "new_fields": []}
        _write_wellness_day(TODAY, st.load_snapshot(TODAY), TODAY, settings(tmp_path), i, metrics, apply=True,
                            store=st)                          # seen at a later write: no longer the bridge's
        i.records[TODAY]["restingHR"] = 49 if typed == 52 else typed      # and changed back
        _days_resting_hr(g, 47)
        _final_read(tmp_path / str(typed), st, g, i)
        assert i.records[TODAY]["restingHR"] == (49 if typed == 52 else typed)
        st.close()


def test_a_snapshot_correction_does_not_make_the_value_the_bridges(tmp_path):
    st, g, i = fresh(tmp_path)
    g.sleep = g.readiness = True
    i.records[TODAY] = {"sleepSecs": 28080, "restingHR": 63}
    raw = {"date": TODAY.isoformat(), "data": {"sleep": {"restingHeartRate": 49, "dailySleepDTO": {}},
                                               "health_snapshots": [{"hr_avg": 63}]}, "errors": {}}
    from garmin_intervals_bridge.sync import _write_wellness_day
    metrics = {"days_locked": 0, "days_with_changes": 0, "writes": 0, "new_fields": []}
    _write_wellness_day(TODAY, raw, TODAY, settings(tmp_path), i, metrics, apply=True,
                        night={"restingHR": 49}, store=st)
    assert i.records[TODAY]["restingHR"] == 49                 # the night's, put back over the snapshot
    assert st.get_meta(f"wellness:provisional:{TODAY}") is None
    st.close()


def test_a_morning_archive_written_after_midnight_keeps_the_mark_for_the_final_read(tmp_path):
    st, g, i = _morning_written(tmp_path)
    from garmin_intervals_bridge.sync import _write_wellness_day
    metrics = {"days_locked": 0, "days_with_changes": 0, "writes": 0, "new_fields": []}
    morning = st.load_snapshot(TODAY)
    _write_wellness_day(TODAY, morning, TOMORROW, settings(tmp_path), i, metrics, apply=True, store=st)
    assert st.get_meta(f"wellness:provisional:{TODAY}")      # not final: nothing settled, nothing dropped
    _days_resting_hr(g, 47)
    _final_read(tmp_path, st, g, i)
    assert i.records[TODAY]["restingHR"] == 47
    st.close()


def test_personal_alone_brings_todays_resting_hr_from_the_daily_summary(tmp_path):
    st, g, i = fresh(tmp_path)
    g.source = "personal"
    g.sleep = g.readiness = True
    original = g.snapshot

    def snapshot(day, endpoints=None):
        raw = original(day, endpoints)
        raw["data"].get("sleep", {}).pop("restingHeartRate", None)     # the Personal sleep has none
        if "stats" in raw["data"]:
            raw["data"]["stats"]["restingHeartRate"] = 51
        return raw
    g.snapshot = snapshot
    i.records[TODAY] = {"sleepSecs": 28080}
    run(tmp_path, st, g, i, at(8, 0))
    assert "stats" in g.reads[0][1] and i.records[TODAY]["restingHR"] == 51
    st.close()


def test_an_explicit_read_before_the_final_read_does_not_settle_the_mornings_value(tmp_path):
    st, g, i = _morning_written(tmp_path)
    _days_resting_hr(g, 47)
    from garmin_intervals_bridge.sync import sync_wellness
    explicit = settings(tmp_path)
    explicit.wellness_refresh_hours = 6
    sync_wellness(explicit, g, i, st, apply=True, force=True, today=TOMORROW,
                  now=at(0, 30, day=TOMORROW))                    # sync --force of yesterday, just after midnight
    assert i.records[TODAY]["restingHR"] == 49 and st.get_meta(f"wellness:provisional:{TODAY}")
    _final_read(tmp_path, st, g, i)
    assert i.records[TODAY]["restingHR"] == 47
    st.close()


def test_the_scheduled_check_forgets_a_value_someone_else_set(tmp_path):
    st, g, i = _morning_written(tmp_path)
    i.records[TODAY]["restingHR"] = 52                         # the official import, or typed in
    run(tmp_path, st, g, i, at(12, 0))                         # the check reads today's record anyway
    assert st.get_meta(f"wellness:provisional:{TODAY}") is None
    i.records[TODAY]["restingHR"] = 49                         # and back to the bridge's number
    _days_resting_hr(g, 47)
    _final_read(tmp_path, st, g, i)
    assert i.records[TODAY]["restingHR"] == 49
    st.close()


def test_a_failed_write_keeps_what_the_bridge_saw(tmp_path):
    st, g, i = _morning_written(tmp_path)
    i.records[TODAY]["restingHR"] = 52
    from garmin_intervals_bridge.sync import _write_wellness_day

    def broken(day, changes):
        raise RuntimeError("Intervals is down")
    i.write_wellness = broken
    raw = {**st.load_snapshot(TODAY), "data": {**st.load_snapshot(TODAY)["data"],
                                               "hrv": {"hrvSummary": {"lastNightAvg": 38, "weeklyAvg": 40}}}}
    i.records[TODAY].pop("GarminHRV7DayAvg", None)             # something to write
    metrics = {"days_locked": 0, "days_with_changes": 0, "writes": 0, "new_fields": []}
    with __import__("pytest").raises(RuntimeError):
        _write_wellness_day(TODAY, raw, TODAY, settings(tmp_path), i, metrics, apply=True, store=st)
    assert st.get_meta(f"wellness:provisional:{TODAY}") is None
    st.close()


def test_a_final_read_whose_daily_summary_failed_settles_nothing(tmp_path):
    st, g, i = _morning_written(tmp_path)
    original = g.snapshot

    def snapshot(day, endpoints=None):
        raw = original(day, endpoints)
        if day == TODAY and endpoints is None:                  # the final read: the summary refused
            raw["data"].pop("stats", None)
            raw["errors"]["stats"] = "HTTPError 403"
        return raw
    g.snapshot = snapshot
    st.save_snapshot(TODAY, {**st.load_snapshot(TODAY), "data": {**st.load_snapshot(TODAY)["data"],
                                                                 "stats": {"restingHeartRate": 49}}})
    _final_read(tmp_path, st, g, i)
    assert i.records[TODAY]["restingHR"] == 49 and st.get_meta(f"wellness:provisional:{TODAY}")
    g.snapshot = original
    _days_resting_hr(g, 47)
    from garmin_intervals_bridge.sync import sync_wellness
    explicit = settings(tmp_path)
    explicit.wellness_refresh_hours = 6
    sync_wellness(explicit, g, i, st, apply=True, force=True, today=TOMORROW, now=at(9, 0, day=TOMORROW))
    assert i.records[TODAY]["restingHR"] == 47
    st.close()


def test_an_empty_field_in_a_check_does_not_end_the_bridges_claim(tmp_path):
    st, g, i = _morning_written(tmp_path)
    from garmin_intervals_bridge.sync import forget_foreign_provisional
    forget_foreign_provisional(st, TODAY, {"id": TODAY.isoformat()})    # Intervals answered without the value
    assert st.get_meta(f"wellness:provisional:{TODAY}")
    _days_resting_hr(g, 47)
    _final_read(tmp_path, st, g, i)
    assert i.records[TODAY]["restingHR"] == 47
    st.close()


def test_a_value_written_again_into_a_cleared_field_is_the_bridges(tmp_path):
    st, g, i = _morning_written(tmp_path)
    i.records[TODAY]["restingHR"] = 52                         # someone else's
    from garmin_intervals_bridge.sync import _write_wellness_day
    metrics = {"days_locked": 0, "days_with_changes": 0, "writes": 0, "new_fields": []}
    _write_wellness_day(TODAY, st.load_snapshot(TODAY), TODAY, settings(tmp_path), i, metrics, apply=True, store=st)
    assert st.get_meta(f"wellness:provisional:{TODAY}") is None
    del i.records[TODAY]["restingHR"]                          # cleared: the next write fills it again
    _write_wellness_day(TODAY, st.load_snapshot(TODAY), TODAY, settings(tmp_path), i, metrics, apply=True, store=st)
    assert i.records[TODAY]["restingHR"] == 49 and st.get_meta(f"wellness:provisional:{TODAY}")
    _days_resting_hr(g, 47)
    _final_read(tmp_path, st, g, i)
    assert i.records[TODAY]["restingHR"] == 47
    st.close()


# ---- the energy balance follows the intake Intervals holds ----

def test_kcal_balance_writes_what_differs_and_leaves_the_rest():
    from garmin_intervals_bridge.sync import kcal_balance
    i = Intervals()
    days = [TODAY - timedelta(days=n) for n in range(1, 6)]
    i.records = {days[0]: {"kcalConsumed": 4000, "GarminTotalCalories": 2650},                   # typed in, no balance
                 days[1]: {"kcalConsumed": 2500, "GarminTotalCalories": 2600, "GarminKcalBalance": -100},   # right
                 days[2]: {"kcalConsumed": 3000, "GarminTotalCalories": 2600, "GarminKcalBalance": -100},   # corrected
                 days[3]: {"GarminTotalCalories": 2600},                                         # nothing logged
                 days[4]: {"kcalConsumed": 3000, "GarminTotalCalories": 2600, "locked": True}}
    dry = kcal_balance(i, days[-1], days[0], apply=False)
    assert dry["would write"] == {days[0].isoformat(): 1350, days[2].isoformat(): 400} and i.writes == []
    assert (dry["same"], dry["without"], dry["locked"]) == (1, 1, 1)
    done = kcal_balance(i, days[-1], days[0], apply=True)
    assert done["written"] == dry["would write"]
    assert i.records[days[0]]["GarminKcalBalance"] == 1350 and i.records[days[2]]["GarminKcalBalance"] == 400
    assert "GarminKcalBalance" not in i.records[days[4]]


def test_the_scheduled_check_does_it_once_a_day_after_yesterdays_final_read(tmp_path):
    st, g, i = fresh(tmp_path)
    st.mark_wellness(YESTERDAY, written=True)                                       # its final values are in
    i.records[YESTERDAY] = {"kcalConsumed": 4000, "GarminTotalCalories": 2650}       # typed in after the final read
    run(tmp_path, st, g, i, at(1))
    assert i.records[YESTERDAY]["GarminKcalBalance"] == 1350
    assert i.ranges == [(TODAY - timedelta(days=7), YESTERDAY)]
    run(tmp_path, st, g, i, at(3))
    assert len(i.ranges) == 1                                   # once a day
    st.close()


def test_the_balance_check_follows_a_final_read_in_the_same_run(tmp_path):
    st, g, i = fresh(tmp_path)
    st.delete_meta(f"wellness:final:{YESTERDAY}", 2e9)                    # yesterday not read finally yet
    i.records[YESTERDAY] = {"kcalConsumed": 4000}
    g.last_sync = f"{TODAY}T05:00:00"                                    # the watch synced after midnight
    original = g.snapshot

    def snapshot(day, endpoints=None):
        raw = original(day, endpoints)
        if "stats" in raw["data"] and day == YESTERDAY:
            raw["data"]["stats"]["totalKilocalories"] = 2650
        return raw
    g.snapshot = snapshot
    i.records[TODAY] = {"steps": 100}                                    # the official import: touched
    m = run(tmp_path, st, g, i, at(8, 0))
    assert m["final"] == 1 and i.records[YESTERDAY]["GarminKcalBalance"] == 1350
    st.close()


def test_a_failed_final_write_keeps_the_balance_check_waiting(tmp_path):
    st, g, i = fresh(tmp_path)
    st.mark_wellness(YESTERDAY)                                          # read again, its write still failing
    i.records[YESTERDAY] = {"kcalConsumed": 4000, "GarminTotalCalories": 2650}
    i.write_wellness = lambda day, changes: (_ for _ in ()).throw(RuntimeError("Intervals is down"))
    run(tmp_path, st, g, i, at(8, 0))
    assert i.ranges == [] and not st.get_meta(f"wellness:balance:{TODAY}")
    st.close()
