"""Provider routing, archive protection, scheduling and configuration with fakes."""
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_fit import build_fit
from test_mapping import sample
from test_personal import KEY, Response, source, token
from test_wellness_schedule import Intervals, at

from garmin_intervals_bridge import cli, config, health, notices, personal, sources, webui, wizard
from garmin_intervals_bridge.backup import backup_once
from garmin_intervals_bridge.garmin import DAY_ENDPOINTS, EXTRA_KEYS, GarminLoginNeeded
from garmin_intervals_bridge.personal import PersonalAuthorizationError, PersonalGarminSource
from garmin_intervals_bridge.sources import HybridGarminSource, garmin_source, merge_snapshot
from garmin_intervals_bridge.store import Store
from garmin_intervals_bridge.sync import archive_activity, mirror_wellness_files, sync_wellness_files, wellness_check

DAY = date(2026, 10, 10)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIDGE_TIMEZONE", "Europe/Vienna")
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GARMIN_SOURCE", "direct")
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", KEY)
    return config.Settings.from_env()


class Direct:
    def __init__(self, *args, blocked=False):
        self.calls, self.requests, self.client, self.blocked = [], 0, None, blocked

    def _call(self, name):
        self.requests += 1
        self.calls.append(name)
        if self.blocked:
            raise GarminLoginNeeded("MFA or sign-in needed")
        self.client = SimpleNamespace(profile_id=42)

    def login(self, interactive=False):
        self._call("login")

    def snapshot(self, day, endpoints=None):
        self._call(tuple(endpoints or DAY_ENDPOINTS))
        return {"date": str(day), "data": {k: {} for k in endpoints or DAY_ENDPOINTS}, "errors": {}}

    def activity_extras(self, activity, keys=None):
        self._call("extras")
        return {"errors": {}, **{k: None for k in keys or EXTRA_KEYS}}

    def wellness_files(self, day):
        self._call("files")
        return None

    def account_snapshot(self, **kwargs):
        self._call("account")
        return {"data": {"user_profile": {}}, "errors": {}}


class Personal(PersonalGarminSource):
    def __init__(self, path):
        super().__init__(path, KEY, delay=0)
        self.reads, self.gets = [], []

    def _get(self, path, **params):
        self.requests += 1
        self.gets.append(path)
        if path == personal.SETTINGS_PATH:
            return {"userData": {"userProfilePK": 42}}
        if "/usersummary/daily" in path:
            return {"userProfileId": 42}
        if "dailySleep" in path:
            return {"sleepTimeSeconds": 28000}
        if "/hrv/" in path:
            return {"hrvSummary": {"lastNightAvg": 42}}
        if "/range/" in path:
            return {"dailyWeightSummaries": []}
        if "/fitnessage/" in path or "hydration" in path or "bodyBattery" in path:
            return []
        if "activities/search" in path:
            return []
        return {}

    def snapshot(self, day, endpoints=None):
        self.reads.append((day, endpoints))
        return super().snapshot(day, endpoints=endpoints)


def test_source_default_direct_and_secret_not_in_repr(settings, monkeypatch):
    assert settings.garmin_source == "direct" and KEY not in repr(settings)
    sentinel = object()
    assert garmin_source(settings, direct_factory=lambda *a: sentinel) is sentinel
    monkeypatch.setenv("GARMIN_SOURCE", "wrong")
    with pytest.raises(ValueError, match="GARMIN_SOURCE"):
        config.Settings.from_env()


def test_personal_mode_never_constructs_direct(settings, monkeypatch):
    monkeypatch.setattr(sources, "PersonalGarminSource", lambda *a, **k: Personal(settings.data_dir))
    def forbidden(*args):
        pytest.fail("personal mode constructed the direct client")
    g = garmin_source(replace(settings, garmin_source="personal"), direct_factory=forbidden)
    g.login()
    g.snapshot(DAY)
    g.activities(DAY, DAY)
    g.account_snapshot()
    assert g.client is None


def test_hybrid_routes_whole_payloads_and_does_not_fallback(tmp_path):
    direct, p = Direct(), Personal(tmp_path)
    hybrid = HybridGarminSource(direct, p)
    raw = hybrid.snapshot(DAY)
    assert set(p.reads[0][1]) == personal.HYBRID_KEYS
    assert set(direct.calls[1]) == set(DAY_ENDPOINTS) - personal.HYBRID_KEYS
    assert raw["provenance"]["sleep"]["source"] == "direct"
    assert raw["provenance"]["hrv"]["source"] == "personal"
    assert hybrid.activities(DAY, DAY) == []
    hybrid.activity_extras({"activityId": 1})
    hybrid.wellness_files(DAY)
    hybrid.account_snapshot()
    assert direct.calls[-3:] == ["extras", "files", "account"]
    def fail(*a, **k):
        raise PersonalAuthorizationError("Personal HTTP 403")
    p._get = fail
    before = list(direct.calls)
    failed = hybrid.snapshot(DAY, endpoints=("hrv",))
    assert failed["data"] == {} and failed["errors"] == {"hrv": "PersonalAuthorizationError 401"}
    assert direct.calls == before


def test_hybrid_login_needed_continues_and_notifies_once_per_episode(settings, monkeypatch):
    store = Store(settings.data_dir)
    monkeypatch.setattr(sources, "PersonalGarminSource", lambda *a, **k: Personal(settings.data_dir))
    sent = []
    intervals = SimpleNamespace(create_note=lambda *a: sent.append(a))
    settings = replace(settings, garmin_source="hybrid", intervals_notes=True)
    try:
        store.set_meta(HybridGarminSource.PROFILE_KEY, "42")
        for attempt in range(2):
            direct = Direct(blocked=True)
            g = garmin_source(settings, direct_factory=lambda *a: direct, store=store, intervals=intervals, apply=True)
            g.login()
            raw = g.snapshot(DAY)
            assert set(raw["data"]) == personal.HYBRID_KEYS and raw["errors"] == {}
            assert set(raw["deferred"]) == set(DAY_ENDPOINTS) - personal.HYBRID_KEYS
            assert g.activities(DAY, DAY) == []
            assert g.activity_extras({"activityId": 1})["deferred"]
            assert direct.calls == (["login"] if attempt == 0 else [])
        assert len(sent) == 1 and "Personal API work continues" in sent[0][2]
        assert store.get_meta(notices.WAITING_KEY)
        notices.personal_ok(store)
        assert store.get_meta(notices.WAITING_KEY)                 # Personal success cannot reset direct episode
        notices.garmin_ok(store)          # sign-in or health probe ends the persisted waiting episode
        good = garmin_source(settings, direct_factory=Direct, store=store)
        good.snapshot(DAY)
        assert not store.get_meta(notices.WAITING_KEY)
    finally:
        store.close()


def test_personal_key_notice_has_its_own_episode(settings, monkeypatch):
    store = Store(settings.data_dir)
    settings = replace(settings, intervals_notes=True)
    notes, mails = [], []
    intervals = SimpleNamespace(create_note=lambda *args: notes.append(args))
    monkeypatch.setattr(notices.alerts, "channels", lambda s: ["test"])
    monkeypatch.setattr(notices.alerts, "send", lambda *a: mails.append(a) or {"test": "sent"})
    try:
        for _ in range(2):
            notices.personal_key_needed(settings, store, intervals, DAY, apply=True)
        notices.garmin_ok(store)
        assert store.get_meta(notices.PERSONAL_WAITING_KEY)
        assert len(notes) == len(mails) == 1
        assert "key needs replacement" in notes[0][1]
        notices.personal_ok(store)
        notices.personal_key_needed(settings, store, intervals, DAY, apply=True)
        assert len(notes) == len(mails) == 2
        assert KEY not in str(notes) + str(mails)
    finally:
        store.close()


def test_personal_capabilities_no_readiness_files_or_extras_retries(settings):
    store, p, intervals = Store(settings.data_dir), Personal(settings.data_dir), Intervals()
    intervals.records[DAY] = {"sleepSecs": 28000}
    store.set_meta("wellness:since", "test")
    try:
        result = wellness_check(settings, p, intervals, store, apply=False, now=at(8))
        assert result["failed"] == 0 and store.get_meta(f"wellness:morning:{DAY}") == "ok"
        reads = list(p.reads)
        for hour in (9, 10, 12, 16):
            wellness_check(settings, p, intervals, store, apply=False, now=at(hour))
        assert p.reads == reads
        assert not any("trainingreadiness" in x or "/files/wellness" in x for x in p.gets)
        assert mirror_wellness_files(DAY, p, store) == "unsupported"
        assert store.load_wellness_index(DAY) is None        # unsupported is not a successful empty download
        assert sync_wellness_files(settings, p, store, days=[DAY])["unsupported"] == 1
        store.atomic_save(store.fit_path("1"), build_fit())
        store.save_activity_json("1", {"activityId": 1})
        p.activity_extras = lambda *a, **k: pytest.fail("extras requested")
        assert archive_activity("1", {"activityId": 1}, p, store) == "on_disk"
    finally:
        store.close()


def test_hybrid_scheduled_hrv_continues_and_deferred_days_recover(settings):
    store, p, intervals = Store(settings.data_dir), Personal(settings.data_dir), Intervals()
    intervals.records[DAY] = {"sleepSecs": 28000}
    store.set_meta("wellness:since", "test")
    direct = Direct(blocked=True)
    g = HybridGarminSource(direct, p, store=store)
    g._profile = "42"  # known direct account from before the expired session
    try:
        result = wellness_check(settings, g, intervals, store, apply=True, now=at(8))
        assert result["failed"] == 0 and intervals.records[DAY]["hrv"] == 42
        assert store.get_meta(f"wellness:morning:{DAY}") == "direct-pending"
        yesterday = DAY - timedelta(days=1)
        assert store.get_meta(f"deferred:wellness:{yesterday}") == "pending"
        reads = len(p.reads)
        wellness_check(settings, g, intervals, store, apply=True, now=at(9))
        assert len(p.reads) == reads and len(direct.calls) == 1
        recovered = HybridGarminSource(Direct(), Personal(settings.data_dir))
        wellness_check(settings, recovered, intervals, store, apply=False, now=at(10))
        assert not store.get_meta(f"deferred:wellness:{yesterday}")
        assert "sleep" in store.load_snapshot(yesterday)["data"]
    finally:
        store.close()


def test_backup_continues_when_direct_login_is_missing(settings):
    store = Store(settings.data_dir)
    store.set_meta(HybridGarminSource.PROFILE_KEY, "42")
    g = HybridGarminSource(Direct(blocked=True), Personal(settings.data_dir), store=store)
    try:
        result = backup_once(settings, g, store, now=at(21))
        assert result["wellness_days"] > 0 and result["failed"] == 0
        assert result["account"] == 0
        assert len(g.direct.calls) == 1
        assert store.load_snapshot(DAY - timedelta(days=1))["data"]["hrv"]
    finally:
        store.close()


def test_archive_never_downgrades_direct_and_keeps_personal_raw(settings):
    previous = {"date": str(DAY), "final": True, "data": {"sleep": {"dailySleepDTO": {"sleepTimeSeconds": 29000},
                                                       "avgSkinTempDeviationC": 0.2}}, "errors": {"hrv": "Timeout"}}
    p = Personal(settings.data_dir)
    fresh = p.snapshot(DAY, endpoints=("sleep",))
    merged = merge_snapshot(previous, fresh)
    assert merged["data"]["sleep"] == previous["data"]["sleep"]
    assert merged["raw_personal"]["sleep"] == {"sleepTimeSeconds": 28000}
    assert merged["provenance"]["sleep"]["source"] == "direct"
    assert merged["errors"] == {"hrv": "Timeout"}
    updated = merge_snapshot(fresh, {"date": str(DAY), "data": {"sleep": previous["data"]["sleep"]}, "errors": {}})
    assert updated["data"]["sleep"] == previous["data"]["sleep"]
    store = Store(settings.data_dir)
    try:
        old = {"activityId": 1, "extraDetail": "retained"}
        store.save_activity_json("1", old)
        result = store.save_activity_json("1", {"activityId": 1, "_bridge": {"source": "personal"}})
        assert result.name == "1.personal.json"
        assert json.loads(store.activity_json_path("1").read_text()) == old
        store.save_account_snapshot({"data": {"user_settings": {"richer": True}, "devices": [1]}, "errors": {}})
        path = store.save_account_snapshot(p.account_snapshot())
        assert path.parent.name == "account-personal"
        assert json.loads(path.read_text())["source"] == "personal"
        direct_path = settings.data_dir / "raw/account" / f"{date.today()}.json"
        assert json.loads(direct_path.read_text())["data"]["devices"] == [1]
    finally:
        store.close()


def test_health_excludes_capability_dropouts_but_keeps_supported_dropouts(settings):
    settings = replace(settings, garmin_source="personal")
    store = Store(settings.data_dir)
    today = at(10).date()
    try:
        for offset in range(9, 0, -1):
            day = today - timedelta(days=offset)
            raw = {"date": str(day), **sample()}
            if offset <= 2:
                raw["data"] = {"sleep": raw["data"]["sleep"]}
                raw["data"]["sleep"].pop("avgSkinTempDeviationC", None)
            raw["errors"] = {"training_readiness": "Old failure"}
            store.save_snapshot(day, raw)
        findings = health.silent_error_checks(settings, store, now=at(10).timestamp())
        assert len([f for f in findings if f["what"] == "Personal API coverage" and not f["alert"]]) == 1
        errors = str([f for f in findings if f["alert"]])
        assert "training_readiness" not in errors and "GarminSkinTempDeviationC" not in errors
        assert "readiness" not in errors and "GarminVO2MaxCycling" not in errors
        assert "hrv" in errors                                # supported fields must still be monitored
    finally:
        store.close()


def test_health_reports_each_hybrid_provider_and_personal_liveness(settings):
    store = Store(settings.data_dir)
    try:
        report = health.probe(replace(settings, garmin_source="hybrid"),
                              HybridGarminSource(Direct(blocked=True), Personal(settings.data_dir)), None, store)
        assert not report["services"]["garmin"]["ok"] and report["services"]["personal"]["ok"]
        assert report["garmin_source"] == "hybrid"
        ok, reason = health.liveness(replace(settings, garmin_source="personal"), store)
        assert ok and "sign-in" not in reason
    finally:
        store.close()


def test_cli_personal_check_only_prints_contract(settings, monkeypatch, capsys):
    settings = replace(settings, garmin_source="personal")
    g = source(settings.data_dir, token(), Response(payload={"private": "never-print"}))
    monkeypatch.setattr(cli.Settings, "from_env", lambda: settings)
    monkeypatch.setattr(cli, "configured_log_file", lambda: None)
    monkeypatch.setattr(cli, "PersonalGarminSource", lambda *a: g)
    assert cli.main(["personal-check"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["settings_statuses"] == [200]
    assert "never-print" not in output and KEY not in output


def test_settings_page_keeps_replaces_clears_and_never_returns_key(settings, monkeypatch):
    path = settings.data_dir / "settings.conf"
    path.write_text("GARMIN_PERSONAL_API_KEY=" + KEY + "\nGARMIN_SOURCE=personal\n")
    monkeypatch.setenv("BRIDGE_ENV_FILE", str(path))
    monkeypatch.setenv("GARMIN_SOURCE", "personal")
    ui = webui.UI(host="127.0.0.1", port=0, in_run=True, newer_release=lambda: None)
    session = webui.Session()
    page = ui.settings_page(session)
    assert KEY not in page and 'name="garmin_personal_api_key"' in page
    form = {"timezone": "Europe/Vienna", "garmin_source": "personal", "garmin_personal_api_key": ""}
    page = ui.settings_post(session, {}, form)
    assert "Saved." in page and KEY not in page
    assert config.parse_env_file(path)["GARMIN_PERSONAL_API_KEY"] == KEY
    monkeypatch.setattr(webui, "PersonalGarminSource", lambda *a: SimpleNamespace(login=lambda: None))
    page = ui.settings_post(session, {}, {**form, "garmin_personal_api_key": "replacement-secret"})
    assert "replacement-secret" not in page
    assert config.parse_env_file(path)["GARMIN_PERSONAL_API_KEY"] == "replacement-secret"
    page = ui.settings_post(session, {}, {**form, "garmin_source": "direct", "clear_personal_key": "1"})
    assert "Saved." in page and config.parse_env_file(path)["GARMIN_PERSONAL_API_KEY"] == ""
    assert config.parse_env_file(path)["GARMIN_SOURCE"] == "direct"


def test_ui_rejected_key_is_not_saved_or_echoed(settings, monkeypatch):
    path = settings.data_dir / "settings.conf"
    path.write_text("GARMIN_SOURCE=direct\n")
    monkeypatch.setenv("BRIDGE_ENV_FILE", str(path))
    def reject():
        raise ValueError("sensitive-server-response")
    monkeypatch.setattr(webui, "PersonalGarminSource", lambda *a: SimpleNamespace(login=reject))
    ui = webui.UI(host="127.0.0.1", port=0, in_run=True, newer_release=lambda: None)
    page = ui.settings_post(webui.Session(), {}, {"timezone": "UTC", "garmin_source": "personal",
                                                 "garmin_personal_api_key": "rejected-key"})
    assert "Could not validate" in page
    assert "rejected-key" not in page and "sensitive-server-response" not in page
    assert path.read_text() == "GARMIN_SOURCE=direct\n"


def test_ui_personal_login_actions_never_touch_direct(settings):
    settings = replace(settings, garmin_source="personal", env_file=None)
    def forbidden(*a, **k):
        pytest.fail("direct login used")
    ui = webui.UI(host="127.0.0.1", port=0, settings=lambda: settings, garmin_source=forbidden,
                  newer_release=lambda: None)
    session = webui.Session()
    for method in (ui.garmin_get, ui.garmin_post, ui.garmin_mfa):
        assert "Personal mode" in method(session, {}, {})
    assert KEY not in ui.settings_page(session)


def test_wizard_optional_personal_question_can_skip_or_select_hybrid(settings, monkeypatch):
    monkeypatch.setattr(wizard, "PersonalGarminSource", lambda *a: SimpleNamespace(login=lambda: None))
    saved = []
    def save(values):
        saved.append(values)
        return replace(settings, garmin_source=values["GARMIN_SOURCE"],
                       garmin_personal_api_key=values.get("GARMIN_PERSONAL_API_KEY", KEY))
    p = wizard.Prompt(ask=lambda q: "n", secret=lambda q: pytest.fail("should be skipped"), say=lambda t: None)
    assert wizard._personal_settings(p, settings, save) is settings and not saved
    answers = iter(["y", "hybrid", "2027-01-02"])
    p = wizard.Prompt(ask=lambda q: next(answers), secret=lambda q: "wizard-secret", say=lambda t: None)
    result = wizard._personal_settings(p, settings, save)
    assert result.garmin_source == "hybrid" and result.garmin_personal_api_key == "wizard-secret"
    assert "wizard-secret" not in repr(result)


def test_home_assistant_password_option_reaches_settings(settings, monkeypatch):
    path = settings.data_dir / "options.json"
    path.write_text(json.dumps({"garmin_source": "hybrid", "garmin_personal_api_key": KEY}))
    for name in ("GARMIN_SOURCE", "GARMIN_PERSONAL_API_KEY"):
        monkeypatch.delenv(name)
    assert set(config.load_options_file(path)) == {"GARMIN_SOURCE", "GARMIN_PERSONAL_API_KEY"}
    loaded = config.Settings.from_env()
    assert loaded.garmin_source == "hybrid" and loaded.garmin_personal_api_key == KEY
    assert KEY not in repr(loaded)
    cfg = Path(__file__).parents[1] / "home-assistant/config.yaml"
    assert "garmin_personal_api_key: password?" in cfg.read_text()


def test_partial_reads_cannot_relabel_untouched_direct_data(settings):
    p = Personal(settings.data_dir)
    old = {"data": {"stats": {"totalSteps": 9000, "rich": {"detail": 1}}, "sleep": {}}, "errors": {}}
    first = merge_snapshot(old, p.snapshot(DAY, endpoints=("sleep",)))
    assert first["provenance"]["stats"]["source"] == "direct"
    second = merge_snapshot(first, p.snapshot(DAY, endpoints=("stats",)))
    assert second["data"]["stats"] == {"userProfileId": 42}
    assert second["provenance"]["stats"]["source"] == "personal"


def test_deferred_work_recovers_beyond_the_normal_lookback(settings):
    from garmin_intervals_bridge.sync import resume_direct_archives
    store = Store(settings.data_dir)
    old = DAY - timedelta(days=12)
    p = Personal(settings.data_dir)
    g = HybridGarminSource(Direct(blocked=True), p)
    try:
        store.atomic_save(store.fit_path("17"), build_fit())
        store.save_activity_json("17", {"activityId": 17, "startTimeGMT": str(old) + "T10:00:00"})
        assert archive_activity("17", {"activityId": 17}, g, store) == "pending"
        assert store.get_meta("archive:direct:17")
        store.set_meta(f"deferred:wellness:{old}", "pending")
        store.set_meta(f"wellness:final:{old}", "partial")
        store.set_meta("wellness:since", "test")
        restored = HybridGarminSource(Direct(), Personal(settings.data_dir))
        resume_direct_archives(restored, store)
        assert not store.get_meta("archive:direct:17")
        assert store.activity_extras_path("17").exists()
        wellness_check(settings, restored, Intervals(), store, apply=False, now=at(21))
        assert not store.get_meta(f"deferred:wellness:{old}")
        assert "sleep" in store.load_snapshot(old)["data"]
    finally:
        store.close()
