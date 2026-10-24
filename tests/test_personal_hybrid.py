"""Hybrid pairing, provider independence and the HTTP trigger, with synthetic credentials and no sockets."""
import io
import json
from dataclasses import replace
from datetime import date, timedelta
from email.message import Message
from types import SimpleNamespace

import pytest
import requests
from test_personal import DAY, KEY, NOW, Response, source, token
from test_personal_integration import Direct, Personal
from test_personal_keys import jwt_response

from garmin_intervals_bridge import alerts, cli, config, notices, personal, polling, sources, webui, wizard
from garmin_intervals_bridge.garmin import GarminBlocked
from garmin_intervals_bridge.personal import PersonalAuthorizationError, PersonalKeyNeeded, PersonalTokenError
from garmin_intervals_bridge.sources import AccountMismatch, HybridGarminSource
from garmin_intervals_bridge.store import Store


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRIDGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BRIDGE_TIMEZONE", "UTC")
    monkeypatch.setenv("GARMIN_SOURCE", "hybrid")
    monkeypatch.setenv("GARMIN_PERSONAL_API_KEY", KEY)
    monkeypatch.delenv("GARMIN_PERSONAL_API_KEY_EXPIRES", raising=False)
    monkeypatch.delenv("BRIDGE_UI_TRIGGER_TOKEN", raising=False)
    return config.Settings.from_env()


@pytest.fixture
def store(settings):
    st = Store(settings.data_dir)
    yield st
    st.close()


@pytest.mark.parametrize("failure", [requests.ConnectionError(), personal.PersonalHTTPError(503),
                                     personal.PersonalRateLimited(), PersonalAuthorizationError("401")])
def test_direct_only_work_never_contacts_personal(settings, store, failure):
    p = Personal(settings.data_dir)
    def fail(*a, **k):
        pytest.fail("direct-only work contacted Personal")
    p.profile_id = p._get = fail
    g = HybridGarminSource(Direct(), p, store=store)
    g.login()
    assert g.snapshot(DAY, endpoints=("sleep",))["data"] == {"sleep": {}}
    g.activity_extras({"activityId": 1})
    g.wellness_files(DAY)
    assert g.account_snapshot()["data"]
    assert p.requests == 0
    # A mixed snapshot also retains the direct datasets when Personal is unavailable.
    def unavailable(*a, **k):
        raise failure
    p.profile_id = unavailable
    assert g.snapshot(DAY, endpoints=("sleep", "hrv"))["data"] == {"sleep": {}}


def test_pairing_survives_restart_and_direct_outage(settings, store):
    first = HybridGarminSource(Direct(), Personal(settings.data_dir), store=store)
    first.activities(DAY, DAY)
    assert store.get_meta(first._pair_key) == "42"
    assert len(first._pair_key.rsplit(":", 1)[-1]) == 16
    direct = Direct()
    def unavailable(*a, **k):
        raise GarminBlocked("direct outage")
    direct.login = unavailable
    p = Personal(settings.data_dir)
    again = HybridGarminSource(direct, p, store=store)
    assert again.activities(DAY, DAY) == []
    assert again.snapshot(DAY, endpoints=("hrv",))["data"]["hrv"]
    assert not any("usersummary/daily" in x for x in p.gets)
    assert again.snapshot(DAY, endpoints=("hrv", "sleep"))["deferred"] == ["sleep"]


def test_new_key_must_match_saved_profile_while_direct_waits(settings, store):
    store.set_meta(HybridGarminSource.PROFILE_KEY, "42")
    store.set_meta(notices.WAITING_KEY, "yes")
    p = Personal(settings.data_dir)
    p.profile_id = lambda: 999
    g = HybridGarminSource(Direct(blocked=True), p, store=store)
    with pytest.raises(AccountMismatch):
        g.activities(DAY, DAY)
    assert g.direct.calls == [] and p.reads == []


def test_unverified_pair_cannot_mix_accounts_without_direct_identity(settings, store):
    g = HybridGarminSource(Direct(blocked=True), Personal(settings.data_dir), store=store)
    with pytest.raises(GarminBlocked, match="Cannot verify"):
        g.activities(DAY, DAY)
    assert not store.get_meta(g._pair_key)


def test_direct_recovery_retries_hourly_across_processes(settings, store, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(sources.time, "time", lambda: now[0])
    def make(blocked):
        return HybridGarminSource(Direct(blocked=blocked), Personal(settings.data_dir), store=store,
                                 on_login_needed=lambda: store.set_meta(notices.WAITING_KEY, "yes"),
                                 on_direct_ok=lambda: notices.garmin_ok(store))
    first = make(True)
    first.activity_extras({})
    assert first.direct.calls == ["login"]
    again = make(True)
    assert again.direct_waiting and not again.direct.calls
    now[0] += 3601
    assert again.direct_waiting and again.direct.calls == ["login"]
    assert again.direct_waiting and again.direct.calls == ["login"]
    now[0] += 3601
    recovered = make(False)
    assert not recovered.direct_waiting
    assert recovered.activity_extras({})["errors"] == {}
    assert not store.get_meta(notices.WAITING_KEY)


@pytest.mark.parametrize("skew", [-7200, 7200])
def test_token_cache_uses_local_clock(settings, monkeypatch, skew):
    monkeypatch.setattr(personal.time, "time", lambda: NOW + skew)
    g = source(settings.data_dir, jwt_response(iat=NOW, exp=NOW + 86400), Response(payload={}))
    g.login()
    cached = json.loads(g.cache_path.read_text())
    assert cached["usable_until"] == NOW + skew + 86400 - 60
    again = source(settings.data_dir, Response(payload={}))
    again.login()
    assert again.requests == 1


@pytest.mark.parametrize("status,error", [(400, PersonalKeyNeeded), (401, PersonalKeyNeeded),
    (403, PersonalTokenError), (404, PersonalTokenError), (410, PersonalTokenError), (418, PersonalTokenError),
    (200, PersonalTokenError)])
def test_all_token_failures_stop_snapshot_and_back_off(settings, monkeypatch, status, error):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    g = source(settings.data_dir, Response(status, payload={"bad": KEY}))
    with pytest.raises(error) as exc:
        g.snapshot(DAY)
    assert KEY not in str(exc.value) and g.requests == 1
    if status == 400:
        assert "malformed" in str(exc.value)
    for _ in range(3):
        other = source(settings.data_dir)
        with pytest.raises(error):
            other.snapshot(DAY)
        assert other.requests == 0
    now[0] += 3601
    retry = source(settings.data_dir, token(), Response(payload={}))
    retry.login()
    assert retry.requests == 2


def test_authorization_scope_retry_and_validation(settings, store, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    g = source(settings.data_dir, token(), Response(401), token("renewed"), Response(401))
    g.on_authorization = lambda: notices.personal_authorization_needed(settings, store)
    with pytest.raises(PersonalAuthorizationError):
        g._get("/hrv-service/hrv/test")
    other = source(settings.data_dir, Response(payload={}))
    other.on_ok = lambda: notices.personal_ok(store)
    other.login()  # working settings must not clear a failed HRV endpoint's suspension
    assert store.get_meta(personal.AUTH_WAIT_KEY)
    with pytest.raises(PersonalAuthorizationError):
        other._get("/hrv-service/hrv/test")
    now[0] += 6 * 3600 + 1
    other.session.responses.append(Response(payload={"hrv": 42}))
    assert other._get("/hrv-service/hrv/test") == {"hrv": 42}
    assert not store.get_meta(personal.AUTH_WAIT_KEY)
    other._save_failure("authorization")
    personal.validated_key(settings.data_dir)
    assert not other.failure_path.exists()


def test_notices_only_most_urgent_and_no_failed_delivery_loop(settings, store, monkeypatch):
    calls = []
    monkeypatch.setattr(alerts, "channels", lambda s: ["mail"])
    monkeypatch.setattr(alerts, "send", lambda *a: calls.append(a) or {"mail": "failed"})
    configured = replace(settings, garmin_personal_expires=DAY + timedelta(days=2))
    for _ in range(4):
        notices.personal_expiry_reminders(configured, store, None, DAY)
        notices.personal_key_needed(configured, store, None, DAY)
    assert len(calls) == 2
    assert "Direct login serves shared data when available" in calls[-1][-1]
    assert not any(":14:" in key for key, value in store.meta_items("notice:personal_expiry:*"))
    p = source(settings.data_dir, Response(401))
    callbacks = []
    p.on_key_needed = lambda **kw: callbacks.append(1)
    for _ in range(4):
        with pytest.raises(PersonalKeyNeeded):
            p.login()
    assert callbacks == [1]


def test_source_status_has_dataset_keys_and_clears_on_mode_change(settings, store):
    sources._source_mode(settings, store)
    g = HybridGarminSource(Direct(), Personal(settings.data_dir), store=store)
    g.snapshot(DAY, endpoints=("hrv", "sleep"))
    g.account_snapshot()
    g.activity_extras({})
    result = sources.source_status(settings, store)["served_by"]
    assert result["hrv"] == "personal" and result["sleep"] == "direct" and result["user_profile"] == "direct"
    assert not {"snapshot", "login", "account_snapshot", "activity_extras"} & result.keys()
    assert sources.source_status(replace(settings, garmin_source="direct"), store)["served_by"] == {}


def test_personal_ok_and_short_notice_do_not_commit_per_request(settings, store, monkeypatch):
    calls = []
    original = store.set_meta
    def record(*a):
        calls.append(a)
        return original(*a)
    monkeypatch.setattr(store, "set_meta", record)
    for _ in range(10):
        notices.personal_ok(store)
        notices.personal_expiry_reminders(settings, store, None, DAY, short=True)
    assert sum(a[0] == "personal_last_ok" for a in calls) == 1
    assert sum(a[0].startswith("personal:short:") for a in calls) == 1


def test_auto_hybrid_login_alert_uses_effective_source(settings, store, monkeypatch):
    settings.token_dir.mkdir(exist_ok=True)
    (settings.token_dir / "session.json").write_text("{}")
    calls = []
    monkeypatch.setattr(alerts, "channels", lambda s: ["mail"])
    monkeypatch.setattr(alerts, "send", lambda *a: calls.append(a) or {"mail": "sent"})
    alerts.login_needed(replace(settings, garmin_source="auto"), store)
    assert "Personal API work continues" in calls[0][-1]


@pytest.mark.parametrize("cmd", ["watch", "sync"])
def test_personal_pause_is_successful_skip(settings, store, monkeypatch, capsys, cmd):
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    store.set_meta(personal.PAUSE_KEY, str(NOW + 600))
    monkeypatch.setenv("GARMIN_SOURCE", "personal")
    assert cli.main([cmd]) == 0
    assert "Personal API asked for a pause" in capsys.readouterr().out


def test_direct_backup_ignores_intervals_pause(settings, store, monkeypatch):
    store.set_meta(cli.PAUSE_KEY, str(NOW + 3600))
    monkeypatch.setattr(cli.time, "time", lambda: NOW)
    calls = []
    monkeypatch.setattr(cli, "_source", lambda *a: Direct())
    monkeypatch.setattr(cli, "backup_once", lambda *a: calls.append(1) or {"activities": 0, "wellness_days": 0, "account": 0})
    assert cli.run_backup_loop(replace(settings, garmin_source="direct"), store, every_minutes=5, iterations=1) == 0
    assert calls == [1]


TRIGGER = "synthetic-trigger-secret-at-least-24"


def request(ui, *, path="/trigger/poll-now", method="POST", headers=None, body=b"ignored", ip="127.0.0.1"):
    """Run the actual HTTP handler over byte streams; the sandbox forbids sockets."""
    handler = webui.handler_for(ui).__new__(webui.handler_for(ui))
    handler.path, handler.command = path, method
    handler.request_version, handler.requestline = "HTTP/1.1", f"{method} {path} HTTP/1.1"
    handler.client_address = (ip, 1234)
    handler.headers = Message()
    for k, v in {"Host": f"127.0.0.1:{ui.port}", **(headers or {})}.items():
        handler.headers[k] = v
    handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
    handler._handle(method)
    raw = handler.wfile.getvalue().decode()
    return int(raw.split()[1]), raw, handler.rfile.tell()


def make_ui(settings, **kwargs):
    return webui.UI(port=8765, settings=lambda: settings, newer_release=lambda: None,
                    **{"host": "127.0.0.1", "in_run": True, **kwargs})


@pytest.mark.parametrize("host,password", [("127.0.0.1", None), ("0.0.0.0", "web-password")])
def test_trigger_no_session_same_effect_as_button(settings, store, monkeypatch, caplog, host, password):
    configured = replace(settings, ui_trigger_token=TRIGGER)
    ui = make_ui(configured, host=host, password=password)
    monkeypatch.setattr(webui.time, "time", lambda: NOW)
    polling.POLL_NOW.clear()
    status, raw, read = request(ui, headers={"Authorization": "Bearer " + TRIGGER,
                                           "Content-Length": "99999999", "Origin": "https://shortcuts.invalid"})
    assert status == 202 and "Content-Type: text/plain" in raw and "looking for new activities now" in raw
    assert read == 0 and not ui.sessions
    triggered = store.get_meta(polling.NOW_KEY)
    assert polling.POLL_NOW.is_set()
    assert polling.interval(configured, store, NOW + 3599, 300)[0] == 300
    polling.POLL_NOW.clear()
    monkeypatch.setattr(ui, "status", lambda *a: "button")
    assert ui.poll_now(webui.Session(), {}, {}) == "button"
    assert store.get_meta(polling.NOW_KEY) == triggered and polling.POLL_NOW.is_set()
    assert TRIGGER not in raw + caplog.text + repr(configured) + ui.settings_page(webui.Session())


@pytest.mark.parametrize("auth", [None, "Bearer wrong", "Basic " + TRIGGER, "Bearer " + TRIGGER + "x"])
def test_trigger_missing_wrong_and_query_tokens_unauthorized(settings, store, caplog, auth):
    ui = make_ui(replace(settings, ui_trigger_token=TRIGGER))
    status, raw, read = request(ui, path="/trigger/poll-now?token=" + TRIGGER,
                                headers={"Authorization": auth} if auth else {})
    assert status == 401 and "WWW-Authenticate: Bearer" in raw
    assert not store.get_meta(polling.NOW_KEY) and read == 0
    assert TRIGGER not in raw + caplog.text


@pytest.mark.parametrize("ingress,configured,ip", [(False, False, "127.0.0.1"), (True, True, webui.INGRESS_IP),
                                                  (True, True, "127.0.0.1")])
def test_trigger_not_configured_or_ingress_404(settings, store, ingress, configured, ip):
    ui = make_ui(replace(settings, ui_trigger_token=TRIGGER if configured else ""), ingress=ingress)
    assert request(ui, headers={"Authorization": "Bearer " + TRIGGER}, ip=ip)[0] == 404
    assert not store.get_meta(polling.NOW_KEY)


def test_trigger_rate_limit_and_no_change_to_host_or_form_rules(settings, store, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(webui.time, "time", lambda: now[0])
    monkeypatch.setattr(webui.time, "monotonic", lambda: now[0])
    ui = make_ui(replace(settings, ui_trigger_token=TRIGGER), in_run=False)
    headers = {"Authorization": "Bearer " + TRIGGER}
    assert "within a minute" in request(ui, headers=headers)[1]
    first = store.get_meta(polling.NOW_KEY)
    now[0] += 29
    polling.POLL_NOW.clear()
    assert "already looking" in request(ui, headers=headers)[1]
    assert store.get_meta(polling.NOW_KEY) == first and not polling.POLL_NOW.is_set()
    now[0] += 1
    assert "within a minute" in request(ui, headers=headers)[1]
    assert store.get_meta(polling.NOW_KEY) != first and polling.POLL_NOW.is_set()
    assert request(ui, headers={**headers, "Host": "foreign.invalid"})[0] == 403
    assert request(ui, method="GET", headers=headers)[0] == 404
    assert request(ui, path="/poll-now", headers={"Origin": "https://foreign.invalid"})[0] == 403
    session = ui.new_session()
    assert request(ui, path="/poll-now", headers={"Cookie": f"{webui.COOKIE}={session.id}"})[0] == 403


def test_trigger_settings_secret_keep_replace_clear_and_validation(settings, monkeypatch, caplog):
    path = settings.data_dir / "settings.conf"
    path.write_text("BRIDGE_UI_TRIGGER_TOKEN=" + TRIGGER + "\n")
    configured = replace(settings, env_file=path, ui_trigger_token=TRIGGER)
    ui = make_ui(configured)
    fields = {"timezone": "UTC"}
    page = ui.settings_page(webui.Session())
    assert 'type="password" name="ui_trigger_token"' in page and TRIGGER not in page
    ui.settings_post(webui.Session(), {}, fields)
    assert config.parse_env_file(path)["BRIDGE_UI_TRIGGER_TOKEN"] == TRIGGER
    bad = "too-short-secret"
    page = ui.settings_post(webui.Session(), {}, {**fields, "ui_trigger_token": bad})
    assert "at least 24" in page and bad not in page + caplog.text
    replacement = "replacement-trigger-secret-24-characters"
    page = ui.settings_post(webui.Session(), {}, {**fields, "ui_trigger_token": replacement})
    assert config.parse_env_file(path)["BRIDGE_UI_TRIGGER_TOKEN"] == replacement
    assert replacement not in page + caplog.text
    ui.settings_post(webui.Session(), {}, {**fields, "clear_trigger_token": "1"})
    assert config.parse_env_file(path)["BRIDGE_UI_TRIGGER_TOKEN"] == ""
    monkeypatch.setenv("BRIDGE_UI_TRIGGER_TOKEN", bad)
    loaded = config.Settings.from_env()
    assert not loaded.ui_trigger_token and loaded.ui_trigger_warning
    assert bad not in loaded.ui_trigger_warning


def test_replacing_key_clears_old_date_and_same_key_validation_clears_failure(settings, monkeypatch):
    path = settings.data_dir / "settings.conf"
    path.write_text("GARMIN_SOURCE=hybrid\n")
    configured = replace(settings, env_file=path, garmin_personal_expires=date(2020, 1, 1))
    monkeypatch.setattr(webui, "PersonalGarminSource", lambda *a: SimpleNamespace(login=lambda: None))
    ui = make_ui(configured)
    p = Personal(settings.data_dir)
    p._save_failure("authorization")
    ui.settings_post(webui.Session(), {}, {"timezone": "UTC", "garmin_personal_api_key": KEY})
    assert not p.failure_path.exists()
    ui.settings_post(webui.Session(), {}, {"timezone": "UTC", "garmin_personal_api_key": "new-key"})
    assert config.parse_env_file(path)["GARMIN_PERSONAL_API_KEY_EXPIRES"] == ""


@pytest.mark.parametrize("failure", [AccountMismatch("different Garmin accounts"), requests.ConnectionError("secret"),
                                     personal.PersonalHTTPError(503)])
def test_wizard_reports_pairing_and_personal_outage(settings, monkeypatch, failure):
    def fail(*a, **k):
        raise failure
    monkeypatch.setattr(sources, "garmin_source", lambda *a, **k: SimpleNamespace(login=fail))
    messages = []
    assert wizard._garmin(wizard.Prompt(say=messages.append), replace(settings, garmin_source="personal"), Direct) is None
    text = " ".join(messages)
    assert "secret" not in text
    if isinstance(failure, AccountMismatch):
        assert "same Garmin account" in text and "Wait an hour" not in text
    else:
        assert "unavailable" in text


def test_auto_personal_allows_web_and_cli_direct_signin(settings, store, monkeypatch):
    configured = replace(settings, garmin_source="auto")
    assert config.effective_source(configured) == "personal"
    ui = make_ui(configured)
    assert 'name="email"' in ui.garmin_get(webui.Session(), {}, {})
    class SigningDirect(Direct):
        def start_login(self, email, password):
            self._call("login")
        def finish_login(self, code):
            self._call("mfa")
    ui.garmin_source = SigningDirect
    assert "Signed in" in ui.garmin_post(webui.Session(), {}, {"email": "test@example.com", "password": "test"})
    assert store.get_meta(HybridGarminSource.PROFILE_KEY) == "42"
    session = webui.Session()
    session.garmin, session.garmin_since = SigningDirect(), webui.time.time()
    assert "Signed in" in ui.garmin_mfa(session, {}, {"code": "123456"})
    monkeypatch.setenv("GARMIN_SOURCE", "auto")
    calls = []
    def direct(*a):
        calls.append(1)
        return Direct()
    monkeypatch.setattr(cli, "GarminSource", direct)
    monkeypatch.setattr(cli, "_source", lambda *a: pytest.fail("auto login selected Personal"))
    assert cli.main(["login"]) == 0 and calls == [1]
    assert 'name="email"' not in make_ui(replace(configured, garmin_source="personal")).garmin_get(
        webui.Session(), {}, {})


def test_saved_pair_rechecks_after_explicit_signin_changes_account(settings, store):
    first = HybridGarminSource(Direct(), Personal(settings.data_dir), store=store)
    first.activities(DAY, DAY)
    sources.remember_direct_profile(store, SimpleNamespace(client=SimpleNamespace(profile_id=999)))
    for g in (first, HybridGarminSource(Direct(), Personal(settings.data_dir), store=store)):
        with pytest.raises(AccountMismatch):
            g.activities(DAY, DAY)


def test_wizard_new_key_asks_for_expiry_without_old_default(settings, monkeypatch):
    configured = replace(settings, garmin_personal_expires=date(2020, 1, 1))
    p = Personal(settings.data_dir)
    p._save_failure("authorization")
    monkeypatch.setattr(wizard, "PersonalGarminSource", lambda *a: SimpleNamespace(login=lambda: None))
    questions, saved = [], []
    answers = iter(["y", "", ""])
    def ask(question):
        questions.append(question)
        return next(answers)
    prompt = wizard.Prompt(ask=ask, secret=lambda q: "new-key", say=lambda t: None)
    wizard._personal_settings(prompt, configured, lambda v: saved.append(v) or configured)
    assert saved[0]["GARMIN_PERSONAL_API_KEY_EXPIRES"] == ""
    assert all("2020-01-01" not in q for q in questions)
    assert not p.failure_path.exists()


def test_health_settings_success_does_not_clear_other_endpoint_incident(settings, store, monkeypatch):
    from garmin_intervals_bridge import health
    p = source(settings.data_dir, token(), Response(payload={}))
    p._save_failure("authorization", scope="/hrv-service/hrv/test")
    store.set_meta(personal.AUTH_WAIT_KEY, "waiting")
    report = health.probe(replace(settings, garmin_source="personal"), p, None, store)
    assert report["services"]["personal"]["ok"]
    assert store.get_meta(personal.AUTH_WAIT_KEY)
    assert any("authorization" in x["what"] for x in report["findings"])


def test_trigger_unexpected_failure_never_logs_authorization(settings, monkeypatch, caplog):
    ui = make_ui(replace(settings, ui_trigger_token=TRIGGER))
    def fail(*a):
        raise RuntimeError(TRIGGER)
    monkeypatch.setattr(ui, "_request_poll", fail)
    status, raw, _ = request(ui, headers={"Authorization": "Bearer " + TRIGGER})
    assert status == 500
    assert TRIGGER not in raw + caplog.text
