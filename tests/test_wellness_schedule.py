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
        self.sleep = False
        self.readiness = False
        self.last_sync = None            # naive GMT string, as Garmin sends it

    def snapshot(self, day, endpoints=None):
        self.reads.append((day, endpoints))
        names = endpoints or ("stats", "sleep", "hrv", "training_readiness", "body_composition")
        data = {}
        for name in names:
            if name == "sleep":
                data["sleep"] = {"dailySleepDTO": {"sleepTimeSeconds": 28080, "averageSpO2Value": 95,
                                                   "averageRespirationValue": 14.5} if self.sleep or day < TODAY
                                 else {}}
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

    def wellness(self, day):
        self.gets.append(day)
        return dict(self.records.get(day, {"id": day.isoformat()}))

    def write_wellness(self, day, changes):
        self.writes.append((day, changes))
        self.records.setdefault(day, {}).update(changes)

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
    gets = len(i.gets)
    run(tmp_path, st, g, i, at(10, 30))
    run(tmp_path, st, g, i, at(23, 0))
    assert len(g.reads) == 1 and len(i.gets) == gets           # done: not even Intervals is asked again
    st.close()


def test_the_check_is_throttled_when_called_every_minute(tmp_path):
    st, g, i = fresh(tmp_path)
    for minute in range(0, 10):
        run(tmp_path, st, g, i, at(6, minute), throttle=True)
    assert len(i.gets) == 1 and g.reads == []
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
