"""Personal API keys and tokens: the measured token contract, expiry reminders, auto and fallback, with fakes."""
import base64
import json
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import requests
from test_fit import build_fit
from test_personal import DAY, KEY, NOW, Response, source, token
from test_personal_integration import Direct, Personal
from test_wellness_schedule import Intervals, at

from garmin_intervals_bridge import (
    backup,
    cli,
    config,
    health,
    notices,
    personal,
    polling,
    sources,
    sync,
    webui,
    wizard,
)
from garmin_intervals_bridge.mapping import map_wellness
from garmin_intervals_bridge.personal import (
    PersonalAuthorizationError,
    PersonalHTTPError,
    PersonalKeyNeeded,
    PersonalTokenError,
    adapt,
)
from garmin_intervals_bridge.sources import HybridGarminSource, garmin_source, merge_snapshot, source_status
from garmin_intervals_bridge.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_SOURCE", "direct")
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", KEY)
    return config.Settings.from_env()


def jwt(**claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{payload}.synthetic-signature"


def jwt_response(**claims):
    return Response(content=(" \n" + jwt(**claims) + "\n").encode(), headers={"Content-Type": "text/plain"})


@pytest.mark.parametrize("claims,seconds", [
    ({"iat": NOW, "exp": NOW + 86400}, 86400),
    ({"iat": NOW - 3600, "exp": NOW + 200000}, 86400),
    ({"iat": NOW, "exp": NOW + 7200}, 7200),
    ({"iat": NOW}, 86400), ({"exp": "unreadable", "iat": NOW}, 86400),
    ({"exp": NOW + 3600}, 3600),
])
def test_live_jwt_scheduling(claims, seconds):
    parsed = personal.parse_token(" \n" + jwt(**claims) + "\n", NOW)
    assert parsed["usable_until"] == NOW + seconds - 60
    assert personal.parse_token({"access_token": jwt(**claims)}, NOW)["usable_until"] == parsed["usable_until"]


def test_unreadable_jwt_payload_uses_24_hours_and_expired_jwt_fails():
    assert personal.parse_token("header.%%%.signature", NOW)["usable_until"] == NOW + 86400 - 60
    with pytest.raises(ValueError):
        personal.parse_token(jwt(exp=NOW - 1), NOW)


def test_personal_check_reports_live_contract_without_claim_values(tmp_path, monkeypatch):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    g = source(tmp_path, jwt_response(iat=NOW, exp=NOW + 86400, garmin_guid="secret-identity"), Response(payload={}))
    report = g.check()
    assert report["token_content_type"] == "text/plain"
    assert report["token_kind"] == "JWT" and report["token_lifetime_hours"] == 24
    assert report["jwt_claim_names"] == ["exp", "garmin_guid", "iat"]
    assert "secret-identity" not in json.dumps(report) and KEY not in json.dumps(report)
    assert source(tmp_path, key="").check()["key"] == "key not set"


def test_rejected_key_backs_off_across_processes_but_rotation_retries(tmp_path, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    first = source(tmp_path, Response(401))
    with pytest.raises(PersonalKeyNeeded):
        first.login()
    for _ in range(3):
        other = source(tmp_path)
        with pytest.raises(PersonalKeyNeeded):
            other.login()
        assert other.requests == 0
    now[0] += 3601
    retry = source(tmp_path, Response(401))
    with pytest.raises(PersonalKeyNeeded):
        retry.login()
    assert retry.requests == 1
    rotated = source(tmp_path, token(), Response(payload={}), key="new-key")
    rotated.login()
    assert rotated.requests == 2


def test_persistent_401_stops_renewal_until_explicit_check(tmp_path):
    g = source(tmp_path, token(), Response(401), token("renewed"), Response(401))
    incidents = []
    g.on_authorization = lambda: incidents.append("authorization")
    with pytest.raises(PersonalAuthorizationError):
        g.login()
    assert incidents == ["authorization"]
    after = source(tmp_path)
    with pytest.raises(PersonalAuthorizationError):
        after.login()
    assert after.requests == 0
    checked = source(tmp_path, token(), Response(payload={}))
    assert checked.check()["settings_statuses"] == [200]
    assert not checked.failure_path.exists()


def test_data_403_keeps_other_wellness_and_becomes_three_day_health_finding(settings):
    store = Store(settings.data_dir)
    try:
        for offset in (2, 1, 0):
            day = at(10).date() - timedelta(days=offset)
            g = source(settings.data_dir, *([token()] if offset == 2 else []), Response(403),
                       Response(payload={"sleepTimeSeconds": 27000}), Response(204))
            raw = g.snapshot(day, endpoints=("stats", "sleep", "hrv"))
            assert raw["errors"] == {"stats": "PersonalHTTPError 403"}
            assert raw["data"]["sleep"]["dailySleepDTO"]["sleepTimeSeconds"] == 27000
            assert sync.transient_errors(raw) == []
            store.save_snapshot(day, raw)
        findings = health.silent_error_checks(replace(settings, garmin_source="personal"), store, now=at(10).timestamp())
        assert any(f["alert"] and "stats" in str(f) for f in findings)
    finally:
        store.close()


class SharedDirect(Direct):
    def activities(self, start, end):
        self._call("activities")
        return [{"activityId": 9}]

    def activity(self, activity_id):
        self._call("activity")
        return {"activityId": activity_id, "direct": True}

    def original_fit(self, activity_id):
        self._call("original_fit")
        return build_fit()


@pytest.mark.parametrize("error", [personal.PersonalRateLimited(), PersonalHTTPError(403),
                                     PersonalHTTPError(503), requests.Timeout("timeout"),
                                     PersonalAuthorizationError("401")])
def test_hybrid_never_falls_back_on_other_failures(tmp_path, error):
    p = Personal(tmp_path)
    g = HybridGarminSource(SharedDirect(), p)
    g._verify()
    before = list(g.direct.calls)
    def fail(*args, **kwargs):
        raise error
    p.activities = fail
    with pytest.raises(type(error)):
        g.activities(DAY, DAY)
    assert g.direct.calls == before


def test_token_403_is_not_a_key_rejection_or_fallback(tmp_path):
    g = HybridGarminSource(SharedDirect(), source(tmp_path, Response(403)))
    with pytest.raises(PersonalTokenError):
        g.activities(DAY, DAY)
    assert g.direct.calls == ["login"]  # establish the direct identity before querying Personal


def test_mutual_fallback_recovery_provenance_and_one_notice(settings, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    monkeypatch.setattr(personal.time, "sleep", lambda _: None)
    store = Store(settings.data_dir)
    notes, mails = [], []
    intervals = SimpleNamespace(create_note=lambda *a: notes.append(a))
    monkeypatch.setattr(notices.alerts, "channels", lambda s: ["mail"])
    monkeypatch.setattr(notices.alerts, "send", lambda *a: mails.append(a) or {"mail": "sent"})
    p = source(settings.data_dir, Response(401))
    monkeypatch.setattr(sources, "PersonalGarminSource", lambda *a, **kw: p)
    settings = replace(settings, garmin_source="hybrid", intervals_notes=True)
    p.on_key_needed = lambda **kw: notices.personal_key_needed(settings, store, intervals, DAY, apply=True)
    p.on_ok = lambda: notices.personal_ok(store)
    try:
        g = garmin_source(settings, direct_factory=SharedDirect, store=store, intervals=intervals, apply=True)
        for _ in range(2):
            assert g.activities(DAY, DAY) == [{"activityId": 9}]
            assert g.activity(9)["direct"]
            assert g.original_fit(9) == build_fit()
            raw = g.snapshot(DAY, endpoints=("stats", "hrv"))
            assert raw["provenance"]["stats"]["source"] == "direct"
        assert p.requests == 1 and len(notes) == len(mails) == 1
        assert source_status(settings, store)["served_by"]["stats"] == "direct"
        now[0] += 3601
        p.session.responses += [token(), Response(payload={"userProfileId": 42}), Response(payload=[]),
                                Response(payload={"totalSteps": 15000})]
        assert g.activities(DAY, DAY) == []
        assert g.snapshot(DAY, endpoints=("stats",))["data"]["stats"]["totalSteps"] == 15000
        assert not store.get_meta(notices.PERSONAL_WAITING_KEY)
        assert source_status(settings, store)["served_by"]["activities"] == "personal"
        assert source_status(settings, store)["served_by"]["stats"] == "personal"
        assert p.requests == 5
    finally:
        store.close()


def test_missing_key_falls_back_and_both_missing_do_not_loop(tmp_path):
    g = HybridGarminSource(SharedDirect(), source(tmp_path, key=""))
    g.login()
    assert g.activities(DAY, DAY) == [{"activityId": 9}]
    assert g.personal.requests == 0
    blocked = HybridGarminSource(SharedDirect(blocked=True), source(tmp_path, key=""))
    for _ in range(3):
        with pytest.raises(PersonalKeyNeeded):
            blocked.activities(DAY, DAY)
    assert blocked.direct.calls == ["login"] and blocked.personal.requests == 0


def test_account_mismatch_stops_before_mixing_data(tmp_path):
    p = Personal(tmp_path)
    p.profile_id = lambda: 999
    g = HybridGarminSource(SharedDirect(), p)
    raw = g.snapshot(DAY)
    assert set(raw["data"]) == set(sources.DAY_ENDPOINTS) - personal.HYBRID_KEYS
    assert raw["errors"] == {k: "AccountMismatch" for k in personal.HYBRID_KEYS}
    assert not p.reads


@pytest.mark.parametrize("key,session,expected", [("", False, "direct"), ("", True, "direct"),
                                                   (KEY, False, "personal"), (KEY, True, "hybrid")])
def test_auto_selection(settings, key, session, expected, monkeypatch):
    monkeypatch.delenv("GARMIN_SOURCE", raising=False)
    assert config.Settings.from_env().garmin_source == "auto"
    if session:
        settings.token_dir.mkdir()
        (settings.token_dir / "session.json").write_text("{}")
    configured = replace(settings, garmin_source="auto", garmin_personal_api_key=key)
    assert config.effective_source(configured) == expected
    assert config.source_label(configured) == f"auto → {expected}"
    for mode in ("direct", "personal", "hybrid"):
        assert config.effective_source(replace(configured, garmin_source=mode)) == mode


@pytest.mark.parametrize("value", ["2026-2-03", "2026-02-30", "26-02-03", "20260203", "bad"])
def test_expiry_format_is_validated(settings, monkeypatch, value):
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY_EXPIRES", value)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        config.Settings.from_env()


def test_expiry_reminders_deduplicate_by_key_date_and_apply(settings, monkeypatch):
    expiry = date(2026, 11, 1)
    settings = replace(settings, garmin_personal_expires=expiry, intervals_notes=True)
    store = Store(settings.data_dir)
    sent, notes = [], []
    monkeypatch.setattr(notices.alerts, "channels", lambda s: ["mail"])
    monkeypatch.setattr(notices.alerts, "send", lambda *a: sent.append(a) or {"mail": "sent"})
    intervals = SimpleNamespace(create_note=lambda *a: notes.append(a))
    try:
        notices.personal_expiry_reminders(settings, store, intervals, expiry - timedelta(days=15), apply=True)
        assert not sent and not notes
        for apply in (False, False, True, True):
            notices.personal_expiry_reminders(settings, store, intervals, expiry - timedelta(days=14), apply=apply)
        assert len(sent) == len(notes) == 1
        for _ in range(2):
            notices.personal_expiry_reminders(settings, store, intervals, expiry - timedelta(days=3), apply=True)
        assert len(sent) == len(notes) == 2
        notices.personal_expiry_reminders(replace(settings, garmin_personal_api_key="rotated"), store, intervals,
                                         expiry - timedelta(days=14), apply=True)
        notices.personal_expiry_reminders(replace(settings, garmin_personal_expires=expiry + timedelta(days=1)),
                                         store, intervals, expiry - timedelta(days=13), apply=True)
        assert len(sent) == len(notes) == 4
        notices.personal_expiry_reminders(settings, store, intervals, expiry + timedelta(days=1), apply=True)
        assert len(sent) == 4
    finally:
        store.close()


def test_short_jwt_reminder_once_and_expired_date_is_only_a_reminder(settings, monkeypatch):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    store = Store(settings.data_dir)
    notes = []
    settings = replace(settings, intervals_notes=True)
    intervals = SimpleNamespace(create_note=lambda *a: notes.append(a))
    try:
        for _ in range(2):
            g = source(settings.data_dir, jwt_response(iat=NOW, exp=NOW + 7200), Response(payload={}))
            g.on_short_token = lambda: notices.personal_expiry_reminders(settings, store, intervals, DAY, apply=True,
                                                                        short=True)
            assert g.check()["token_lifetime_hours"] == 2
        assert len(notes) == 1 and "within a day" in notes[0][2]
        expired = source(settings.data_dir, Response(payload={}))
        expired.expires, expired.today = DAY, lambda: DAY + timedelta(days=1)
        expired.login()
        assert expired.requests == 1
        assert not personal.parse_token(jwt(iat=NOW, exp=NOW + 86399), NOW)["short_lifetime"]
    finally:
        store.close()


def test_live_weight_sleep_fitness_shapes_and_raw_deduplication(tmp_path):
    weight = {"dailyWeightSummaries": [
        {"summaryDate": str(DAY), "allWeightMetrics": [
            {"date": str(DAY) + "T06:00:00", "weight": 71.2},
            {"date": str(DAY) + "T18:00:00", "weight": 72.1}]},
        {"summaryDate": str(DAY - timedelta(days=1)), "allWeightMetrics": [{"weight": 99}]}],
        "nextDateWeight": {"weight": 123}, "totalAverage": {"weight": 77}}
    age = {"chronologicalAge": 45, "fitnessAge": 33, "lastUpdated": str(DAY) + "T09:00:00.123"}
    g = source(tmp_path, token(), Response(payload={"totalSteps": 15000, "lastSyncTimestampGMT": None}),
               Response(payload={"sleepTimeSeconds": None, "sleepScores": {}, "awakeSleepSeconds": None}),
               Response(payload=weight), Response(payload=age))
    raw = g.snapshot(DAY, endpoints=("stats", "sleep", "body_composition", "weigh_ins", "fitness_age"))
    assert not raw["errors"] and raw["data"]["sleep"] is None
    assert raw["data"]["fitness_age"] == age
    assert [x["weight"] for x in raw["data"]["weigh_ins"]["dateWeightList"]] == [72100, 71200]
    native, _ = map_wellness(raw, DAY, DAY + timedelta(days=1), "all")
    assert native["weight"] == 72.1 and "sleepSecs" not in native
    assert set(raw["raw_personal"]) == {"sleep", "body_composition", "weigh_ins"}
    call = g.session.calls[-2]
    assert "/weight/range/" in call[1] and call[2]["params"] == {"includeAll": "true"}
    assert weight["dailyWeightSummaries"][0]["allWeightMetrics"][0]["weight"] == 71.2


@pytest.mark.parametrize("final", [False, True])
def test_only_final_lossy_direct_payloads_are_retained(final):
    old = {"data": {"stats": {"totalSteps": 1200}, "hrv": {"value": 1}, "body_battery": {"value": 1},
                    "nutrition": {"value": 1}, "sleep": {"rich": True}}, "final": final}
    fresh = {"source": "personal", "data": {key: {"fresh": True} for key in old["data"]}, "provenance": {}}
    merged = merge_snapshot(old, fresh)
    for key in ("stats", "hrv", "body_battery", "nutrition"):
        assert merged["data"][key] == {"fresh": True}
    assert merged["data"]["sleep"] == ({"rich": True} if final else {"fresh": True})
    if final:
        assert merged["provenance"]["sleep"]["retained"] is True


def test_new_process_does_not_retry_direct_or_deferred_days_until_recovery(settings, monkeypatch):
    store = Store(settings.data_dir)
    today = at(8).date()
    monkeypatch.setattr(sources, "PersonalGarminSource", lambda *a, **k: Personal(settings.data_dir))
    configured = replace(settings, garmin_source="hybrid")
    intervals = Intervals()
    intervals.records[today] = {"sleepSecs": 28000}
    store.set_meta("wellness:since", "test")
    try:
        store.set_meta(HybridGarminSource.PROFILE_KEY, "42")
        first = garmin_source(configured, direct_factory=lambda *a: Direct(blocked=True), store=store)
        sync.wellness_check(settings, first, intervals, store, apply=False, now=at(8))
        for hour in (9, 10, 11):
            again = garmin_source(configured, direct_factory=lambda *a: Direct(blocked=True), store=store)
            sync.wellness_check(settings, again, intervals, store, apply=False, now=at(hour))
            assert not again.personal.reads and not again.direct.calls
        notices.garmin_ok(store)
        restored = garmin_source(configured, direct_factory=Direct, store=store)
        sync.wellness_check(settings, restored, intervals, store, apply=False, now=at(12))
        assert restored.personal.reads and restored.direct.calls
        assert not store.meta_items("deferred:wellness:*")
    finally:
        store.close()


@pytest.mark.parametrize("kind", ["wellness", "backup"])
def test_deferred_day_recovery_is_capped_and_paced(settings, monkeypatch, kind):
    store = Store(settings.data_dir)
    today = at(21).date()
    prefix = "deferred:wellness:" if kind == "wellness" else "backup:direct:"
    sleeps = []
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)
    try:
        store.set_meta("wellness:since", "test")
        store.set_meta(f"wellness:morning:{today}", "ok")
        for n in range(1, 31):
            day = today - timedelta(days=n)
            store.set_meta(prefix + str(day), "pending")
            store.set_meta(f"wellness:final:{day}", "partial")
            store.set_meta(backup.READS_KEY + str(day), "2@1")
        p = Personal(settings.data_dir)
        g = HybridGarminSource(Direct(), p)
        if kind == "wellness":
            sync.wellness_check(settings, g, Intervals(), store, apply=False, now=at(21))
        else:
            backup.backup_once(settings, g, store, now=at(21))
        assert len(store.meta_items(prefix + "*")) == 27
        assert len(p.reads) == 3 and sleeps == [2, 2, 2]
    finally:
        store.close()


def test_personal_final_read_uses_time_fallback_without_stats_probe(settings):
    store = Store(settings.data_dir)
    today = at(10).date()
    p = Personal(settings.data_dir)
    p._get = lambda *a, **k: None
    intervals = Intervals()
    intervals.records[today] = {"steps": 50}
    store.set_meta("wellness:since", "test")
    store.set_meta(f"wellness:next:{today}", str(at(21).timestamp()))
    try:
        sync.wellness_check(settings, p, intervals, store, apply=False, now=at(10))
        assert p.reads == [(today - timedelta(days=2), None)]
        result = sync.wellness_check(settings, p, intervals, store, apply=False, now=at(20))
        assert result["sync_checks"] == 0 and result["final"] == 1
        assert all(day < today for day, _ in p.reads)
    finally:
        store.close()


def test_hybrid_final_read_asks_direct_for_the_device_sync(settings):
    store = Store(settings.data_dir)
    today = at(10).date()

    class Synced(Direct):
        def snapshot(self, day, endpoints=None):
            out = super().snapshot(day, endpoints=endpoints)
            if endpoints == ("stats",):
                out["data"]["stats"] = {"lastSyncTimestampGMT": f"{today}T06:00:00.0"}   # 08:00 local
            return out

    d, p = Synced(), Personal(settings.data_dir)
    intervals = Intervals()
    intervals.records[today] = {"steps": 50}               # the official import brought something today
    store.set_meta("wellness:since", "test")
    store.set_meta(f"wellness:next:{today}", str(at(21).timestamp()))
    try:
        result = sync.wellness_check(settings, HybridGarminSource(d, p, store=store), intervals, store,
                                     apply=False, now=at(10))
        assert result["sync_checks"] == 1 and ("stats",) in d.calls
        assert store.get_meta(f"wellness:final:{today - timedelta(days=1)}") == "synced"
        fresh = Store(settings.data_dir / "probe-only")
        try:
            HybridGarminSource(Synced(), Personal(settings.data_dir / "probe-only"), store=fresh).device_sync_stats(today)
            assert fresh.meta_items("source:served:*") == []       # only asked for the sync time
        finally:
            fresh.close()
    finally:
        store.close()


def test_hybrid_device_sync_probe_ignores_another_accounts_watch(settings):
    store = Store(settings.data_dir)
    today = at(10).date()

    class Other(Direct):
        def _call(self, name):
            super()._call(name)
            self.client = SimpleNamespace(profile_id=99)              # signed in to a different account

        def snapshot(self, day, endpoints=None):
            out = super().snapshot(day, endpoints=endpoints)
            if endpoints == ("stats",):
                out["data"]["stats"] = {"lastSyncTimestampGMT": f"{today}T06:00:00.0"}
            return out

    d, p = Other(), Personal(settings.data_dir)
    store.set_meta("source:pair:" + p._fingerprint[:16], "42")         # the key belongs to 42
    try:
        assert HybridGarminSource(d, p, store=store).device_sync_stats(today) is None
        assert ("stats",) not in d.calls
    finally:
        store.close()


def test_hybrid_device_sync_probe_waits_while_direct_needs_a_sign_in(settings):
    store = Store(settings.data_dir)
    today = at(10).date()
    d, p = Direct(blocked=True), Personal(settings.data_dir)
    store.set_meta(HybridGarminSource.PROFILE_KEY, "42")                # paired before the sign-in expired
    store.set_meta("source:pair:" + p._fingerprint[:16], "42")
    store.set_meta(notices.WAITING_KEY, "yes")                         # known: the sign-in has to be renewed
    store.set_meta(HybridGarminSource.RETRY_KEY, str(at(12).timestamp()))
    intervals = Intervals()
    intervals.records[today] = {"steps": 50}
    store.set_meta("wellness:since", "test")
    store.set_meta(f"wellness:next:{today}", str(at(21).timestamp()))
    try:
        result = sync.wellness_check(settings, HybridGarminSource(d, p, store=store), intervals, store,
                                     apply=False, now=at(10))
        assert result["sync_checks"] == 0                        # nothing to ask while the sign-in waits
        assert store.get_meta(f"wellness:final:{today - timedelta(days=1)}") is None   # 20:00 fallback later
        assert not store.get_meta(f"wellness:verify:{today}")                         # and no growing backoff
    finally:
        store.close()


def test_broken_extras_queue_entries_do_not_starve_valid_archives(settings):
    store = Store(settings.data_dir)
    try:
        for n in range(1, 7):
            store.set_meta(f"archive:direct:{n}", "pending")
            if n >= 4:
                store.save_activity_json(str(n), {"activityId": n})
                store.atomic_save(store.fit_path(str(n)), build_fit())
        sync.resume_direct_archives(Direct(), store)
        assert not store.meta_items("archive:direct:*")
        for n in range(4, 7):
            assert store.activity_extras_path(str(n)).is_file()
    finally:
        store.close()


def test_no_deferred_extras_before_summary_or_fit_is_saved(settings):
    store = Store(settings.data_dir)
    g = HybridGarminSource(Direct(blocked=True), source(settings.data_dir, key=""))
    g.direct_waiting = True
    try:
        with pytest.raises(PersonalKeyNeeded):
            sync.archive_activity("12", {"activityId": 12}, g, store)
        assert not store.get_meta("archive:direct:12")
        assert store.save_account_snapshot({"data": {}, "deferred": ["account"]}) is None
        assert not (settings.data_dir / "raw/account").exists()
    finally:
        store.close()


def test_polling_counts_personal_activity_sidecar_only_once(settings):
    store = Store(settings.data_dir)
    try:
        item = {"activityId": 1, "startTimeGMT": "2026-10-10 06:00:00", "duration": 3600}
        store.save_activity_json("1", item)
        store.save_activity_json("1", {**item, "_bridge": {"source": "personal"}})
        assert len(polling.archived_ends(store, settings.timezone, at(12).timestamp())) == 1
    finally:
        store.close()


def test_requests_keeps_proxy_ca_environment_and_no_netrc(tmp_path, monkeypatch):
    g = personal.PersonalGarminSource(tmp_path, KEY)
    def forbidden(*a, **k):
        pytest.fail("netrc authentication was consulted")
    monkeypatch.setattr(requests.sessions, "get_netrc_auth", forbidden)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/synthetic/ca.pem")
    request = g.session.prepare_request(requests.Request("GET", personal.ROOT, headers={"Authorization": "Bearer test"}))
    assert request.headers["Authorization"] == "Bearer test"
    assert request.headers["User-Agent"].startswith("garmin-intervals-bridge/")
    env = g.session.merge_environment_settings(personal.ROOT, {}, None, None, None)
    assert env["proxies"]["https"] == "http://proxy.invalid:8080" and env["verify"] == "/synthetic/ca.pem"


def test_failed_settings_save_does_not_replace_shared_token_cache(settings, monkeypatch):
    path = settings.data_dir / "settings.conf"
    path.write_text("GARMIN_SOURCE=direct\n")
    configured = replace(settings, env_file=path)
    original = source(settings.data_dir, token(), Response(payload={}))
    original.login()
    before = original.cache_path.read_bytes()
    checked_paths = []
    def candidate(folder, key, delay):
        checked_paths.append(folder)
        return source(folder, token("candidate-token"), Response(payload={}), key=key)
    monkeypatch.setattr(webui, "PersonalGarminSource", candidate)
    ui = webui.UI(host="127.0.0.1", port=0, settings=lambda: configured, in_run=True, newer_release=lambda: None)
    page = ui.settings_post(webui.Session(), {}, {"timezone": "UTC", "garmin_source": "personal",
                                                "garmin_personal_api_key": "candidate-key", "alert_email": "invalid"})
    assert "nothing was changed" in page
    assert original.cache_path.read_bytes() == before and all(not p.exists() for p in checked_paths)
    assert path.read_text() == "GARMIN_SOURCE=direct\n"


@pytest.mark.parametrize("form,phrase", [
    ({"garmin_source": "personal"}, "key is required"),
    ({"garmin_source": "hybrid", "clear_personal_key": "1"}, "key is required"),
    ({"garmin_personal_api_key": "new", "clear_personal_key": "1"}, "either replacement or removal"),
    ({"personal_key_expires": "2026-2-5"}, "YYYY-MM-DD"),
])
def test_invalid_personal_settings_do_not_save(settings, form, phrase):
    path = settings.data_dir / "settings.conf"
    path.write_text("GARMIN_SOURCE=direct\n")
    configured = replace(settings, env_file=path, garmin_personal_api_key="")
    ui = webui.UI(host="127.0.0.1", port=0, settings=lambda: configured, in_run=True, newer_release=lambda: None)
    page = ui.settings_post(webui.Session(), {}, {"timezone": "UTC", **form})
    assert phrase in page and path.read_text() == "GARMIN_SOURCE=direct\n"


def test_wizard_validates_before_saving_and_defaults_to_auto(settings, monkeypatch):
    order = []
    monkeypatch.setattr(wizard, "PersonalGarminSource", lambda *a: SimpleNamespace(login=lambda: order.append("validated")))
    answers = iter(["y", "", "2027-01-02"])
    prompt = wizard.Prompt(ask=lambda q: next(answers), secret=lambda q: "new", say=lambda t: None)
    def save(values):
        assert values["GARMIN_SOURCE"] == "auto" and values["GARMIN_PERSONAL_API_KEY_EXPIRES"] == "2027-01-02"
        order.append("saved")
        return settings
    wizard._personal_settings(prompt, settings, save)
    assert order == ["validated", "saved"]
    def reject():
        raise PersonalKeyNeeded()
    monkeypatch.setattr(wizard, "PersonalGarminSource", lambda *a: SimpleNamespace(login=reject))
    answers = iter(["y", "", ""])
    assert wizard._personal_settings(prompt, settings, lambda v: pytest.fail("saved invalid key")) is settings


def test_wizard_hybrid_offers_direct_signin(settings, monkeypatch):
    direct = Direct(blocked=True)
    logins = []
    def login(interactive=False):
        logins.append(interactive)
        if interactive:
            direct.blocked = False
        direct._call("login")
    direct.login = login
    monkeypatch.setattr(sources, "PersonalGarminSource", lambda *a, **k: Personal(settings.data_dir))
    prompt = wizard.Prompt(say=lambda t: None)
    g = wizard._garmin(prompt, replace(settings, garmin_source="hybrid"), lambda *a: direct)
    assert g is not None and logins == [False, True]


def test_ha_expiry_option_and_status_surfaces(settings, monkeypatch):
    path = settings.data_dir / "options.json"
    path.write_text(json.dumps({"garmin_personal_api_key_expires": "2027-01-01"}))
    assert config.load_options_file(path) == ["GARMIN_PERSONAL_API_KEY_EXPIRES"]
    loaded = config.Settings.from_env()
    assert loaded.garmin_personal_expires == date(2027, 1, 1)
    loaded = replace(loaded, garmin_source="auto", garmin_personal_api_key="", env_file=None)
    store = Store(settings.data_dir)
    try:
        report = source_status(loaded, store)
        assert report["garmin_source"] == "auto → direct" and report["key_expires"] == "2027-01-01"
        assert isinstance(report["key_days_left"], int)
        store.set_meta(personal.PAUSE_KEY, str(NOW + 3600))
        assert cli.intervals_paused(store) == 0
        ok, reason = health.liveness(replace(loaded, garmin_source="personal"), store, now=NOW)
        assert ok and "Personal API paused" in reason
        ui = webui.UI(host="127.0.0.1", port=0, settings=lambda: loaded, in_run=True, newer_release=lambda: None)
        assert "2027-01-01" in ui.settings_page(webui.Session())
    finally:
        store.close()


@pytest.mark.parametrize("chosen, link", [("auto", True), ("personal", False)])
def test_key_alone_says_what_stays_empty(settings, monkeypatch, chosen, link):
    loaded = replace(settings, garmin_source=chosen, env_file=None)        # a key, no Garmin Connect session
    assert config.effective_source(loaded) == "personal"
    ui = webui.UI(host="127.0.0.1", port=0, settings=lambda: loaded, in_run=True, newer_release=lambda: None)
    page = ui.status(webui.Session(), {}, {})
    assert "With the key alone" in page and "Training Readiness" in page
    assert ('<a href="/garmin">sign in to Garmin Connect as well</a>' in page) is link
    assert ("choose the Garmin source auto or hybrid" in page) is not link     # explicit personal: settings first
    hybrid = replace(settings, garmin_source="hybrid", env_file=None)
    assert "With the key alone" not in webui.UI(host="127.0.0.1", port=0, settings=lambda: hybrid, in_run=True,
                                                newer_release=lambda: None).status(webui.Session(), {}, {})
    assert "sign in to Garmin Connect as well, with the Garmin source auto or hybrid" in personal.KEY_ALONE


def test_existing_personal_pause_is_honored_without_extending_it(settings, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    store = Store(settings.data_dir)
    try:
        store.set_meta(personal.PAUSE_KEY, str(NOW + 900))
        g = garmin_source(replace(settings, garmin_source="personal"), store=store)
        for advance in (300, 300, 299):
            now[0] += advance
            with pytest.raises(personal.PersonalRateLimited) as exc:
                g.login()
            assert cli.pause_on_rate_limit(store, exc.value) == NOW + 900 - now[0]
            assert float(store.get_meta(personal.PAUSE_KEY)) == NOW + 900
        assert g.requests == 0 and cli.intervals_paused(store) == 0
    finally:
        store.close()


def test_failed_final_read_does_not_promote_old_intraday_direct_payload():
    old = {"data": {"sleep": {"intraday": True}}, "errors": {}}
    incomplete_final = {"final": True, "data": {"stats": {}}, "errors": {"sleep": "Timeout"}}
    archive = merge_snapshot(old, incomplete_final)
    assert archive["provenance"]["sleep"]["final"] is False
    updated = merge_snapshot(archive, {"source": "personal", "data": {"sleep": {"new": True}}})
    assert updated["data"]["sleep"] == {"new": True}


def test_failed_extras_back_off_so_the_next_run_reaches_later_entries(settings):
    store = Store(settings.data_dir)
    direct = Direct()
    attempted = []
    original = direct.activity_extras
    def extras(activity, keys=None):
        attempted.append(activity["activityId"])
        if activity["activityId"] < 4:
            raise requests.Timeout()
        return original(activity, keys=keys)
    direct.activity_extras = extras
    try:
        for n in range(1, 7):
            store.set_meta(f"archive:direct:{n}", "pending")
            store.save_activity_json(str(n), {"activityId": n})
            store.atomic_save(store.fit_path(str(n)), build_fit())
        sync.resume_direct_archives(direct, store)
        sync.resume_direct_archives(direct, store)
        assert attempted == [1, 2, 3, 4, 5, 6]
        assert len(store.meta_items("archive:direct:*")) == 3
    finally:
        store.close()


def test_account_check_supports_preferences_only_settings(tmp_path):
    p = source(tmp_path, token(), Response(payload={"userProfileId": 42}), Response(payload=[]))
    g = HybridGarminSource(SharedDirect(), p)
    assert g.activities(DAY, DAY) == []
    assert "/usersummary/daily" in p.session.calls[1][1]
    assert "calendarDate" in p.session.calls[1][2]["params"]


def test_json_envelope_containing_a_jwt_is_still_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    payload = {"access_token": jwt(iat=NOW, exp=NOW + 86400)}
    g = source(tmp_path, Response(payload=payload, content=json.dumps(payload).encode(),
                                  headers={"Content-Type": "application/json"}), Response(payload={}))
    report = g.check()
    assert report["token_kind"] == "JWT" and report["settings_statuses"] == [200]
    assert report["token_response_type"] == "dict"


def test_token_403_aborts_once_while_data_403_is_per_key(tmp_path):
    g = source(tmp_path, Response(403))
    with pytest.raises(PersonalTokenError, match="token endpoint"):
        g.snapshot(DAY)
    assert g.requests == 1


def test_live_weight_in_grams_is_not_converted_again():
    # measured 2026-10-24: weight/range returns grams despite the spec's kilograms
    grams = {"dailyWeightSummaries": [{"summaryDate": str(DAY), "allWeightMetrics": [
        {"date": str(DAY) + "T06:00:00", "weight": 71200.0}]}]}
    composition = adapt("body_composition", grams, DAY)
    assert [x["weight"] for x in composition["dateWeightList"]] == [71200.0]
    native, _ = map_wellness({"data": {"body_composition": composition}}, DAY, DAY + timedelta(days=1), "all")
    assert native["weight"] == 71.2
