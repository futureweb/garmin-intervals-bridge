import json
from datetime import date
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_enrich import ITEMS
from test_fit import build_fit

from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import sync_enrich


def settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), activity_days=3)


class GarminFake:
    def __init__(self):
        self.acts = [{"activityId": 42, "startTimeGMT": "2026-10-07 08:00:00", "duration": 3.0}]
        self.downloads, self.details, self.extras_calls = 0, 0, 0

    def activities(self, start, end, fields=None, limit=None):
        return self.acts

    def original_fit(self, gid):
        self.downloads += 1
        return build_fit(records=3, training_effect=3.2)       # the device original

    def activity(self, gid):
        self.details += 1
        return next(a for a in self.acts if str(a["activityId"]) == str(gid))

    def activity_extras(self, activity, keys=None):
        self.extras_calls += 1
        found = {"weather": {"temp": 12}, "gear": None, "exercise_sets": None, "typed_splits": {"splits": []},
                 "split_summaries": {"splitSummaries": []}}
        return {**{k: v for k, v in found.items() if keys is None or k in keys}, "errors": {}}


class IntervalsFake:
    """One officially imported activity whose partner file lacks the training effect."""

    def __init__(self):
        self.remote = [{"id": "i42", "source": "GARMIN_CONNECT", "external_id": "42",
                        "start_date": "2026-10-07T08:00:00Z", "elapsed_time": 3}]
        self.activity_obj = {"id": "i42", "stream_types": ["time"], "AerobicEffect": 0.0, "Sweatloss": None}
        self.updates, self.stream_puts = [], []

    def activities(self, start, end, fields=None, limit=None):
        return self.remote

    def activity(self, iid):
        return dict(self.activity_obj)

    def activity_file(self, iid):
        return build_fit(records=3, training_effect=None)      # filtered partner copy

    def custom_items(self):
        return ITEMS

    def streams(self, iid, types=None):
        return [{"type": "time", "data": [0, 1, 2]}]

    def update_activity(self, iid, fields):
        self.updates.append((iid, fields))
        self.activity_obj.update(fields)
        return {"id": iid}

    def put_streams(self, iid, body):
        self.stream_puts.append((iid, body))
        return {"updated": [b["type"] for b in body], "deleted": []}


def test_scheduled_enrich_dry_run_plans_but_writes_nothing(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    m = sync_enrich(settings(tmp_path), g, i, st, apply=False, today=date(2026, 10, 8))
    assert m["seen"] == 1 and m["planned"] == 1 and m["enriched"] == 0
    assert i.updates == [] and i.stream_puts == []
    assert st.enrichment("42") is None
    st.close()


def test_scheduled_enrich_writes_once_then_remembers(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["enriched"] == 1 and m["fields_written"] == 1
    # AerobicEffect was 0.0 from the filtered file and its source is absent there -> replaced
    assert i.updates == [("i42", {"AerobicEffect": 3.2})]
    assert st.enrichment("42")["fields"] == ["AerobicEffect"]
    again = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert again["already_enriched"] == 1 and len(i.updates) == 1 and g.downloads == 1
    st.close()


def test_scheduled_enrich_waits_for_the_official_import(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    i.remote = []                                              # not imported yet
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["unmatched"] == 1 and m["failed"] == 0 and i.updates == []
    assert st.failed_activities() == []                        # not an error, just not yet
    st.close()


def test_scheduled_enrich_completes_the_local_mirror(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["archived"] == 1 and g.details == 1 and g.extras_calls == 1
    assert st.activity_json_path("42").is_file() and st.activity_extras_path("42").is_file()
    again = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert again["archived"] == 0 and g.details == 1 and g.extras_calls == 1       # nothing is asked twice
    st.close()


def test_archive_waits_for_the_extras_of_a_fresh_activity(tmp_path):
    from datetime import datetime, timezone

    from garmin_intervals_bridge.sync import archive_activity
    st, g = Store(tmp_path), GarminFake()
    activity = g.acts[0]
    g.activity_extras = lambda a, keys=None: {"weather": None, "gear": None, "exercise_sets": None,
                                              "typed_splits": None, "split_summaries": None,
                                              "errors": {"weather": "HTTPError"}}
    just_after = datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc)
    assert archive_activity("42", activity, g, st, now=just_after) == "pending"
    assert st.activity_json_path("42").is_file() and not st.activity_extras_path("42").is_file()
    assert archive_activity("42", activity, g, st, now=just_after) == "pending"      # asked again next pass
    days_later = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    assert archive_activity("42", activity, g, st, now=days_later) == "archived"     # Garmin never filled it in
    assert st.activity_extras_path("42").is_file()
    assert archive_activity("42", activity, g, st, now=days_later) == "on_disk"
    st.close()


def test_scheduled_enrich_isolates_a_broken_activity(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    g.acts = [{"activityId": 41, "startTimeGMT": "2026-10-07 07:00:00"}] + g.acts
    original = g.original_fit
    g.original_fit = lambda gid: (_ for _ in ()).throw(ValueError("no file")) if str(gid) == "41" else original(gid)
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["failed"] == 1 and m["enriched"] == 1
    assert st.activity_status("41") == "failed"
    st.close()


def test_one_activity_again_after_intervals_reprocess_file(tmp_path):
    from garmin_intervals_bridge.enrich import enrich_activity, resolve_one
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    i.activity_obj["AerobicEffect"] = 0.0                     # "Reprocess file": the filtered copy's value again
    i.activity_obj["external_id"] = "42"                      # what the official sync stores
    gid, iid = resolve_one(i, "https://intervals.icu/activities/i42")
    assert (gid, iid) == ("42", "i42")
    assert enrich_activity(gid, g, i, st, apply=True, intervals_id=iid)["outcome"] == "already_enriched"
    plan = enrich_activity(gid, g, i, st, apply=True, intervals_id=iid, refresh_own=True, recheck=True)
    assert plan["outcome"] == "enriched" and i.updates[-1] == ("i42", {"AerobicEffect": 3.2})
    assert g.downloads == 1                                    # the archived original, not a new download
    st.close()


def test_enrich_puts_the_logged_intake_back_after_a_reprocess(tmp_path):
    from garmin_intervals_bridge.sync import restore_intake

    class Reprocessed(IntervalsFake):
        def __init__(self):
            super().__init__()
            self.created_items = []

        def create_custom_item(self, item):
            self.created_items.append(item)

    st, i = Store(tmp_path), Reprocessed()
    assert restore_intake("42", "i42", i, st, apply=True) == {}                 # nothing archived: nothing to do
    st.save_activity_json("42", {"summaryDTO": {"waterConsumed": 1500, "caloriesConsumed": 1200}})
    assert restore_intake("42", "i42", i, st, apply=False) == {"GarminFluidIntake": 1500,
                                                              "GarminCaloriesConsumed": 1200}
    assert i.updates == []                                                      # a dry run writes nothing
    restore_intake("42", "i42", i, st, apply=True)
    assert i.updates[-1] == ("i42", {"GarminFluidIntake": 1500, "GarminCaloriesConsumed": 1200})
    assert restore_intake("42", "i42", i, st, apply=True) == {}                 # there now: not again
    st.close()


def test_intervals_first_garmin_import_is_found_by_start_and_duration(tmp_path):
    # that import names Garmin's file, not the activity: the one look-alike whose start and duration agree
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    i.remote = [{"id": "i42", "source": "GARMIN_CONNECT", "external_id": "athlete_46500740483.fit",
                 "start_date": "2026-10-07T08:00:30Z", "elapsed_time": 3}]
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["enriched"] == 1 and i.updates == [("i42", {"AerobicEffect": 3.2})]
    st.close()


def test_two_look_alikes_from_the_first_import_are_no_match(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    one = {"id": "i42", "source": "GARMIN_CONNECT", "external_id": "athlete_1.fit",
           "start_date": "2026-10-07T08:00:30Z", "elapsed_time": 3}
    i.remote = [one, dict(one, id="i43", external_id="athlete_2.fit")]   # a watch and an Edge
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["unmatched"] == 1 and i.updates == []
    st.close()


def _gpx_zip(name="42_UNKNOWN.gpx"):
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "<gpx><trk/></gpx>")
    return buf.getvalue()


def test_an_original_that_is_a_gpx_is_named_as_such():
    import pytest

    from garmin_intervals_bridge.fit import InvalidFIT, NotFIT, extract_original_fit
    with pytest.raises(NotFIT) as caught:
        extract_original_fit(_gpx_zip())
    assert caught.value.name == "42_UNKNOWN.gpx" and caught.value.data == b"<gpx><trk/></gpx>"
    assert not isinstance(caught.value, InvalidFIT)                  # no FIT refused: nothing to give up on
    assert "GPX file, no FIT" in str(caught.value)


class GarminGpx(GarminFake):
    def original_fit(self, gid):
        from garmin_intervals_bridge.fit import extract_original_fit
        self.downloads += 1
        return extract_original_fit(_gpx_zip())


def test_a_gpx_upload_is_archived_and_skipped_for_good(tmp_path):
    # an activity uploaded to Garmin Connect as GPX (a GPS unit, BaseCamp): nothing to add, never retried
    st, g, i = Store(tmp_path), GarminGpx(), IntervalsFake()
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m.get("not_fit") == 1 and m["failed"] == 0 and i.updates == []
    assert st.activity_status("42") == "skipped" and st.failed_activities() == []
    assert st.fit_path("42").with_suffix(".gpx").read_bytes() == b"<gpx><trk/></gpx>"
    again = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert again["skipped"] == 1 and g.downloads == 1                 # not asked again
    st.close()


def test_a_failed_gpx_upload_outside_the_window_ends_its_retries(tmp_path):
    from garmin_intervals_bridge.sync import _retry_overdue
    st, g, i = Store(tmp_path), GarminGpx(), IntervalsFake()
    st.record_failure("42", "InvalidFIT: Expected exactly one FIT in archive, found 0")
    st.db.execute("UPDATE activity SET next_retry=0 WHERE garmin_id='42'")
    metrics = {"failed": 0}
    _retry_overdue(g, i, st, metrics, set(), None, apply=True)
    assert metrics == {"failed": 0, "not_fit": 1} and st.failed_activities() == []
    assert st.activity_status("42") == "skipped"
    st.close()


# --- Training Effect and load from Garmin's summary (an Edge 510 writes none into its file) ----------

TE_ITEMS = ITEMS + [
    {"type": "ACTIVITY_FIELD", "content": {"code": "AnaerobicEffect", "fit_session_field": "total_anaerobic_training_effect"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "TrainingLoad", "fit_session_field": "training_load_peak"}},
]
SUMMARY = {"summaryDTO": {"trainingEffect": 3.4000000953674316, "anaerobicTrainingEffect": 1.2999999523162842,
                          "activityTrainingLoad": 87.5, "startTimeGMT": "2026-10-07 08:00:00"}}


def edge510(tmp_path, summary=SUMMARY):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    g.original_fit = lambda gid: (setattr(g, "downloads", g.downloads + 1) or build_fit(records=3, training_effect=None))
    act = g.acts[0]
    g.activity = lambda gid: (setattr(g, "details", g.details + 1) or {**act, **(summary or {})})
    i.custom_items = lambda: TE_ITEMS
    i.activity_obj = {"id": "i42", "stream_types": ["time"], "AerobicEffect": None, "AnaerobicEffect": 0.0,
                      "TrainingLoad": None, "Sweatloss": None, "start_date": "2026-10-07T08:00:00Z"}
    return st, g, i


def test_a_device_without_training_effect_gets_it_from_garmins_summary(tmp_path):
    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY
    st, g, i = edge510(tmp_path)
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    # enriched before the summary was archived: the fields wait; the settled pass fills them
    assert i.updates == [("i42", {"AerobicEffect": 3.4, "AnaerobicEffect": 1.3, "TrainingLoad": 87.5})]
    assert m["fields_written"] == 3 and not st.get_meta(SUMMARY_PENDING_KEY + "42")
    again = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert len(i.updates) == 1 and again["fields_written"] == 0           # nothing twice
    st.close()


def test_the_fields_wait_until_the_activity_has_settled(tmp_path):
    from datetime import datetime, timezone

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import complete_summary_fields
    st, g, i = edge510(tmp_path)
    plan = enrich_activity("42", g, i, st, apply=True, intervals_id="i42")
    assert plan["fields"]["summary_pending"] == ["AerobicEffect", "AnaerobicEffect", "TrainingLoad"]
    assert st.get_meta(SUMMARY_PENDING_KEY + "42") and g.details == 0      # no summary yet, none asked for
    act, metrics = g.acts[0], {}
    complete_summary_fields("42", act, "i42", g, i, st, metrics, apply=True,
                            now=datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc))
    assert i.updates == [] and g.details == 0                               # four hours after: still waiting
    complete_summary_fields("42", act, "i42", g, i, st, metrics, apply=True,
                            now=datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))
    assert g.details == 1 and st.activity_json_path("42").is_file()        # asked once, archived
    assert i.updates == [("i42", {"AerobicEffect": 3.4, "AnaerobicEffect": 1.3, "TrainingLoad": 87.5})]
    assert metrics["fields_written"] == 3 and not st.get_meta(SUMMARY_PENDING_KEY + "42")
    complete_summary_fields("42", act, "i42", g, i, st, metrics, apply=True,
                            now=datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc))
    assert g.details == 1 and len(i.updates) == 1                          # done: not again
    st.close()


def test_a_summary_without_the_values_ends_the_wait(tmp_path):
    from datetime import datetime, timezone

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import complete_summary_fields
    st, g, i = edge510(tmp_path, summary={"summaryDTO": {"trainingEffectLabel": "UNKNOWN"}})
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42")
    complete_summary_fields("42", g.acts[0], "i42", g, i, st, {}, apply=True,
                            now=datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc))
    assert i.updates == [] and not st.get_meta(SUMMARY_PENDING_KEY + "42") and g.details == 1
    st.close()


def test_one_activity_on_request_asks_garmin_for_the_summary(tmp_path):
    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    st, g, i = edge510(tmp_path)
    dry = enrich_activity("42", g, i, st, apply=False, intervals_id="i42", recheck=True)
    assert dry["outcome"] == "planned" and dry["fields"]["from_garmin_summary"] == [
        "AerobicEffect", "AnaerobicEffect", "TrainingLoad"] and g.details == 1 and i.updates == []
    done = enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    assert done["outcome"] == "enriched" and g.details == 1                # the summary asked for is archived
    assert i.updates == [("i42", {"AerobicEffect": 3.4, "AnaerobicEffect": 1.3, "TrainingLoad": 87.5})]
    assert not st.get_meta(SUMMARY_PENDING_KEY + "42")
    st.close()


def test_a_device_with_training_effect_never_needs_the_summary(tmp_path):
    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    i.custom_items = lambda: TE_ITEMS[:-1]          # the fake device file carries both effects, no load
    plan = enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    assert g.details == 0 and plan["fields"]["from_garmin_summary"] == []
    assert plan["fields"]["writes"]["AerobicEffect"] == 3.2 and not st.get_meta(SUMMARY_PENDING_KEY + "42")
    st.close()


def test_the_summary_settles_when_the_intake_does():
    from garmin_intervals_bridge.enrich import SUMMARY_SETTLE_HOURS
    from garmin_intervals_bridge.sync import INTAKE_SETTLE_HOURS
    assert SUMMARY_SETTLE_HOURS == INTAKE_SETTLE_HOURS


def at(hours_after_start):
    from datetime import datetime, timedelta, timezone
    return datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc) + timedelta(hours=hours_after_start)


def test_a_failed_write_keeps_the_mark_for_the_next_run(tmp_path):
    import pytest

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import complete_summary_fields
    st, g, i = edge510(tmp_path)
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42")
    good = i.update_activity
    i.update_activity = lambda iid, fields: (_ for _ in ()).throw(RuntimeError("HTTP 502"))
    with pytest.raises(RuntimeError):
        complete_summary_fields("42", g.acts[0], "i42", g, i, st, {}, apply=True, now=at(13))
    assert st.get_meta(SUMMARY_PENDING_KEY + "42")                        # not lost
    i.update_activity = good
    assert complete_summary_fields("42", g.acts[0], "i42", g, i, st, {}, apply=True, now=at(14))
    assert i.updates == [("i42", {"AerobicEffect": 3.4, "AnaerobicEffect": 1.3, "TrainingLoad": 87.5})]
    assert st.get_meta(SUMMARY_PENDING_KEY + "42") is None
    st.close()


def test_one_activity_on_request_asks_again_when_the_archive_lacks_the_values(tmp_path):
    from garmin_intervals_bridge.enrich import enrich_activity
    st, g, i = edge510(tmp_path)
    st.save_activity_json("42", {**g.acts[0], "summaryDTO": {"startTimeGMT": "2026-10-07 08:00:00"}})  # settled, empty
    plan = enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    assert g.details == 1 and plan["fields"]["from_garmin_summary"] == ["AerobicEffect", "AnaerobicEffect",
                                                                        "TrainingLoad"]
    st.close()


def test_an_early_summary_fills_at_once_and_the_settled_one_corrects(tmp_path):
    from datetime import datetime, timedelta, timezone

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import complete_summary_fields
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    gmt = start.strftime("%Y-%m-%d %H:%M:%S")
    early = {"summaryDTO": {"trainingEffect": 3.0, "startTimeGMT": gmt}}
    st, g, i = edge510(tmp_path, summary=early)
    g.acts[0]["startTimeGMT"] = gmt
    i.activity_obj["start_date"] = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    plan = enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    assert i.updates == [("i42", {"AerobicEffect": 3.0})] and plan["fields"]["summary_pending"]
    assert st.get_meta(SUMMARY_PENDING_KEY + "42")                        # fetched an hour after the start
    later = {"summaryDTO": {"trainingEffect": 3.6, "anaerobicTrainingEffect": 1.0, "startTimeGMT": gmt}}
    g.activity = lambda gid: (setattr(g, "details", g.details + 1) or {**g.acts[0], **later})
    complete_summary_fields("42", g.acts[0], "i42", g, i, st, {}, apply=True, now=start + timedelta(hours=13))
    assert g.details == 2 and i.updates[-1] == ("i42", {"AerobicEffect": 3.6, "AnaerobicEffect": 1.0})
    assert st.get_meta(SUMMARY_PENDING_KEY + "42") is None
    st.close()


def test_marked_activities_outside_the_scan_window_are_completed_by_their_mark(tmp_path):
    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import _complete_marked_summaries
    st, g, i = edge510(tmp_path)
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42")
    metrics = {}
    _complete_marked_summaries(g, i, st, metrics, {"42"}, apply=True, now=at(13))
    assert i.updates == []                                                 # listed: the scan does it
    _complete_marked_summaries(g, i, st, metrics, set(), apply=True, now=at(13))
    assert len(i.updates) == 1 and metrics["fields_written"] == 3
    assert st.get_meta(SUMMARY_PENDING_KEY + "42") is None
    st.close()


def test_a_mark_too_old_is_given_up(tmp_path):
    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import _complete_marked_summaries
    st, g, i = edge510(tmp_path)
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42")
    _complete_marked_summaries(g, i, st, {}, set(), apply=True, now=at(15 * 24))
    assert i.updates == [] and g.details == 0 and st.get_meta(SUMMARY_PENDING_KEY + "42") is None
    st.close()


def test_the_start_intervals_knows_comes_before_the_summarys(tmp_path):
    from datetime import datetime, timedelta, timezone

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    now = datetime.now(timezone.utc).replace(microsecond=0)
    old_gmt = (now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M:%S")
    st, g, i = edge510(tmp_path, summary={"summaryDTO": {"trainingEffect": 3.0, "startTimeGMT": old_gmt}})
    i.activity_obj["start_date"] = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    mark = json.loads(st.get_meta(SUMMARY_PENDING_KEY + "42"))           # an hour old by Intervals: not final
    assert mark["start"].startswith((now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
    st.close()


def test_a_value_written_by_a_pass_that_failed_half_way_is_still_the_bridges(tmp_path):
    import pytest

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    st, g, i = edge510(tmp_path, summary={"summaryDTO": {"trainingEffect": 3.0}})
    i.activity_obj["start_date"] = None
    good = i.put_streams
    i.put_streams = lambda iid, body: (_ for _ in ()).throw(RuntimeError("HTTP 502"))
    plans = []
    orig_plan = __import__("garmin_intervals_bridge.enrich", fromlist=["plan_streams"]).plan_streams

    def with_stream(*args, **kw):                      # a stream to write, so the pass fails after the fields
        out = orig_plan(*args, **kw)
        out["writes"] = [{"type": "Stamina", "data": [1, 2, 3], "_stats": {}}]
        plans.append(out)
        return out
    import garmin_intervals_bridge.enrich as en
    en.plan_streams, saved = with_stream, en.plan_streams
    try:
        with pytest.raises(RuntimeError):
            enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
        assert i.updates == [("i42", {"AerobicEffect": 3.0})] and st.enrichment("42") is None
        i.put_streams = good
        g.activity = lambda gid: {**g.acts[0], "summaryDTO": {"trainingEffect": 3.6}}
        st.save_activity_json("42", g.activity("42"))
        plan = enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    finally:
        en.plan_streams = saved
    assert plan["fields"]["writes"] == {"AerobicEffect": 3.6}           # its own value: corrected
    assert st.get_meta(SUMMARY_PENDING_KEY + "42") is None
    st.close()


def test_a_swap_retargets_the_mark(tmp_path):
    from garmin_intervals_bridge.enrich import enrich_activity
    from garmin_intervals_bridge.sync import complete_summary_fields
    st, g, i = edge510(tmp_path)
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42")
    st.db.execute("DELETE FROM enrichment WHERE garmin_id='42'")          # an upload swap: a new activity
    st.record_activity("42", "uploaded", intervals_id="i99")
    asked = []
    real = i.activity
    i.activity = lambda iid: (asked.append(iid) or real(iid))
    complete_summary_fields("42", {}, None, g, i, st, {}, apply=True, now=at(13))
    assert asked and set(asked) == {"i99"} and i.updates[-1][0] == "i99"
    st.close()


def test_failing_marks_take_turns_and_old_ones_are_dropped_even_when_listed(tmp_path):
    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY
    from garmin_intervals_bridge.sync import _complete_marked_summaries
    st, g, i = edge510(tmp_path)
    for n in range(1, 7):
        st.set_meta(f"{SUMMARY_PENDING_KEY}{n}", json.dumps({"intervals_id": f"i{n}",
                                                            "start": "2026-10-07T08:00:00+00:00", "fields": ["X"]}))
    st.set_meta(f"{SUMMARY_PENDING_KEY}99", json.dumps({"intervals_id": "i99", "start": "2026-09-01T08:00:00+00:00"}))
    tried = []

    def complete(gid, *a, **kw):
        tried.append(gid)
        raise RuntimeError("HTTP 404")
    import garmin_intervals_bridge.sync as sync
    sync.complete_summary_fields, saved = complete, sync.complete_summary_fields
    try:
        _complete_marked_summaries(g, i, st, {}, {"99"}, apply=True, now=at(13))
        assert len(tried) == 5 and st.get_meta(f"{SUMMARY_PENDING_KEY}99") is None   # too old: gone though listed
        first = set(tried)
        tried.clear()
        _complete_marked_summaries(g, i, st, {}, set(), apply=True, now=at(14))
        assert tried == [(set("123456") - first).pop()]                    # the waiting one; the failed rest later
        tried.clear()
        _complete_marked_summaries(g, i, st, {}, set(), apply=True, now=at(20))
        assert len(tried) == 5
    finally:
        sync.complete_summary_fields = saved
    st.close()


def test_after_a_swap_the_old_activitys_values_are_not_the_bridges(tmp_path):
    from garmin_intervals_bridge.enrich import enrich_activity
    st, g, i = edge510(tmp_path, summary={"summaryDTO": {"trainingEffect": 3.0}})
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42", recheck=True)
    assert i.updates[-1] == ("i42", {"AerobicEffect": 3.0})                 # the bridge's 3.0 on the old activity
    g.activity = lambda gid: {**g.acts[0], "summaryDTO": {"trainingEffect": 3.6}}
    i.activity_obj = {**i.activity_obj, "id": "i99", "AerobicEffect": 3.0}     # a new activity, 3.0 typed in
    for _ in range(2):                                                     # the second pass knows i99 as "same"
        plan = enrich_activity("42", g, i, st, apply=True, intervals_id="i99", recheck=True)
        assert plan["fields"]["kept_existing"]["AerobicEffect"] == 3.0
        assert "AerobicEffect" not in plan["fields"]["writes"]
    st.close()


def test_an_early_part_of_the_archive_is_not_final_because_a_later_part_is(tmp_path):
    import os

    from garmin_intervals_bridge.enrich import SUMMARY_PENDING_KEY, enrich_activity
    from garmin_intervals_bridge.sync import complete_summary_fields
    st, g, i = edge510(tmp_path, summary={"summaryDTO": {"trainingEffect": 3.6, "activityTrainingLoad": 100.0}})
    enrich_activity("42", g, i, st, apply=True, intervals_id="i42")       # marked
    st.save_activity_json("42", {"summaryDTO": {"trainingEffect": 3.0, "activityTrainingLoad": 80.0}})
    personal = st.activity_json_path("42").with_suffix(".personal.json")
    personal.write_text('{"summaryDTO": {"anaerobicTrainingEffect": 1.3}}')
    os.utime(st.activity_json_path("42"), (at(1).timestamp(),) * 2)       # an hour after the start
    os.utime(personal, (at(13).timestamp(),) * 2)                           # thirteen hours after
    complete_summary_fields("42", g.acts[0], "i42", g, i, st, {}, apply=True, now=at(14))
    assert g.details == 1                                                  # asked again: not final
    assert i.updates[-1] == ("i42", {"AerobicEffect": 3.6, "TrainingLoad": 100.0})
    assert st.get_meta(SUMMARY_PENDING_KEY + "42") is None
    st.close()
