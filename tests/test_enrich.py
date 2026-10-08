from garmin_intervals_bridge.enrich import gap_report, match_activity
from test_fit import build_fit

GARMIN = {"activityId": 42, "startTimeGMT": "2026-10-07 08:00:00", "duration": 3600.0}


def test_match_by_external_id_wins_regardless_of_source():
    remote = [{"id": "i1", "external_id": "garmin:42", "source": "STRAVA"}]
    assert match_activity(GARMIN, remote)["id"] == "i1"
    assert match_activity(GARMIN, [{"id": "i1", "external_id": "42"}])["id"] == "i1"


def test_match_recognises_manual_upload_file_names():
    remote = [{"id": "i9", "source": "UPLOAD", "external_id": "42_ACTIVITY.fit",
               "start_date": "2026-10-07T08:00:00Z"}]
    assert match_activity(GARMIN, remote)["id"] == "i9"
    # A different activity whose ID merely starts with the same digits must not match.
    assert match_activity({**GARMIN, "activityId": 4}, remote) is None


def test_match_requires_garmin_source_start_and_duration():
    ok = {"id": "i2", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:01:30Z", "elapsed_time": 3590}
    wrong_source = {**ok, "id": "i3", "source": "STRAVA"}
    too_late = {**ok, "id": "i4", "start_date": "2026-10-07T08:05:00Z"}
    too_long = {**ok, "id": "i5", "elapsed_time": 5400}
    assert match_activity(GARMIN, [wrong_source, too_late, too_long]) is None
    assert match_activity(GARMIN, [wrong_source, ok])["id"] == "i2"


def test_match_fails_closed_without_usable_timestamps():
    assert match_activity({"activityId": 42}, [{"id": "i1", "source": "GARMIN_CONNECT"}]) is None
    naive = {"id": "i1", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:00:00"}
    assert match_activity(GARMIN, [naive]) is None


def test_short_activities_use_the_absolute_duration_floor():
    garmin = {**GARMIN, "duration": 300.0}
    remote = [{"id": "i6", "source": "GARMIN_CONNECT", "start_date": "2026-10-07T08:00:00Z", "elapsed_time": 345}]
    assert match_activity(garmin, remote)["id"] == "i6"


def test_gap_report_lists_session_scalars_and_whole_messages_only_in_original():
    original = build_fit(records=3, training_effect=3.2)
    partner = build_fit(records=3, training_effect=None)
    report = gap_report(original, partner)
    assert report["comparable"] is True and report["same_bytes"] is False
    assert report["scalar_candidates"] == {"total_anaerobic_training_effect": 1.1,
                                           "total_training_effect": 3.2}
    assert report["stream_candidates"] == []
    assert report["messages_only_in_original"] == {}
    assert report["unknown_ids_needing_names"] == {}


def test_gap_report_without_partner_is_an_inventory_not_a_gap():
    report = gap_report(build_fit(records=2), None)
    assert report["comparable"] is False and report["partner"] is None
    assert "total_training_effect" in report["session_fields"]
    assert "stream_candidates" not in report


def test_gap_report_counts_message_differences():
    report = gap_report(build_fit(records=5), build_fit(records=2))
    assert report["message_count_differences"]["record"] == {"original": 5, "partner": 2}


# ---- writing the gap back ----
from garmin_intervals_bridge.enrich import (align_stream, load_field_mappings, plan_scalars,
                                            plan_streams)

ITEMS = [
    {"type": "ACTIVITY_FIELD", "content": {"code": "AerobicEffect", "fit_session_field": "total_training_effect"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "Sweatloss", "fit_session_field": "178"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "RecoveryTime", "fit_session_field": "140.9",
                                           "script": "activity.isNew ? activity.RecoveryTime / 60 : activity.RecoveryTime"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "VO2MaxGarmin", "fit_session_field": "140.7",
                                           "script": "activity.isNew ? activity.VO2MaxGarmin * 3.5 / 65536 : activity.VO2MaxGarmin"}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "ActiveCalories", "fit_session_field": None,
                                           "script": "total = activity.calories; ..."}},
    {"type": "ACTIVITY_FIELD", "content": {"code": "Weird", "fit_session_field": "181",
                                           "script": "activity.isNew ? someOtherThing(activity.Weird) : activity.Weird"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "Stamina", "script": "{\n for (let m of icu.fit.record) {\n let f = m.f_138\n if (f) data.setAt(m.timestamp.value, f.value)\n }\n}"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "GarminGCT", "fit_record_field": "stance_time"}},
    {"type": "ACTIVITY_STREAM", "content": {"code": "Battery", "script": "for (let m of icu.fit) { if (m._num !== 104) continue; ... }"}},
    {"type": "INPUT_FIELD", "content": {"code": "BodyBatteryMax", "type": "numeric"}},
]


def test_mappings_come_from_the_athletes_own_definitions():
    m = load_field_mappings(ITEMS)
    by_code = {x.code: x for x in m.scalars}
    assert by_code["AerobicEffect"].source == "session:total_training_effect" and by_code["AerobicEffect"].convert == ()
    assert by_code["Sweatloss"].source == "session:178"
    assert by_code["RecoveryTime"].source == "mesg:140.9" and by_code["RecoveryTime"].convert == (("/", 60.0),)
    assert by_code["VO2MaxGarmin"].convert == (("*", 3.5), ("/", 65536.0))
    assert "ActiveCalories" not in by_code            # computed by Intervals, not a FIT field
    assert m.unsupported == {"Weird": "script with unknown semantics",
                             "Battery": "stream script not limited to a single record field"}
    streams = {x.code: x.record_field for x in m.streams}
    assert streams == {"Stamina": 138, "GarminGCT": "stance_time"}


def test_scalar_plan_converts_and_never_overwrites():
    messages = {"session": [{"total_training_effect": 3.2, 178: 1240}],
                "140": [{9: 2160, 7: 936228}]}
    m = load_field_mappings(ITEMS)
    plan = plan_scalars(messages, {"AerobicEffect": 2.9, "Sweatloss": None}, m)
    assert plan["writes"] == {"Sweatloss": 1240.0, "RecoveryTime": 36.0,
                              "VO2MaxGarmin": round(936228 * 3.5 / 65536, 4)}
    assert plan["kept_existing"] == {"AerobicEffect": 2.9}
    assert plan["absent_in_original"] == []


def test_stream_alignment_is_by_timestamp_not_index():
    records = [{"timestamp": 1000, 138: 90}, {"timestamp": 1001, 138: 89},
               {"timestamp": 1003, 138: 87}]              # second 1002 missing on the device
    data, stats = align_stream(records, [0, 1, 2, 3], 138)
    assert data == [90, 89, None, 87]
    assert stats == {"matched": 3, "points": 4, "non_null": 3}


def test_stream_plan_skips_existing_absent_and_misaligned():
    m = load_field_mappings(ITEMS)
    records = [{"timestamp": 1000 + i, 138: 90 - i, "stance_time": 250.0} for i in range(10)]
    messages = {"record": records}
    activity = {"stream_types": ["time", "GarminGCT"]}
    plan = plan_streams(messages, activity, list(range(10)), m)
    assert [w["type"] for w in plan["writes"]] == ["Stamina"]
    assert plan["writes"][0]["custom"] is True and plan["writes"][0]["data"][0] == 90
    assert plan["skipped"] == {"GarminGCT": "already on activity"}
    # Time stream that does not line up with the records at all -> refuse
    bad = plan_streams(messages, {"stream_types": []}, [5000 + i for i in range(10)], m)
    assert "alignment too poor" in bad["skipped"]["Stamina"]
    # Original without stamina -> nothing to write
    none = plan_streams({"record": [{"timestamp": 1000, "stance_time": 1.0}]}, {"stream_types": []}, [0], m)
    assert none["skipped"]["Stamina"] == "not in original"
