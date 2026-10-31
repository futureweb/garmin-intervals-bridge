"""The same FIT file refused three times: given up, named once, tried again after an update."""
import io
import zipfile
from datetime import date
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_fit import build_fit
from test_sync import FlakyGarmin as SyncGarmin
from test_sync import IntervalsFake, run
from test_sync_enrich import GarminFake
from test_sync_enrich import IntervalsFake as EnrichIntervals

from garmin_intervals_bridge import __version__, alerts
from garmin_intervals_bridge.fit import InvalidFIT, extract_original_fit, sha256
from garmin_intervals_bridge.health import GIVEN_UP, probe, silent_error_checks
from garmin_intervals_bridge.store import REFUSED_KEY, TOLD_KEY, VERSION_KEY, Store
from garmin_intervals_bridge.sync import sync_enrich


def broken_fit(flip: int = 20) -> bytes:
    data = bytearray(build_fit())
    data[flip] ^= 0xFF                       # the file CRC no longer matches; the length still does
    return bytes(data)


def zipped(data: bytes, year: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("activity.fit", date_time=(year, 1, 1, 0, 0, 0)), data)
    return out.getvalue()


def test_a_refused_file_carries_the_digest_of_the_fit_not_of_its_zip():
    digests = []
    for year in (2025, 2026):                # the same FIT in two ZIPs that differ
        with pytest.raises(InvalidFIT, match="CRC") as caught:
            extract_original_fit(zipped(broken_fit(), year))
        digests.append(caught.value.digest)
    assert digests[0] == digests[1] == sha256(broken_fit())
    # an empty answer, an error page, a ZIP without a FIT say nothing about the file: no digest
    # (a ZIP with a GPX or TCX instead is no failure at all: NotFIT, see test_sync_enrich)
    for download in (b"", b"<html>Service unavailable</html>", zipped(b"", 2026).replace(b".fit", b".txt")):
        with pytest.raises(InvalidFIT) as caught:
            extract_original_fit(download)
        assert caught.value.digest is None


class RefusingGarmin(SyncGarmin):
    """Garmin hands out a broken file for 42: the same one, or (`changing`) another one each time."""

    def __init__(self, changing=False):
        super().__init__()
        self.changing, self.calls = changing, 0

    def original_fit(self, activity_id):
        if str(activity_id) != "42":
            return super().original_fit(activity_id)
        self.requested.append("42")
        self.calls += 1
        return extract_original_fit(broken_fit(20 + self.calls if self.changing else 20))


def expire(st, gid="42"):
    st.db.execute("UPDATE activity SET next_retry = next_retry - 8 * 86400 WHERE garmin_id=?", (gid,))
    st.db.commit()


def test_the_same_file_refused_three_times_is_given_up_and_not_asked_for_again(tmp_path, caplog):
    st, g, i = Store(tmp_path), RefusingGarmin(), IntervalsFake()
    for _ in range(2):
        assert run(tmp_path, st, g, i)["failed"] == 1 and st.activity_status("42") == "failed"
        expire(st)
    caplog.clear()
    with caplog.at_level("WARNING", logger="garmin_intervals_bridge"):
        assert run(tmp_path, st, g, i)["failed"] == 1
    assert st.activity_status("42") == "invalid" and st.failed_activities() == []
    assert any("42 given up" in r.getMessage() for r in caplog.records)
    assert not any("42 failed" in r.getMessage() for r in caplog.records)     # no "retrying later"
    (given_up,) = st.invalid_activities()
    assert given_up["garmin_id"] == "42" and "CRC" in given_up["error"] and given_up["told"] is False
    expire(st)
    result = run(tmp_path, st, g, i)
    assert result["deferred"] == 1 and g.requested.count("42") == 3               # never asked again
    st.close()


def test_a_different_file_each_time_is_a_transfer_problem_and_keeps_its_retries(tmp_path):
    st, g, i = Store(tmp_path), RefusingGarmin(changing=True), IntervalsFake()
    for _ in range(4):
        run(tmp_path, st, g, i)
        expire(st)
    assert st.activity_status("42") == "failed" and st.invalid_activities() == []
    assert st.failed_activities()[0]["attempts"] == 4
    st.close()


def test_a_file_that_differs_every_time_keeps_the_usual_backoff(tmp_path):
    import time
    st = Store(tmp_path)
    delays = []
    for n in range(4):
        started = time.time()
        st.record_failure("7", "InvalidFIT: FIT CRC check failed", refused=f"digest{n}")
        (next_retry,) = st.db.execute("SELECT next_retry FROM activity WHERE garmin_id='7'").fetchone()
        delays.append(round((next_retry - started) / 60))
    assert delays == [10, 60, 360, 1440] and st.activity_status("7") == "failed"
    st.close()


def test_other_errors_never_give_up(tmp_path):
    st = Store(tmp_path)
    for _ in range(4):
        st.record_failure("7", "ValueError: no original file for this activity")
    assert st.activity_status("7") == "failed" and st.invalid_activities() == []
    st.close()


def test_a_refused_file_is_checked_again_soon_whatever_the_count_before(tmp_path):
    import time
    st = Store(tmp_path)
    for _ in range(5):
        st.record_failure("7", "ValueError: boom")                  # a week until the next try
    started = time.time()
    st.record_failure("7", "InvalidFIT: FIT CRC check failed", refused="abc")
    (next_retry,) = st.db.execute("SELECT next_retry FROM activity WHERE garmin_id='7'").fetchone()
    assert abs(next_retry - started - 600) < 60
    st.record_failure("7", "InvalidFIT: FIT CRC check failed", refused="abc")
    (next_retry,) = st.db.execute("SELECT next_retry FROM activity WHERE garmin_id='7'").fetchone()
    assert abs(next_retry - started - 3600) < 60
    st.close()


def test_scheduled_enrich_gives_up_the_same_refused_original(tmp_path):
    class Refusing(GarminFake):
        def original_fit(self, gid):
            self.downloads += 1
            return extract_original_fit(broken_fit())

    cfg = SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), activity_days=3)
    st, g, i = Store(tmp_path), Refusing(), EnrichIntervals()
    for _ in range(3):
        assert sync_enrich(cfg, g, i, st, apply=True, today=date(2026, 10, 8))["failed"] == 1
        expire(st)
    assert st.activity_status("42") == "invalid"
    m = sync_enrich(cfg, g, i, st, apply=True, today=date(2026, 10, 8))
    assert m["deferred"] == 1 and g.downloads == 3 and i.updates == []
    st.close()


def given_up(st, gid="5"):
    for _ in range(3):
        st.record_failure(gid, "InvalidFIT: FIT CRC check failed", refused="abc")


def rows(st):
    return dict(st.db.execute("SELECT garmin_id, status || ',' || attempts || ',' || IFNULL(next_retry, '-') "
                              "FROM activity").fetchall())


def test_a_newer_version_gives_failed_and_given_up_activities_a_fresh_try(tmp_path):
    st = Store(tmp_path)
    given_up(st)
    st.mark_invalid_told(["5"])
    for _ in range(5):
        st.record_failure("6", "ValueError: boom")                  # a week until its next try
    st.record_activity("8", "uploaded", "x")
    st.set_meta(VERSION_KEY, "0.0.1")                               # as an older version left it
    st.close()
    st = Store(tmp_path)
    assert st.activity_status("5") == "invalid" and st.is_deferred("6")     # opening the store changes nothing
    assert st.retry_after_update() == 2
    assert rows(st) == {"5": "failed,3,-", "6": "failed,5,-", "8": "uploaded,0,-"}   # due now, count kept
    assert st.get_meta(VERSION_KEY) == __version__
    assert st.retry_after_update() == 0                             # once per version
    # the same file again: given up at once, and not named again
    st.record_failure("5", "InvalidFIT: FIT CRC check failed", refused="abc")
    (again,) = st.invalid_activities()
    assert again["garmin_id"] == "5" and again["told"] is True
    st.close()


def test_one_given_up_and_named_before_is_no_repeated_failure_when_an_update_makes_it_due(tmp_path):
    st = Store(tmp_path)
    given_up(st)
    st.mark_invalid_told(["5"])
    for _ in range(2):
        st.record_failure("6", "ValueError: boom")
    st.set_meta(VERSION_KEY, "0.0.1")
    st.retry_after_update()
    found = {f["what"]: f for f in silent_error_checks(cfg(tmp_path), st)}
    assert "6" in found["1 activities failing repeatedly"]["detail"]
    assert "5" not in found["1 activities failing repeatedly"]["detail"]
    st.close()


def test_an_older_process_next_to_a_newer_one_leaves_the_backoff_alone(tmp_path):
    st = Store(tmp_path)
    st.record_failure("6", "ValueError: boom")
    for stored in ("99.0.0", __version__):                          # a newer one ran, or this one already
        st.set_meta(VERSION_KEY, stored)
        assert st.retry_after_update() == 0 and st.is_deferred("6")
    st.close()


def test_the_activity_runs_give_the_fresh_try(tmp_path):
    st, g, i = Store(tmp_path), RefusingGarmin(changing=True), IntervalsFake()
    run(tmp_path, st, g, i)
    assert run(tmp_path, st, g, i)["deferred"] == 1
    st.set_meta(VERSION_KEY, "0.0.1")
    assert run(tmp_path, st, g, i)["deferred"] == 0 and g.requested.count("42") == 2
    st.close()


def test_success_ends_the_bookkeeping_of_a_given_up_activity(tmp_path):
    st = Store(tmp_path)
    for gid in ("5", "7"):
        given_up(st, gid)
        st.mark_invalid_told([gid])
    st.clear_failure("5")                                           # enrich mode
    st.record_activity("7", "uploaded", "x")                        # upload mode
    assert st.activity_status("5") == "downloaded" and st.invalid_activities() == []
    for gid in ("5", "7"):
        assert st.get_meta(REFUSED_KEY + gid) is None and st.get_meta(TOLD_KEY + gid) is None
    st.close()


def cfg(tmp_path, **kw):
    return SimpleNamespace(data_dir=tmp_path, timezone=ZoneInfo("Europe/Vienna"), wellness_profile="recommended",
                           stale_hours=24, **kw)


class Ok:
    def login(self, interactive=False):
        pass

    def activities(self, start, end, fields=None, limit=None):
        return []


def test_the_health_check_names_a_given_up_activity_once(tmp_path, monkeypatch):
    st = Store(tmp_path)
    given_up(st)
    sent = []
    monkeypatch.setattr(alerts, "channels", lambda settings: ["push"])
    monkeypatch.setattr(alerts, "send", lambda settings, subject, text, **kw: sent.append(text) or {"push": "sent"})
    report = probe(cfg(tmp_path), Ok(), Ok(), st)
    assert report["exit"] == 2 and len(sent) == 1
    assert "1 activities given up" in sent[0] and "5: InvalidFIT: FIT CRC check failed" in sent[0]
    assert "failing repeatedly" not in sent[0]
    st.set_meta(alerts.HEALTH_KEY, "2000-01-01")                    # the next day
    report = probe(cfg(tmp_path), Ok(), Ok(), st)
    assert report["exit"] == 0 and len(sent) == 1                   # not again
    (listed,) = [f for f in report["findings"] if f["what"] == GIVEN_UP]
    assert listed["alert"] is False and "5" in listed["detail"]
    st.close()


def test_an_alert_that_did_not_go_out_names_it_again_next_day(tmp_path, monkeypatch):
    st = Store(tmp_path)
    given_up(st)
    monkeypatch.setattr(alerts, "channels", lambda settings: ["email"])
    monkeypatch.setattr(alerts, "send", lambda settings, subject, text, **kw: {"email": "failed: smtp down"})
    probe(cfg(tmp_path), Ok(), Ok(), st)
    assert st.invalid_activities()[0]["told"] is False
    st.close()


def test_without_an_alert_channel_the_log_names_it_once(tmp_path, monkeypatch, caplog):
    st = Store(tmp_path)
    given_up(st)
    monkeypatch.setattr(alerts, "channels", lambda settings: [])
    with caplog.at_level("INFO", logger="garmin_intervals_bridge"):
        probe(cfg(tmp_path), Ok(), Ok(), st)
        probe(cfg(tmp_path), Ok(), Ok(), st)
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "given up" in r.getMessage()]
    infos = [r for r in caplog.records if r.levelname == "INFO" and r.getMessage().startswith(GIVEN_UP)]
    assert len(errors) == 1 and len(infos) == 1
    assert [f["what"] for f in silent_error_checks(cfg(tmp_path), st) if "given up" in f["what"]] == [GIVEN_UP]
    st.close()
