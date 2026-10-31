"""Fluid and food logged for an activity and the sweat it cost, from Garmin's summary or the file into Intervals."""
import json
import os
from datetime import date, datetime, timezone

from garmin_intervals_bridge import sync
from garmin_intervals_bridge.intake import ensure_fields, field_item, fit_intake_values, intake_values, write_intake
from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import backfill_intake, complete_intake, restore_intake

SUMMARY = {"activityId": 42, "summaryDTO": {"startTimeLocal": "2026-09-20T13:56:00.0", "waterConsumed": 2250.0,
                                            "caloriesConsumed": 1099.99, "waterEstimated": 2108.0,
                                            "duration": 7200.0}}
EXPECTED = {"GarminFluidIntake": 2250, "GarminCaloriesConsumed": 1100, "GarminSweatLoss": 2108,
            "GarminFluidBalance": 142, "GarminSweatRate": 1054}


class Intervals:
    def __init__(self, activity=None, listing=None):
        self.record = dict(activity or {})
        self.listing = listing or []
        self.items = []
        self.updates = []

    def custom_items(self):
        return list(self.items)

    def create_custom_item(self, item):
        self.items.append(item)
        return {"id": len(self.items)}

    def activity(self, iid):
        return dict(self.record)

    def update_activity(self, iid, fields):
        self.updates.append((iid, fields))
        self.record.update(fields)

    def activities(self, start, end, fields=None, limit=None):
        return self.listing


def test_values_come_from_the_summary_and_nothing_logged_stays_out():
    assert intake_values(SUMMARY) == EXPECTED
    assert intake_values({"summaryDTO": {"waterConsumed": 0, "caloriesConsumed": None}}) == {}
    # nothing drunk logged: no balance; five minutes: no rate per hour
    assert intake_values({"summaryDTO": {"waterEstimated": 300.0, "duration": 300.0}}) == {"GarminSweatLoss": 300}


def test_the_device_entries_come_from_the_files_session_fields():
    ride = {"session": [{177: 400, 178: 1207, 179: 562, "total_timer_time": 9432.0}]}
    assert fit_intake_values(ride) == {"GarminFluidIntake": 562, "GarminCaloriesConsumed": 400, "GarminSweatLoss": 1207,
                                       "GarminFluidBalance": -645, "GarminSweatRate": 461}
    # multisport: the sweat runs on from session to session, the timer adds up; 0xFFFF is no value
    multi = {"session": [{178: 687, 179: 65535, "total_timer_time": 4201.8},
                         {178: 3888, "total_timer_time": 14972.8}, {178: 4169, "total_timer_time": 1282.2}]}
    assert fit_intake_values(multi) == {"GarminSweatLoss": 4169, "GarminSweatRate": 734}
    assert fit_intake_values({}) == {}


def test_fields_are_created_once_and_only_empty_ones_are_filled():
    i = Intervals({"GarminCaloriesConsumed": 800})
    rest = {k: v for k, v in EXPECTED.items() if k != "GarminCaloriesConsumed"}
    assert write_intake("i1", intake_values(SUMMARY), i, apply=False) == rest
    assert i.updates == [] and i.items == []                            # a dry run writes nothing
    assert write_intake("i1", intake_values(SUMMARY), i, apply=True) == rest
    assert i.updates == [("i1", rest)]
    assert [it["content"]["code"] for it in i.items] == sorted(rest)
    content = field_item("GarminFluidIntake")["content"]
    assert content["units"] == "ml" and content["aggregate"] == "SUM" and content["fit_session_field"] is None
    assert field_item("GarminSweatRate")["content"]["aggregate"] == "AVERAGE"
    assert ensure_fields(i, {"GarminFluidIntake"}) == []                # exists now: not created twice


def test_a_sweat_field_of_ones_own_reading_the_file_gets_no_twin():
    i = Intervals()
    i.items = [{"type": "ACTIVITY_FIELD", "content": {"code": "Sweatloss", "fit_session_field": "178"}}]
    written = write_intake("i1", intake_values(SUMMARY), i, apply=True)
    assert "GarminSweatLoss" not in written and written["GarminFluidBalance"] == 142
    assert "GarminSweatLoss" not in {it["content"]["code"] for it in i.items}


class Garmin:
    def __init__(self):
        self.asked = 0

    def activity(self, gid):
        self.asked += 1
        return SUMMARY


def test_the_summary_is_asked_again_once_half_a_day_later(tmp_path):
    st, g, i = Store(tmp_path), Garmin(), Intervals()
    activity = {"activityId": 42, "startTimeGMT": "2026-09-20 11:56:00"}
    st.save_activity_json("42", {"summaryDTO": {}})                     # archived right after the ride
    os.utime(st.activity_json_path("42"), (1, 1))
    metrics = {}
    complete_intake("42", activity, "i1", g, i, st, metrics, apply=True,
                    now=datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc))
    assert g.asked == 0 and i.updates == []                             # too early: the drink may come
    complete_intake("42", activity, "i1", g, i, st, metrics, apply=True,
                    now=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc))
    assert g.asked == 1 and i.updates == [("i1", EXPECTED)]
    assert json.loads(st.activity_json_path("42").read_text())["summaryDTO"]["waterConsumed"] == 2250
    complete_intake("42", activity, "i1", g, i, st, metrics, apply=True,
                    now=datetime(2026, 9, 22, 7, 0, tzinfo=timezone.utc))
    assert g.asked == 1 and metrics["intake_written"] == 1             # once per activity
    st.close()


def test_the_past_is_filled_from_the_archive_by_garmins_activity_id(tmp_path):
    st = Store(tmp_path)
    st.save_activity_json("42", SUMMARY)
    st.save_activity_json("43", {"summaryDTO": {"startTimeLocal": "2026-09-21T08:00:00.0", "waterConsumed": 500}})
    listing = [{"id": "i42", "external_id": "42", "source": "GARMIN_CONNECT"},
               {"id": "s43", "external_id": "43", "source": "STRAVA"}]     # a bare number from Strava is not ours
    i = Intervals(listing=listing)
    dry = backfill_intake(i, st, start=date(2026, 9, 1), end=date(2026, 9, 30), apply=False)
    assert dry == {"with_intake": 2, "written": 1, "already": 0, "unmatched": 1} and i.updates == []
    out = backfill_intake(i, st, start=date(2026, 9, 1), end=date(2026, 9, 30), apply=True)
    assert out["written"] == 1 and i.updates == [("i42", EXPECTED)]
    st.close()


def _file_on_disk(st, gid, monkeypatch, session):
    st.fit_path(gid).parent.mkdir(parents=True, exist_ok=True)
    st.fit_path(gid).write_bytes(b"original")
    monkeypatch.setattr(sync, "decode_fit", lambda data, checked=False: ({"session": [session]}, []))


def test_what_the_device_asked_for_is_written_at_once_and_the_app_fills_the_rest_later(tmp_path, monkeypatch):
    st, g, i = Store(tmp_path), Garmin(), Intervals()
    _file_on_disk(st, "42", monkeypatch, {178: 2108, "total_timer_time": 7200.0})   # a watch: no food asked
    activity = {"activityId": 42, "startTimeGMT": "2026-09-20 11:56:00"}
    early = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
    complete_intake("42", activity, "i1", g, i, st, {}, apply=True, now=early)
    complete_intake("42", activity, "i1", g, i, st, {}, apply=True, now=early)
    assert g.asked == 0 and i.updates == [("i1", {"GarminSweatLoss": 2108, "GarminSweatRate": 1054})]   # once
    complete_intake("42", activity, "i1", g, i, st, {}, apply=True, now=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc))
    assert g.asked == 1 and i.updates[1] == ("i1", {"GarminFluidIntake": 2250, "GarminCaloriesConsumed": 1100,
                                                    "GarminFluidBalance": 142})   # logged in the app later
    st.close()


def test_a_reprocessed_activity_gets_the_values_back_from_the_file_before_the_summary_is_archived(tmp_path, monkeypatch):
    st, i = Store(tmp_path), Intervals()
    _file_on_disk(st, "42", monkeypatch, {177: 400, 179: 562, 178: 1207, "total_timer_time": 9432.0})
    assert restore_intake("42", "i1", i, st, apply=True)["GarminFluidBalance"] == -645
    st.save_activity_json("42", SUMMARY)                                 # archived: the summary wins
    assert restore_intake("42", "i2", Intervals(), st, apply=False) == EXPECTED
    st.close()


class GarminWithGear(Garmin):
    def __init__(self, gear):
        super().__init__()
        self.gear, self.gear_asked = gear, 0

    def activity_extras(self, activity, keys=None):
        assert keys == ("gear",)
        self.gear_asked += 1
        return {"gear": self.gear, "errors": {}}


def test_gear_picked_in_the_app_after_the_run_is_read_again_half_a_day_later(tmp_path):
    shoes = [{"uuid": "u1", "displayName": "Pegasus"}]
    st, g, i = Store(tmp_path), GarminWithGear(shoes), Intervals()
    activity = {"activityId": 42, "startTimeGMT": "2026-09-20 11:56:00"}
    st.save_activity_json("42", {"summaryDTO": {}})
    st.save_activity_extras("42", {"gear": [], "weather": {"temp": 12}, "errors": {}})   # archived before the pick
    for path in (st.activity_json_path("42"), st.activity_extras_path("42")):
        os.utime(path, (1, 1))
    complete_intake("42", activity, "i1", g, i, st, {}, apply=True, now=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc))
    extras = json.loads(st.activity_extras_path("42").read_text())
    assert g.gear_asked == 1 and extras["gear"] == shoes and extras["weather"] == {"temp": 12}
    st.close()


def test_gear_archived_late_enough_is_not_asked_again(tmp_path):
    st, g, i = Store(tmp_path), GarminWithGear([]), Intervals()
    activity = {"activityId": 42, "startTimeGMT": "2026-09-20 11:56:00"}
    st.save_activity_json("42", {"summaryDTO": {}})
    st.save_activity_extras("42", {"gear": [], "errors": {}})            # read today: already after the pick
    complete_intake("42", activity, "i1", g, i, st, {}, apply=True, now=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc))
    assert g.gear_asked == 0
    st.close()


def test_an_unreadable_file_does_not_hold_up_the_summary(tmp_path, monkeypatch):
    st, g, i = Store(tmp_path), Garmin(), Intervals()
    st.fit_path("42").parent.mkdir(parents=True, exist_ok=True)
    st.fit_path("42").write_bytes(b"broken")

    def fails(data, checked=False):
        raise IndexError("truncated")
    monkeypatch.setattr(sync, "decode_fit", fails)
    activity = {"activityId": 42, "startTimeGMT": "2026-09-20 11:56:00"}
    complete_intake("42", activity, "i1", g, i, st, {}, apply=True, now=datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc))
    assert g.asked == 1 and i.updates == [("i1", EXPECTED)]
    st.close()


def test_the_first_garmin_import_is_found_by_start_and_duration(tmp_path):
    # Intervals' first Garmin import names Garmin's file, not the activity: start and duration decide
    st = Store(tmp_path)
    st.save_activity_json("44", {"summaryDTO": {"startTimeLocal": "2020-01-01T11:03:57.0",
                                                "startTimeGMT": "2020-01-01T10:03:57.0", "duration": 3600.0,
                                                "waterEstimated": 900.0}})
    old = {"id": "i44", "external_id": "hellkeeper_46500740483.fit", "source": "GARMIN_CONNECT",
           "start_date": "2020-01-01T10:04:01Z", "moving_time": 3550, "elapsed_time": 3700}
    i = Intervals(listing=[old])
    out = backfill_intake(i, st, start=date(2020, 1, 1), end=date(2020, 1, 31), apply=True)
    assert out["written"] == 1 and i.updates == [("i44", {"GarminSweatLoss": 900, "GarminSweatRate": 900})]
    # a watch and an Edge on the same ride agree both: no guess
    twin = dict(old, id="i45", external_id="hellkeeper_46500740484.fit")
    i = Intervals(listing=[old, twin])
    st.set_meta("intake:44", "")
    assert backfill_intake(i, st, start=date(2020, 1, 1), end=date(2020, 1, 31), apply=False)["unmatched"] == 1
    st.close()


def test_the_backfill_pauses_after_each_activity_it_reads(tmp_path, monkeypatch):
    st, slept = Store(tmp_path), []
    monkeypatch.setattr(sync.time, "sleep", slept.append)
    st.save_activity_json("42", SUMMARY)
    i = Intervals(listing=[{"id": "i42", "external_id": "42", "source": "GARMIN_CONNECT"}])
    backfill_intake(i, st, start=date(2026, 9, 1), end=date(2026, 9, 30), apply=True, pause_seconds=2.0)
    assert slept == [2.0]
    st.close()
