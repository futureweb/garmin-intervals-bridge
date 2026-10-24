"""Provider isolation during outages and the configuration it relies on, with fake HTTP and clients."""
import json
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from test_personal import DAY, KEY, NOW, Response, source, token
from test_personal_hybrid import TRIGGER, make_ui, request
from test_personal_hybrid import settings as _settings_fixture
from test_personal_hybrid import store as _store_fixture
from test_personal_integration import Direct, Personal
from test_sync_enrich import GarminFake, IntervalsFake

from garmin_intervals_bridge import alerts, cli, config, health, notices, personal, sources, sync, webui
from garmin_intervals_bridge.garmin import GarminBlocked
from garmin_intervals_bridge.intervals import transient
from garmin_intervals_bridge.personal import PersonalAuthorizationError, PersonalKeyNeeded, PersonalTransientTokenError
from garmin_intervals_bridge.sources import AccountMismatch, HybridGarminSource

settings, store = _settings_fixture, _store_fixture


def _schema_valid(value, schema):
    """The Supervisor scalar types used here; optional means absent, not an empty string."""
    schema = schema.removesuffix("?")
    if schema in ("str", "password"):
        return isinstance(value, str)
    if schema == "bool":
        return type(value) is bool
    if schema.startswith("int"):
        bounds = re.fullmatch(r"int\((-?\d+),(-?\d+)\)", schema)
        assert schema == "int" or bounds, schema
        return type(value) is int and (not bounds or int(bounds[1]) <= value <= int(bounds[2]))
    if schema.startswith("list("):
        return value in schema[5:-1].split("|")
    if schema.startswith("match("):
        return re.match(schema[6:-1], str(value)) is not None
    raise AssertionError(f"Add validation for schema type {schema}")


def test_every_home_assistant_default_validates_against_its_schema():
    config_path = Path(__file__).resolve().parents[1] / "home-assistant/config.yaml"
    options, schema = config_path.read_text(encoding="utf-8").split("\noptions:\n", 1)[1].split("\nschema:\n", 1)
    entries = dict(re.findall(r"^  (\w+): (.+)$", schema, re.M))
    defaults = dict(re.findall(r"^  (\w+): (.+)$", options, re.M))
    assert "garmin_personal_api_key_expires" not in defaults
    assert "garmin_personal_api_key_expires" in entries
    for key, raw in defaults.items():
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        assert _schema_valid(value, entries[key]), (key, value, entries[key])


@pytest.mark.parametrize("value,schema,valid", [("", r"match(^\d{4}-\d{2}-\d{2}$)?", False),
    ("2026-10-24", r"match(^\d{4}-\d{2}-\d{2}$)?", True), ("", "password?", True),
    ("auto", "list(auto|direct|personal|hybrid)", True), ("invalid", "list(auto|direct)", False),
    (False, "bool", True), ("false", "bool", False), (30, "int(30,3600)?", True),
    (29, "int(30,3600)?", False), (True, "int?", False), ("UTC", "str?", True)])
def test_schema_validation_types(value, schema, valid):
    assert _schema_valid(value, schema) is valid


def paired(direct, p, store):
    store.set_meta(HybridGarminSource.PROFILE_KEY, "42")
    store.set_meta("source:pair:" + p._fingerprint[:16], "42")
    return HybridGarminSource(direct, p, store=store)


@pytest.mark.parametrize("error", [GarminBlocked("429"), requests.ConnectionError("offline")])
def test_direct_outage_enriches_all_three_and_retries_once_per_window(settings, store, monkeypatch, error):
    now, attempts = [NOW], []
    monkeypatch.setattr(sources.time, "time", lambda: now[0])
    p, fake, intervals = Personal(settings.data_dir), GarminFake(), IntervalsFake()
    fake.acts = [{**fake.acts[0], "activityId": gid} for gid in (1, 2, 3)]
    intervals.remote = [{**intervals.remote[0], "id": f"i{gid}", "external_id": str(gid)} for gid in (1, 2, 3)]
    # Each remote activity starts without the training effect.
    intervals.update_activity = lambda iid, fields: intervals.updates.append((iid, fields)) or {"id": iid}
    p.activities, p.activity, p.original_fit = fake.activities, fake.activity, fake.original_fit
    def direct():
        d = Direct()
        def login(**kwargs):
            attempts.append(1)
            raise error
        d.login = login
        return d
    g = paired(direct(), p, store)
    result = sync.sync_enrich(settings, g, intervals, store, apply=True, today=date(2026, 10, 8))
    assert result["enriched"] == result["archive_pending"] == 3 and result["failed"] == 0
    assert [iid for iid, _ in intervals.updates] == ["i1", "i2", "i3"]
    for gid in (1, 2, 3):
        assert store.get_meta(f"archive:direct:{gid}") == "pending"
        assert store.fit_path(str(gid)).exists() and store.activity_json_path(str(gid)).exists()
    assert not store.get_meta(notices.WAITING_KEY)
    for _ in range(3):
        g = HybridGarminSource(direct(), p, store=store)
        assert g.snapshot(DAY, endpoints=("hrv", "sleep"))["deferred"] == ["sleep"]
        assert sync.mirror_wellness_files(DAY, g, store) == "deferred"
        assert not g.extras_keys and not g.has_wellness_files
        assert g.account_snapshot()["deferred"]
    assert attempts == [1]
    now[0] += 901
    g.activity_extras({})
    g.activity_extras({})
    assert attempts == [1, 1]
    now[0] += 901
    recovered = HybridGarminSource(Direct(), p, store=store)
    sync.resume_direct_archives(recovered, store)
    assert not store.meta_items("archive:direct:*")


@pytest.mark.parametrize("method,args", [("activity_extras", ({},)), ("wellness_files", (DAY,)),
                                         ("account_snapshot", ()), ("snapshot", (DAY,))])
def test_direct_data_outage_defers_and_account_mismatch_is_not_backoff(settings, store, method, args):
    d, p = Direct(), Personal(settings.data_dir)
    def fail(*a, **kw):
        raise requests.Timeout("offline")
    setattr(d, method, fail)
    g = paired(d, p, store)
    g._direct(method, *args)
    assert g.direct_unavailable and store.get_meta(g.BLOCKED_KEY)
    assert not store.get_meta(notices.WAITING_KEY)
    g._blocked_until = 0
    def mismatch(*a, **kw):
        raise AccountMismatch("different accounts")
    setattr(d, method, mismatch)
    with pytest.raises(AccountMismatch):
        g._direct(method, *args)


def test_one_endpoint_401_keeps_other_wellness_and_scope_across_dates(settings, monkeypatch):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    p = source(settings.data_dir, token(), Response(payload={"steps": 100}), Response(payload={}),
               Response(payload=[]), Response(401), token("renewed"), Response(401))
    raw = p.snapshot(DAY, endpoints=personal.HYBRID_KEYS)
    assert set(raw["data"]) == {"stats", "hrv", "body_battery"}
    assert raw["errors"] == {"nutrition": "PersonalAuthorizationError 401"}
    again = source(settings.data_dir, Response(payload={}), Response(payload={}), Response(payload=[]))
    raw = again.snapshot(DAY + timedelta(days=1), endpoints=personal.HYBRID_KEYS)
    assert set(raw["data"]) == {"stats", "hrv", "body_battery"}
    assert set(raw["errors"]) == {"nutrition"}
    assert all(method == "GET" for method, _, _ in again.session.calls)
    assert len(again.session.calls) == 3


def test_second_endpoint_401_suspends_provider_across_processes(settings, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    p = source(settings.data_dir, token(), Response(401), token("renewed"), Response(401),
               Response(401), token("renewed-again"), Response(401))
    raw = p.snapshot(DAY, endpoints=personal.HYBRID_KEYS)
    assert set(raw["errors"]) == personal.HYBRID_KEYS and not raw["data"]
    assert sum(method == "POST" for method, _, _ in p.session.calls) == 3
    failures = json.loads(p.failure_path.read_text())["failures"]
    assert len(failures) == 3 and "*" in failures
    for _ in range(8):
        again = source(settings.data_dir)
        raw = again.snapshot(DAY + timedelta(days=1))
        assert not raw["data"] and raw["errors"]
        with pytest.raises(PersonalAuthorizationError):
            again.activities(DAY, DAY)
        assert again.requests == 0
    now[0] += 6 * 3600 + 1
    retry = source(settings.data_dir, Response(payload={}))
    assert retry.snapshot(DAY, endpoints=("hrv",))["data"] == {"hrv": {}}


def test_settings_401_is_global_but_zones_401_preserves_settings(settings):
    p = source(settings.data_dir, token(), Response(payload={}), Response(401), token("renewed"), Response(401))
    raw = p.account_snapshot()
    assert raw["data"] == {"user_settings": {}}
    assert raw["errors"] == {"heart_rate_zones": "PersonalAuthorizationError 401"}
    p.session.responses += [Response(401), token("again"), Response(401)]
    raw = p.account_snapshot()
    assert set(raw["errors"]) == {"user_settings", "heart_rate_zones"} and not raw["data"]
    with pytest.raises(PersonalAuthorizationError):
        source(settings.data_dir).activities(DAY, DAY)


@pytest.mark.parametrize("failure", [Response(503), requests.Timeout(KEY), requests.ConnectionError(KEY)])
@pytest.mark.parametrize("command", ["sync", "watch"])
def test_transient_token_outage_backoff_and_successful_timer_exit(settings, store, monkeypatch, caplog, failure, command):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    monkeypatch.setenv("GARMIN_SOURCE", "personal")
    p = source(settings.data_dir, failure)
    monkeypatch.setattr(cli, "_source", lambda *a, **kw: p)
    monkeypatch.setattr(cli, "IntervalsClient", lambda *a: SimpleNamespace())
    monkeypatch.setattr(cli.polling, "due", lambda *a: (True, "test"))
    def read(*a, **kw):
        return p.activities(DAY, DAY)
    monkeypatch.setattr(cli, "sync_enrich", read)
    monkeypatch.setattr(cli, "watch_once", read)
    assert cli.main([command]) == 0
    assert p.requests == 1 and KEY not in caplog.text
    for _ in range(3):
        again = source(settings.data_dir)
        with pytest.raises(PersonalTransientTokenError) as error:
            again.login()
        assert transient(error.value) and again.requests == 0
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    now[0] += 901
    retry = source(settings.data_dir, token(), Response(payload={}))
    retry.login()
    assert retry.requests == 2


@pytest.mark.parametrize("command", ["sync", "watch", "run"])
@pytest.mark.parametrize("mode", ["enrich", "upload"])
def test_hybrid_personal_pause_continues_direct_wellness(settings, store, monkeypatch, command, mode):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    store.set_meta(personal.PAUSE_KEY, str(NOW + 1800))
    p = source(settings.data_dir)
    p.pause_remaining = lambda: 1800
    g = paired(Direct(), p, store)
    monkeypatch.setattr(cli, "_source", lambda *a, **kw: g)
    monkeypatch.setattr(cli, "IntervalsClient", lambda *a: SimpleNamespace())
    monkeypatch.setattr(cli.polling, "due", lambda *a: (True, "test"))
    monkeypatch.setattr(cli, "_gear_recent", lambda *a, **kw: {})
    def activities(*a, **kw):
        return g.activities(DAY, DAY)
    for name in ("sync_enrich", "sync_activities", "watch_once"):
        monkeypatch.setattr(cli, name, activities)
    wellness = []
    def read(*a, **kw):
        raw = g.snapshot(DAY, endpoints=("hrv", "sleep"))
        wellness.append(raw)
        return {"days_checked": 1}
    monkeypatch.setattr(cli, "sync_wellness", read)
    monkeypatch.setattr(cli, "wellness_check", read)
    args = [command] + (["--mode", mode] if command != "watch" else [])
    if command == "run":
        args += ["--iterations", "1"]
    assert cli.main(args) == 0
    assert len(wellness) == 1 and wellness[0]["data"] == {"sleep": {}}
    assert wellness[0]["errors"] == {"hrv": "PersonalRateLimited"} and p.requests == 0


def test_hybrid_backup_personal_pause_still_archives_direct_data(settings, store, monkeypatch):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    monkeypatch.setattr(personal.time, "sleep", lambda _: None)
    store.set_meta(personal.PAUSE_KEY, str(NOW + 1800))
    p = source(settings.data_dir)
    p.pause_remaining = lambda: 1800
    g = paired(Direct(), p, store)
    monkeypatch.setattr(cli, "_source", lambda *a, **kw: g)
    assert cli.run_backup_loop(settings, store, every_minutes=5, iterations=1) == 0
    assert "account" in g.direct.calls and any(isinstance(call, tuple) for call in g.direct.calls)
    raw = store.load_snapshot(datetime.now(timezone.utc).date() - timedelta(days=1))
    assert raw["data"] and "sleep" in raw["data"] and raw["errors"]
    assert p.requests == 0


def test_account_mismatch_only_blocks_personal_data(settings, store):
    p = Personal(settings.data_dir)
    p.profile_id = lambda: 999
    g = HybridGarminSource(Direct(), p, store=store)
    raw = g.snapshot(DAY, endpoints=("hrv", "sleep"))
    assert raw["data"] == {"sleep": {}} and raw["errors"] == {"hrv": "AccountMismatch"}
    assert raw["provenance"] == {"sleep": {"source": "direct"}}
    for method, args in (("activities", (DAY, DAY)), ("activity", (1,)), ("original_fit", (1,))):
        with pytest.raises(AccountMismatch):
            getattr(g, method)(*args)
    assert not p.reads
    assert g.activity_extras({})["errors"] == {}
    assert g.account_snapshot()["data"] and g.wellness_files(DAY) is None


def test_pairing_checks_known_direct_identity_before_personal(settings, store):
    p = Personal(settings.data_dir)
    p.profile_id = lambda: pytest.fail("queried Personal before finding a direct identity")
    g = HybridGarminSource(Direct(blocked=True), p, store=store)
    with pytest.raises(GarminBlocked, match="Cannot verify"):
        g.activities(DAY, DAY)
    assert g.direct.calls == ["login"]


@pytest.mark.parametrize("status", [400, 401])
def test_key_notice_has_correct_reason_and_direct_waiting_text(settings, store, monkeypatch, caplog, status):
    store.set_meta(notices.WAITING_KEY, "yes")
    calls = []
    monkeypatch.setattr(alerts, "channels", lambda s: ["mail"])
    monkeypatch.setattr(alerts, "send", lambda *a: calls.append(a) or {"mail": "sent"})
    p = source(settings.data_dir, Response(status))
    p.on_key_needed = lambda **kw: notices.personal_key_needed(settings, store, None, DAY, **kw)
    with pytest.raises(PersonalKeyNeeded):
        p.login()
    text = calls[0][-1]
    assert "Direct login also needs attention" in text and "serves" not in text
    assert ("malformed or empty" in text) == (status == 400)
    assert ("expired or revoked" in text) == (status == 401)
    assert KEY not in caplog.text + text


def test_hybrid_direct_success_throttles_writes_but_clears_notices(settings, store, monkeypatch):
    now, writes = [NOW], []
    monkeypatch.setattr(notices.time, "time", lambda: now[0])
    original = store.set_meta
    monkeypatch.setattr(store, "set_meta", lambda *a: writes.append(a) or original(*a))
    g = HybridGarminSource(Direct(), Personal(settings.data_dir), store=store,
                          on_direct_ok=lambda: notices.garmin_ok(store))
    for _ in range(10):
        g.activity_extras({})
    assert sum(a[0] == "garmin_last_ok" for a in writes) == 1
    store.set_meta(notices.WAITING_KEY, "yes")
    notices.garmin_ok(store)
    assert not store.get_meta(notices.WAITING_KEY)
    now[0] += 61
    g.activity_extras({})
    assert sum(a[0] == "garmin_last_ok" for a in writes) == 2


def test_trigger_config_strips_or_disables_only_trigger(settings, store, monkeypatch, caplog):
    monkeypatch.setenv("BRIDGE_UI_TRIGGER_TOKEN", " " + TRIGGER + " \n")
    assert config.Settings.from_env().ui_trigger_token == TRIGGER
    monkeypatch.setenv("BRIDGE_UI_TRIGGER_TOKEN", "invalid-secret")
    loaded = config.Settings.from_env()
    assert not loaded.ui_trigger_token and loaded.ui_trigger_warning
    assert "invalid-secret" not in caplog.text + repr(loaded)
    assert sources.source_status(loaded, store)["trigger_warning"] == config.TRIGGER_WARNING
    ui = make_ui(loaded)
    assert config.TRIGGER_WARNING in ui.settings_page(webui.Session())
    assert config.TRIGGER_WARNING in ui.status(webui.Session(), {}, {})
    assert request(ui, headers={"Authorization": "Bearer invalid-secret"})[0] == 404
    assert cli.main(["status"]) == 0


@pytest.mark.parametrize("waiting", [notices.PERSONAL_WAITING_KEY, personal.PAUSE_KEY])
def test_hybrid_liveness_checks_timers_when_personal_waits(settings, store, monkeypatch, waiting):
    monkeypatch.setattr(health.time, "time", lambda: NOW)
    store.record_run("sync", {}, started=NOW - 7200)
    store.set_meta(waiting, str(NOW + 3600))
    assert not health.liveness(settings, store, now=NOW)[0]
    assert health.liveness(replace(settings, garmin_source="personal"), store, now=NOW)[0]


def test_web_can_clear_expiry_without_revalidating_or_removing_key(settings):
    path = settings.data_dir / "settings.conf"
    path.write_text("GARMIN_PERSONAL_API_KEY=" + KEY + "\nGARMIN_PERSONAL_API_KEY_EXPIRES=2026-12-01\n")
    configured = replace(settings, env_file=path, garmin_personal_api_key_expires=date(2026, 12, 1))
    ui = make_ui(configured)
    assert 'name="clear_personal_expiry"' in ui.settings_page(webui.Session())
    ui.settings_post(webui.Session(), {}, {"timezone": "UTC", "clear_personal_expiry": "1"})
    saved = config.parse_env_file(path)
    assert saved["GARMIN_PERSONAL_API_KEY"] == KEY and saved["GARMIN_PERSONAL_API_KEY_EXPIRES"] == ""
    conflict = ui.settings_post(webui.Session(), {}, {"timezone": "UTC", "clear_personal_expiry": "1",
                                                    "personal_key_expires": "2026-12-31"})
    assert "Choose either replacement or removal of the expiry" in conflict
    assert config.parse_env_file(path) == saved


def test_upload_continues_all_three_activities_during_direct_outage(settings, store):
    from test_sync import GarminFake as UploadGarmin
    from test_sync import IntervalsFake as UploadIntervals
    fake, intervals = UploadGarmin(), UploadIntervals()
    fake.acts = [{**fake.acts[0], "activityId": gid} for gid in (1, 2, 3)]
    p = Personal(settings.data_dir)
    p.activities, p.activity, p.original_fit = fake.activities, fake.activity, fake.original_fit
    direct = Direct()
    attempts = []
    def login(**kwargs):
        attempts.append(1)
        raise GarminBlocked("429")
    direct.login = login
    g = paired(direct, p, store)
    result = sync.sync_activities(settings, g, intervals, store, apply=True, allow_upload=True,
                                  today=date(2026, 10, 8))
    assert result["uploaded"] == 3 and result["failed"] == 0
    assert [gid for gid, _ in intervals.uploads] == ["1", "2", "3"] and attempts == [1]
    assert all(store.get_meta(f"archive:direct:{gid}") for gid in (1, 2, 3))


@pytest.mark.parametrize("scope", [personal.SETTINGS_PATH, "/nutrition-service/food/logs/2026-10-20"])
def test_legacy_authorization_state_survives_upgrade(settings, monkeypatch, scope):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    p = source(settings.data_dir, token(), Response(payload={}), Response(payload={}))
    p.login()
    p.failure_path.write_text(json.dumps({"key_fingerprint": p._fingerprint, "kind": "authorization",
                                          "scope": scope, "retry_until": NOW + 3600}))
    before = p.requests
    if scope == personal.SETTINGS_PATH:
        with pytest.raises(PersonalAuthorizationError):
            p.activities(DAY, DAY)
        assert p.requests == before
    else:
        raw = p.snapshot(DAY + timedelta(days=1), endpoints=("hrv", "nutrition"))
        assert raw["data"] == {"hrv": {}}
        assert raw["errors"] == {"nutrition": "PersonalAuthorizationError 401"}
        assert p.requests == before + 1


def test_changed_direct_account_still_serves_only_direct_snapshot_keys(settings, store):
    p, d = Personal(settings.data_dir), Direct()
    g = paired(d, p, store)
    d.client = SimpleNamespace(profile_id=999)
    raw = g.snapshot(DAY, endpoints=("hrv", "sleep"))
    assert raw["data"] == {"sleep": {}} and raw["errors"] == {"hrv": "AccountMismatch"}
    assert not store.get_meta(g.BLOCKED_KEY)


@pytest.mark.parametrize("reply", [Response(503), requests.Timeout(KEY), Response(payload={}),
                                   Response(payload=ValueError(KEY)), Response(429)])
def test_activity_step_isolates_personal_failures_but_propagates_intervals(settings, store, reply):
    p = source(settings.data_dir, token(), reply)
    result = sources.activity_step(settings, store, p.activities, DAY, DAY)
    assert result["deferred"].startswith("Personal")
    if isinstance(reply, Response) and reply.status_code == 429:
        assert store.get_meta(personal.PAUSE_KEY)
    def intervals_failure():
        raise requests.ConnectionError("Intervals unavailable")
    with pytest.raises(requests.ConnectionError):
        sources.activity_step(settings, store, intervals_failure)


def test_trigger_and_mismatch_documentation():
    root = Path(__file__).resolve().parents[1]
    for name in ("deploy/README.md", "SECURITY.md"):
        text = (root / name).read_text(encoding="utf-8")
        assert "BRIDGE_UI_TRIGGER_TOKEN" in text and "Authorization: Bearer <token>" in text
        assert "POST /trigger/poll-now" in text and "Home Assistant ingress" in text
    text = (root / "docs/PERSONAL_API.md").read_text(encoding="utf-8")
    assert "Direct wellness, extras, files and account data continue" in text
    assert "Clear the saved expiry (keep the key)" in text
