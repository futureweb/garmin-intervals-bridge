"""Synthetic Personal API responses only. No Garmin credentials or network."""
import io
import json
import stat
import sys
import time
import zipfile
from datetime import date, datetime, timezone

import pytest
import requests
from test_fit import build_fit

from garmin_intervals_bridge import cli, personal
from garmin_intervals_bridge.fit import InvalidFIT
from garmin_intervals_bridge.garmin import GarminBlocked
from garmin_intervals_bridge.mapping import map_wellness
from garmin_intervals_bridge.personal import (
    PersonalAuthorizationError,
    PersonalGarminSource,
    PersonalHTTPError,
    PersonalKeyNeeded,
    PersonalRateLimited,
    PersonalTokenError,
    adapt,
    parse_token,
)
from garmin_intervals_bridge.store import Store, single_instance

DAY = date(2026, 10, 20)
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc).timestamp()
KEY = "synthetic-personal-key"
TOKEN = "synthetic-access-token"


class Response:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code, self.payload, self.content = status, payload, content
        self.headers = headers or {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert url.startswith(personal.ROOT + "/")
        assert kwargs["allow_redirects"] is False
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def source(tmp_path, *responses, key=KEY):
    return PersonalGarminSource(tmp_path, key, delay=0, session=Session(*responses))


def token(value=TOKEN, **kwargs):
    return Response(payload={"access_token": value, **kwargs})


@pytest.mark.parametrize("field", ["access_token", "accessToken", "token"])
@pytest.mark.parametrize("expiry,expected", [({}, 86400), ({"expires_in": 300}, 300),
    ({"expiresIn": "900"}, 900), ({"expires_at": NOW + 7200}, 7200),
    ({"expiresAt": "2026-10-24T01:00:00Z"}, 3600), ({"expiry": (NOW + 500) * 1000}, 500),
    ({"expires_in": 200000}, 86400), ({"expiresIn": 3600, "keyExpiresAt": NOW + 100}, 100)])
def test_token_contract_names_and_expiry(field, expiry, expected):
    result = parse_token({field: TOKEN, **expiry}, NOW)
    assert result["token"] == TOKEN and result["obtained"] == NOW
    assert result["usable_until"] == NOW + expected - min(60, expected / 2)


@pytest.mark.parametrize("payload", [None, TOKEN, [], {}, {"accessToken": {}}, {"token": "bad\nsecret"},
    {"token": TOKEN, "expiresIn": -1}, {"token": TOKEN, "expires_in": "NaN"},
    {"token": TOKEN, "expiry": "bad-secret-value"}])
def test_unknown_token_contract_is_clear_and_never_echoes_values(payload):
    with pytest.raises(ValueError, match="personal-check") as err:
        parse_token(payload, NOW)
    assert TOKEN not in str(err.value) and "bad-secret" not in str(err.value)


def test_cache_private_atomic_and_invalidated_by_rotation(tmp_path, monkeypatch):
    monkeypatch.setattr(personal.time, "time", lambda: NOW)
    g = source(tmp_path, token(), Response(payload={}))
    g.login()
    cache = json.loads(g.cache_path.read_text())
    assert set(cache) == {"token", "obtained", "usable_until", "key_fingerprint", "lifetime_hours", "short_lifetime"}
    assert KEY not in g.cache_path.read_text()
    if sys.platform != "win32":                       # POSIX file modes
        assert stat.S_IMODE(g.cache_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(g.cache_path.stat().st_mode) == 0o600
    assert not list(g.cache_dir.glob(".partial-*"))
    assert not (tmp_path / "tokens").exists()
    again = source(tmp_path, Response(payload={}))
    again.login()
    assert [x[0] for x in again.session.calls] == ["GET"]
    rotated = source(tmp_path, token("rotated-token"), Response(payload={}), key="replacement-key")
    rotated.login()
    assert [x[0] for x in rotated.session.calls] == ["POST", "GET"]
    assert json.loads(g.cache_path.read_text())["token"] == "rotated-token"
    assert g.session.calls[0][2]["headers"] == {"Authorization": "ApiKey " + KEY}
    assert g.session.calls[1][2]["headers"] == {"Authorization": "Bearer " + TOKEN}


def test_renew_before_expiry_and_after_corrupt_cache(tmp_path, monkeypatch):
    now = [NOW]
    monkeypatch.setattr(personal.time, "time", lambda: now[0])
    g = source(tmp_path, token(expires_in=120), Response(payload={}), token("renewed"), Response(payload={}))
    g.login()
    now[0] += 61
    g.login()
    assert [x[0] for x in g.session.calls] == ["POST", "GET", "POST", "GET"]
    g.cache_path.write_text("broken")
    other = source(tmp_path, token(), Response(payload={}))
    other.login()
    assert other.requests == 2


def test_concurrent_renewal_never_issues_two_tokens(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    g = source(tmp_path, token(), Response(payload={}))
    other = source(tmp_path, Response(payload={}))
    entered, release = Event(), Event()
    original = g.session.request
    def request(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    g.session.request = request
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(g.login)
        assert entered.wait(5)
        second = pool.submit(other.login)
        release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert [c[0] for c in g.session.calls] == ["POST", "GET"]
    assert [c[0] for c in other.session.calls] == ["GET"]
    # An already valid token is usable even while another command holds the renewal lock.
    with single_instance(g.cache_dir, ("token",)):
        assert source(tmp_path)._token() == TOKEN


def test_401_renews_and_replays_once(tmp_path):
    g = source(tmp_path, token(), Response(401), token("fresh-token"), Response(payload={}))
    g.login()
    assert [x[0] for x in g.session.calls] == ["POST", "GET", "POST", "GET"]
    assert g.session.calls[-1][2]["headers"]["Authorization"] == "Bearer fresh-token"
    bad = source(tmp_path, Response(401), token("second-token"), Response(401))
    with pytest.raises(PersonalAuthorizationError, match="401"):
        bad.login()
    assert bad.requests == 3


@pytest.mark.parametrize("status", [401])
def test_rejected_key_has_own_condition(tmp_path, status):
    g = source(tmp_path, Response(status, {"error": KEY}))
    with pytest.raises(PersonalKeyNeeded, match="key needs replacement") as error:
        g.login()
    assert KEY not in str(error.value) and TOKEN not in str(error.value)
    assert len(g.session.calls) == 1


def test_missing_key_does_not_make_requests(tmp_path):
    g = source(tmp_path, key="")
    with pytest.raises(PersonalKeyNeeded):
        g.login()
    assert g.requests == 0


@pytest.mark.parametrize("status,error", [(403, PersonalHTTPError), (429, PersonalRateLimited),
                                         (500, PersonalHTTPError), (302, PersonalHTTPError)])
def test_data_failures_are_not_credentials_or_fallbacks(tmp_path, status, error):
    g = source(tmp_path, token(), Response(status, {"secret": KEY}, headers={"Retry-After": "2400"}))
    with pytest.raises(error) as exc:
        g.login()
    assert len(g.session.calls) == 2
    assert KEY not in str(exc.value) and TOKEN not in str(exc.value)
    if status == 429:
        with StoreContext(tmp_path) as store:
            assert cli.pause_on_rate_limit(store, exc.value) == 2400
            assert cli.intervals_paused(store) == 0
            assert float(store.get_meta(personal.PAUSE_KEY)) > time.time() + 2399


class StoreContext:
    def __init__(self, path):
        self.store = Store(path)

    def __enter__(self):
        return self.store

    def __exit__(self, *args):
        self.store.close()


def test_connection_errors_and_bad_json_do_not_echo_http_details(tmp_path):
    g = source(tmp_path, requests.ConnectionError(KEY))
    with pytest.raises(PersonalTokenError) as error:
        g.login()
    assert KEY not in str(error.value) and error.value.__suppress_context__
    g = source(tmp_path / "other", Response(payload=ValueError(KEY)))
    with pytest.raises(PersonalTokenError) as error:
        g.login()
    assert KEY not in str(error.value)


def test_personal_check_only_field_names_types_and_statuses_even_for_unknown_contract(tmp_path):
    g = source(tmp_path, Response(payload={"unknownToken": TOKEN, "seconds": 123, "other": {"value": KEY}}))
    result = g.check()
    assert result["token_statuses"] == [200]
    assert result["token_fields"] == {"unknownToken": "str", "seconds": "int", "other": "dict"}
    assert result["settings_statuses"] == []
    assert TOKEN not in json.dumps(result) and KEY not in json.dumps(result)
    g = source(tmp_path, token(), Response(payload={"private": "profile-value"}))
    result = g.check()
    assert result["settings_statuses"] == [200] and result["token_kind"] == "opaque"


def test_adapters_select_days_convert_only_documented_units_and_preserve_missing():
    sleep = {"sleepTimeSeconds": 27000, "avgHeartRate": 48}
    assert adapt("sleep", sleep, DAY) == {"dailySleepDTO": sleep}
    assert adapt("hydration", [{"calendarDate": "2026-10-19", "valueInML": 1000}], DAY) is None
    water = {"calendarDate": str(DAY), "valueInML": 1500}
    assert adapt("hydration", [water], DAY) == water
    assert adapt("fitness_age", [{"fitnessAge": 33}], DAY) == {"fitnessAge": 33}
    assert adapt("fitness_age", [{"fitnessAge": 33}, {"fitnessAge": 34}], DAY) is None
    assert adapt("fitness_age", [{"lastUpdated": "2026-10-21", "fitnessAge": 31},
                                  {"lastUpdated": "2026-10-19", "fitnessAge": 32}], DAY)["fitnessAge"] == 32
    assert adapt("fitness_age", [{"lastUpdated": "2026-10-21", "fitnessAge": 31}], DAY) is None
    rows = [{"calendarDate": str(DAY), "date": str(DAY) + "T06:00:00", "weight": 71.2, "bodyFat": 17},
            {"calendarDate": str(DAY), "date": str(DAY) + "T18:00:00", "weight": 72.1},
            {"calendarDate": "2026-10-19", "weight": 70}]
    composition = adapt("body_composition", rows, DAY)
    assert [x["weight"] for x in composition["dateWeightList"]] == [72100, 71200]
    assert rows[0]["weight"] == 71.2                       # original response retained in kg
    native, custom = map_wellness({"data": {"body_composition": composition, "sleep": adapt("sleep", sleep, DAY),
                                            "hydration": water}}, DAY, date(2026, 10, 21), "all")
    assert native["weight"] == 72.1 and native["hydrationVolume"] == 1.5
    assert "restingHR" not in native and "GarminSkinTempDeviationC" not in custom


def test_snapshot_capabilities_provenance_and_204(tmp_path):
    g = source(tmp_path, token(), Response(204), Response(payload=[{"calendarDate": str(DAY), "weight": 70}]))
    raw = g.snapshot(DAY, endpoints=("hrv", "body_composition", "weigh_ins", "training_readiness"))
    assert raw["data"]["hrv"] is None and raw["errors"] == {}
    assert raw["unsupported"] == ["training_readiness"] and raw["source"] == "personal"
    assert raw["raw_personal"]["body_composition"][0]["weight"] == 70
    assert raw["data"]["body_composition"]["dateWeightList"][0]["weight"] == 70000
    assert raw["provenance"]["hrv"]["source"] == "personal"
    assert g.requests == 3                            # both weight views share one read
    assert KEY not in json.dumps(raw) and TOKEN not in json.dumps(raw)


def test_supported_endpoint_outages_keep_existing_failure_policy(tmp_path):
    g = source(tmp_path, token(), Response(500), Response(500), Response(500))
    with pytest.raises(GarminBlocked, match="unavailable"):
        g.snapshot(DAY, endpoints=("stats", "sleep", "hrv"))
    g = source(tmp_path, Response(404), Response(204))
    raw = g.snapshot(DAY, endpoints=("stats", "hrv"))
    assert raw["errors"] == {"stats": "PersonalHTTPError 404"} and raw["data"] == {"hrv": None}
    g = source(tmp_path, Response(429))
    with pytest.raises(PersonalRateLimited):
        g.snapshot(DAY)
    assert g.requests == 1


def test_activity_pagination_detail_and_original_validation(tmp_path):
    item = {"activityId": 1, "startTimeLocal": str(DAY), "isManualActivity": True}
    g = source(tmp_path, token(), Response(payload=[item]),
               Response(payload={"activityId": 1, "summaryDTO": {"startTimeGMT": str(DAY), "duration": 30}}))
    assert g.activities(DAY, DAY) == [{**item, "manualActivity": True}]
    assert [c[2]["params"]["start"] for c in g.session.calls[1:3]] == [0]
    detail = g.activity(1)
    assert detail["duration"] == 30 and detail["_bridge"]["source"] == "personal"
    assert "raw" not in detail["_bridge"]
    fit = build_fit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("original.fit", fit)
    g.session.responses += [Response(content=buf.getvalue()), Response(content=b"not FIT")]
    assert g.original_fit(1) == fit
    assert json.loads((tmp_path / "fits/1.provenance.json").read_text())["source"] == "personal"
    with pytest.raises(InvalidFIT):
        g.original_fit(2)
    assert not (tmp_path / "fits/2.provenance.json").exists()
    with pytest.raises(ValueError, match="numeric"):
        g.activity("../token")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_cache_hardens_existing_permissions(tmp_path):
    g = source(tmp_path, token(), Response(payload={}))
    g.login()
    g.cache_dir.chmod(0o755)
    g.cache_path.chmod(0o644)
    after = source(tmp_path, Response(payload={}))
    after.login()
    assert stat.S_IMODE(g.cache_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(g.cache_path.stat().st_mode) == 0o600
    assert json.loads(g.cache_path.read_text())["usable_until"] > time.time()


@pytest.mark.parametrize("key,payload", [("hydration", {}), ("body_composition", {}), ("sleep", []), ("hrv", [])])
def test_unexpected_shapes_are_errors_not_empty_measurements(key, payload):
    with pytest.raises(ValueError, match="response shape"):
        adapt(key, payload, DAY)


def test_invalid_dates_and_ambiguous_fitness_age_are_not_selected():
    assert adapt("fitness_age", [{"lastUpdated": "0000000000", "fitnessAge": 35}], DAY) is None
    assert adapt("hydration", [{"calendarDate": "not-a-date", "valueInML": 1500}], DAY) is None


def test_token_rate_limit_uses_the_same_pause_and_date_header(tmp_path):
    from email.utils import formatdate
    when = time.time() + 3600
    g = source(tmp_path, Response(429, headers={"Retry-After": formatdate(when, usegmt=True)}))
    with pytest.raises(PersonalRateLimited) as error:
        g.login()
    with StoreContext(tmp_path) as store:
        assert 3500 < cli.pause_on_rate_limit(store, error.value) <= 3600
    assert g.requests == 1


def test_repeated_activity_page_stops_without_retry_storm(tmp_path):
    item = {"activityId": 1, "startTimeLocal": str(DAY)}
    g = source(tmp_path, token(), Response(payload=[item] * 99), Response(payload=[item] * 99))
    with pytest.raises(ValueError, match="repeated"):
        g.activities(DAY, DAY)
    assert g.requests == 3
