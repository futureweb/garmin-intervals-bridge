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
