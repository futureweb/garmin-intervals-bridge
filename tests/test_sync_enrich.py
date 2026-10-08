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
        self.downloads = 0

    def activities(self, start, end, fields=None, limit=None):
        return self.acts

    def original_fit(self, gid):
        self.downloads += 1
        return build_fit(records=3, training_effect=3.2)       # the device original


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


def test_scheduled_enrich_isolates_a_broken_activity(tmp_path):
    st, g, i = Store(tmp_path), GarminFake(), IntervalsFake()
    g.acts = [{"activityId": 41, "startTimeGMT": "2026-10-07 07:00:00"}] + g.acts
    original = g.original_fit
    g.original_fit = lambda gid: (_ for _ in ()).throw(ValueError("no file")) if str(gid) == "41" else original(gid)
    m = sync_enrich(settings(tmp_path), g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["failed"] == 1 and m["enriched"] == 1
    assert st.activity_status("41") == "failed"
    st.close()
