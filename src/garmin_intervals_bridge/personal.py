"""Garmin's Personal API: separate credentials, conservative adapters, no direct login."""
from __future__ import annotations

import base64
import json
import math
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

from . import __version__
from .config import key_fingerprint
from .fit import extract_original_fit, sha256
from .garmin import ACCOUNT_ENDPOINTS, DAY_ENDPOINTS, GarminBlocked, _is_transient
from .store import InstanceBusy, Store, single_instance

ROOT = "https://personal.connectapi.garmin.com"
TOKEN_PATH = "/personalapitoken-service/personal-api/personal-api-key/token"
SETTINGS_PATH = "/userprofile-service/userprofile/settings"
DAY_KEYS = frozenset(("stats", "sleep", "hrv", "body_battery", "nutrition", "hydration",
                      "body_composition", "weigh_ins", "fitness_age", "heart_rates", "spo2"))
HYBRID_KEYS = frozenset(("stats", "hrv", "body_battery", "nutrition"))
PAUSE_KEY = "personal:paused_until"
AUTH_WAIT_KEY = "notice:personal_authorization"
KEY_ALONE_MISSING = ("Training Readiness, recovery time, acute load and training status; VO2max, endurance and "
                     "hill scores and race predictions; the night's skin temperature and Body Battery change; sweat "
                     "loss; Health Snapshots and the watch's own wellness files; gear and the activity extras "
                     "(weather, splits, exercise sets); the account backup")
KEY_ALONE = (f"With the Personal API key alone these stay empty: {KEY_ALONE_MISSING}. "
             "To get them, sign in to Garmin Connect as well, with the Garmin source auto or hybrid.")
COVERAGE = ("Personal API coverage: activities and supported daily wellness; partial sleep and account data. "
            "No activity extras, gear, original wellness files, readiness or other training metrics. "
            "The archive is a partial mirror. See docs/PERSONAL_API.md.")


def validated_key(data_dir: Path) -> None:
    """A successful explicit validation, even of the same key, ends the saved suspension."""
    from . import notices
    (data_dir / "personal-api" / "failure.json").unlink(missing_ok=True)
    store = Store(data_dir)
    try:
        notices.personal_ok(store)
    finally:
        store.close()


class PersonalError(Exception):
    """A failure confined to the Personal provider."""


class PersonalConnectionError(PersonalError, requests.ConnectionError):
    pass


class PersonalTimeout(PersonalError, requests.Timeout):
    pass


class PersonalDataError(PersonalError, ValueError):
    """A malformed Personal response; other providers may still be used."""


class PersonalKeyNeeded(PersonalError, GarminBlocked):
    """The configured key is absent or the token service rejected it."""

    def __init__(self, malformed=False):
        self.malformed = malformed
        super().__init__("Personal API key is missing or malformed: check GARMIN_PERSONAL_API_KEY" if malformed else
                         "Personal API key needs replacement: set GARMIN_PERSONAL_API_KEY or select direct mode")


class PersonalHTTPError(PersonalError, requests.HTTPError):
    """Only status and Retry-After survive; no body, request, headers or credentials."""

    def __init__(self, status: int, retry_after: str | None = None):
        response = requests.Response()
        response.status_code = status
        response.url = ROOT
        if retry_after:
            response.headers["Retry-After"] = retry_after
        super().__init__(f"Personal API HTTP {status}", response=response)


class PersonalRateLimited(PersonalError, GarminBlocked):
    """A provider-wide pause, with only the safe Retry-After metadata."""

    def __init__(self, retry_after=None, *, resume_at: float | None = None):
        super().__init__("Personal API rate limited (HTTP 429)")
        self.response = PersonalHTTPError(429, retry_after).response
        self.resume_at = resume_at


class PersonalAuthorizationError(PersonalError, GarminBlocked):
    """A data request was refused, even with a fresh token; never fall back to direct."""


class PersonalTokenError(PersonalError, GarminBlocked):
    """Token issuance failed for the provider; back off before trying any dataset again."""


class PersonalTransientTokenError(PersonalTokenError, requests.ConnectionError):
    """A short token-service outage, including its persisted backoff; no failure alarm."""


def _timestamp(value: Any) -> float:
    if isinstance(value, (int, float)) or isinstance(value, str) and value.replace(".", "", 1).isdigit():
        number = float(value)
        return number / 1000 if number > 100_000_000_000 else number
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()


def jwt_claims(token: str) -> dict:
    """Decode scheduling metadata only. These claims are not authenticated identity."""
    try:
        parts = token.split(".")
        if len(parts) != 3 or not all(parts):
            return {}
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return payload if isinstance(payload, dict) else {}
    except (ValueError, UnicodeError):
        return {}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def parse_token(payload: Any, now: float) -> dict:
    """Live text/plain JWT, plus JSON access-token envelopes for compatibility."""
    try:
        if isinstance(payload, str):
            token = payload.strip()
            if len(token.split(".")) != 3 or not all(token.split(".")):
                raise ValueError
        elif isinstance(payload, dict):
            token = next((payload[k] for k in ("access_token", "accessToken", "token") if payload.get(k)), None)
        else:
            raise ValueError
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError
        claims = jwt_claims(token)
        exp, iat = _number(claims.get("exp")), _number(claims.get("iat"))
        lifetime = exp - iat if exp is not None and iat is not None else None
        expires = [now + min(lifetime, 86400) if lifetime is not None else
                   min(exp, now + 86400) if exp is not None else now + 86400]
        if isinstance(payload, dict):
            for key in ("expires_in", "expiresIn"):
                if payload.get(key) is not None:
                    expires.append(now + float(payload[key]))
            for key in ("expires_at", "expiresAt", "expiration", "expirationTime", "expiry", "expiryTime",
                        "expirationDate", "keyExpiresAt", "key_expires_at"):
                if payload.get(key) is not None:
                    expires.append(_timestamp(payload[key]))
        if any(not math.isfinite(x) or x <= now for x in expires):
            raise ValueError
        until = min(expires)
        lifetime = exp - iat if exp is not None and iat is not None else until - now
        return {"token": token, "obtained": now, "usable_until": until - min(60, (until - now) / 2),
                "lifetime_hours": lifetime / 3600,
                "short_lifetime": exp is not None and iat is not None and 0 < lifetime < 86400 - 300}
    except (ValueError, TypeError, OverflowError, StopIteration):
        raise ValueError("Unrecognized Personal API token response; "
                         "run personal-check to inspect field types") from None


def _day(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("localDate") or value.get("date")
    try:
        return date.fromisoformat(str(value or "")[:10]).isoformat()
    except ValueError:
        return ""


def adapt(key: str, payload: Any, day: date) -> Any:
    """Only documented conversions; unselected entries remain in raw_personal."""
    target = day.isoformat()
    expected = (dict, list) if key in ("fitness_age", "body_composition", "weigh_ins") else (
        list if key in ("hydration", "body_battery") else dict)
    if payload is not None and not isinstance(payload, expected):
        raise ValueError(f"Unexpected Personal API {key} response shape")
    if key == "sleep":
        return {"dailySleepDTO": payload} if payload and any(payload.values()) else None
    if key == "hydration":
        return next((x for x in payload or [] if isinstance(x, dict) and _day(x.get("calendarDate")) == target),
                    None)
    if key == "fitness_age":
        if isinstance(payload, dict):
            return payload or None
        entries = [x for x in payload or [] if isinstance(x, dict)]
        dated = [x for x in entries if len(_day(x.get("lastUpdated"))) == 10
                 and _day(x["lastUpdated"]) <= target]
        if dated:
            return max(dated, key=lambda x: str(x["lastUpdated"]))
        return entries[0] if len(entries) == 1 and not entries[0].get("lastUpdated") else None
    if key in ("body_composition", "weigh_ins"):
        entries = []
        rows = payload or []
        if isinstance(payload, dict):
            if not isinstance(payload.get("dailyWeightSummaries"), list):
                raise ValueError("Unexpected Personal API weight response shape")
            rows = []
            for summary in payload["dailyWeightSummaries"]:
                if isinstance(summary, dict) and _day(summary.get("summaryDate")) == target:
                    for item in summary.get("allWeightMetrics") or []:
                        if isinstance(item, dict):
                            rows.append({**item, "calendarDate": target})
        for item in rows:
            if not isinstance(item, dict) or _day(item.get("calendarDate") or item.get("date")) != target:
                continue
            item = {**item, "calendarDate": target}
            weight = item.get("weight")
            # The spec says kilograms; the live API answers grams, like the direct login. A body
            # weight below 1000 can only be kilograms.
            if isinstance(weight, (float, int)) and not isinstance(weight, bool) and 0 < weight < 1000:
                item["weight"] = weight * 1000
            entries.append(item)
        # The mapper takes the first matching day: latest local measurement first.
        entries.sort(key=lambda x: str(x.get("date") or x.get("timestampGMT") or ""), reverse=True)
        return {"dateWeightList": entries}
    return payload


class PersonalGarminSource:
    source = "personal"
    day_keys = DAY_KEYS
    extras_keys = frozenset()
    has_wellness_files = False
    client = None                             # never mistaken for a successful direct login

    def __init__(self, data_dir: Path, api_key: str, delay: float = 0.5, *, session: Any = None,
                 on_ok=None, on_key_needed=None, on_short_token=None, on_authorization=None,
                 expires: date | None = None, today=None):
        self.cache_dir = data_dir / "personal-api"
        self.cache_path = self.cache_dir / "token.json"
        self._key = api_key
        self._fingerprint = key_fingerprint(api_key)
        self.delay, self.requests = delay, 0
        self.session = session or requests.Session()
        if session is None:
            # Explicit no-op auth prevents requests from loading .netrc, while retaining proxy/CA settings.
            self.session.auth = lambda request: request
            self.session.headers["User-Agent"] = f"garmin-intervals-bridge/{__version__}"
        self.on_ok, self.on_key_needed = on_ok, on_key_needed
        self.on_short_token, self.on_authorization = on_short_token, on_authorization
        self.expires, self.today = expires, today or date.today
        self.failure_path = self.cache_dir / "failure.json"
        self._notified = set()
        self._settings = None
        self.pause_remaining = lambda: 0
        self.diagnostics: dict = {"key": "set" if api_key else "key not set", "token_statuses": [],
                                  "token_content_type": None, "token_response_type": None,
                                  "token_kind": None, "token_fields": {},
                                  "jwt_claim_names": [], "token_lifetime_hours": None, "settings_statuses": []}

    def _http(self, method: str, path: str, authorization: str, *, pace: bool = True, **kwargs):
        self._check_pause()
        self.requests += 1
        try:
            response = self.session.request(method, ROOT + path, headers={"Authorization": authorization},
                                            timeout=(10, 75), allow_redirects=False, **kwargs)
        except requests.Timeout:
            raise PersonalTimeout("Personal API timed out") from None
        except requests.RequestException:
            raise PersonalConnectionError("Personal API connection failed") from None
        finally:
            if pace:
                time.sleep(self.delay)
        if path == TOKEN_PATH:
            self.diagnostics["token_statuses"].append(response.status_code)
            self.diagnostics["token_content_type"] = response.headers.get("Content-Type", "")
        elif path == SETTINGS_PATH:
            self.diagnostics["settings_statuses"].append(response.status_code)
        return response

    def _key_needed(self, malformed=False):
        if self.on_key_needed and "key" not in self._notified:
            self._notified.add("key")
            self.on_key_needed(malformed=malformed)
        raise PersonalKeyNeeded(malformed)

    def _cached(self, now: float) -> dict | None:
        try:
            obj = json.loads(self.cache_path.read_text())
            if (obj["key_fingerprint"] == self._fingerprint and isinstance(obj["token"], str)
                    and obj["token"] and not any(c.isspace() for c in obj["token"])
                    and now < float(obj["usable_until"])
                    and now >= float(obj["obtained"]) and float(obj["usable_until"]) <= float(obj["obtained"]) + 86400):
                self.cache_path.chmod(0o600)
                return obj
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def _failure(self) -> dict:
        try:
            failure = json.loads(self.failure_path.read_text())
            if failure.get("key_fingerprint") != self._fingerprint:
                return {}
            # Read the former single-slot format as well, so upgrades keep their backoff.
            scope = failure.get("scope")
            entries = failure.get("failures", {"*" if scope == SETTINGS_PATH else self._scope(scope): failure})
            return {scope: entry for scope, entry in entries.items() if time.time() < entry.get("retry_until", 0)}
        except (OSError, ValueError, AttributeError, TypeError):
            return {}

    @staticmethod
    def _scope(path: str | None) -> str:
        # Dates and activity IDs identify requests, not different authorization endpoints.
        return re.sub(r"/\d+(?:-\d+-\d+)?(?=/|$)", "/:id", path) if path else "*"

    def _save_failure(self, kind: str, *, scope: str | None = None) -> None:
        with single_instance(self.cache_dir, ("failure",), timeout=10):
            entries = self._failure()
            endpoint = self._scope(scope) if kind == "authorization" else "*"
            entries[endpoint] = {"kind": kind, "retry_until": time.time() + (
                6 * 3600 if kind == "authorization" else 900 if kind == "token_transient" else 3600)}
            if kind == "authorization" and (scope == SETTINGS_PATH or
                    len([e for e in entries.values() if e["kind"] == "authorization"]) > 1):
                entries["*"] = dict(entries[endpoint])
            Store.atomic_save(self.failure_path, json.dumps({"key_fingerprint": self._fingerprint,
                                                            "failures": entries}).encode())

    def _clear_failure(self, *, scope=None, force=False) -> dict:
        with single_instance(self.cache_dir, ("failure",), timeout=10):
            entries = {} if force else self._failure()
            entries.pop(self._scope(scope), None)
            if entries:
                Store.atomic_save(self.failure_path, json.dumps({"key_fingerprint": self._fingerprint,
                                                                "failures": entries}).encode())
            else:
                self.failure_path.unlink(missing_ok=True)
            return entries

    def _blocked(self, scope=None) -> None:
        entries = self._failure()
        failure = entries.get("*", entries.get(self._scope(scope), {}))
        if failure.get("kind") == "authorization":
            if self.on_authorization and "authorization" not in self._notified:
                self._notified.add("authorization")
                self.on_authorization()
            raise PersonalAuthorizationError("Personal API authorization failed (HTTP 401); "
                                             "retrying in six hours; validate the key to retry sooner")
        if failure.get("kind") in ("key", "configuration"):
            self._key_needed(failure["kind"] == "configuration")
        if failure.get("kind") == "token":
            raise PersonalTokenError("Personal API token service unavailable; retrying after an hour")
        if failure.get("kind") == "token_transient":
            raise PersonalTransientTokenError("Personal API token service temporarily unavailable; "
                                              "retrying after 15 minutes")

    def _check_pause(self) -> None:
        remaining = self.pause_remaining()
        if remaining:
            raise PersonalRateLimited(str(remaining), resume_at=time.time() + remaining)

    def _token(self, *, rejected: str | None = None, force: bool = False, scope=None) -> str:
        self._check_pause()
        if not self._key:
            self._key_needed(malformed=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_dir.chmod(0o700)
        if not force:
            self._blocked(scope)
        cached = self._cached(time.time())
        if cached and not force and cached["token"] != rejected:
            return self._use_token(cached)
        issued = False
        try:
            # Wait only when renewal is needed, then reuse a competing renewal's result.
            with single_instance(self.cache_dir, ("token",), timeout=90):
                if not force:
                    self._blocked(scope)
                cached = self._cached(time.time())
                if cached and not force and cached["token"] != rejected:
                    token = cached
                else:
                    issued = True
                    response = self._http("POST", TOKEN_PATH, "ApiKey " + self._key, pace=False)
                    if response.status_code in (400, 401):
                        self._save_failure("configuration" if response.status_code == 400 else "key")
                        self._key_needed(response.status_code == 400)
                    if 400 <= response.status_code < 500 and response.status_code != 429:
                        self._save_failure("token")
                        raise PersonalTokenError(f"Personal API token endpoint refused access "
                                                 f"(HTTP {response.status_code}); retrying after an hour")
                    self._check(response)
                    content_type = response.headers.get("Content-Type", "")
                    self.diagnostics["token_content_type"] = content_type
                    body = response.content.strip()
                    if "text/plain" in content_type or ("json" not in content_type
                                                        and not body.startswith((b"{", b"["))
                                                        and body.count(b".") == 2):
                        payload = response.content.decode("utf-8", errors="replace").strip()
                    else:
                        payload = self._json(response)
                    self.diagnostics["token_response_type"] = type(payload).__name__
                    if isinstance(payload, dict):
                        self.diagnostics["token_fields"] = {k: type(v).__name__ for k, v in payload.items()}
                    token = {**parse_token(payload, time.time()), "key_fingerprint": self._fingerprint}
                    Store.atomic_save(self.cache_path, (json.dumps(token) + "\n").encode())
                    if force or self._failure().get("*", {}).get("kind") != "authorization":
                        self._clear_failure(force=force)
        except requests.RequestException as exc:
            from .intervals import transient
            if transient(exc):
                self._save_failure("token_transient")
                raise PersonalTransientTokenError("Personal API token service temporarily unavailable; "
                                                  "retrying after 15 minutes") from None
            self._save_failure("token")
            raise PersonalTokenError("Personal API token service unavailable; retrying after an hour") from None
        except ValueError:
            self._save_failure("token")
            raise PersonalTokenError("Personal API token service unavailable or returned an invalid token; "
                                     "retrying after an hour; run personal-check for HTTP statuses") from None
        except InstanceBusy:
            raise GarminBlocked("Personal API token renewal still in progress; try again later") from None
        finally:
            if issued:
                time.sleep(self.delay)             # outside the token lock, including failed requests
        return self._use_token(token)

    def _use_token(self, token: dict) -> str:
        jwt = len(token["token"].split(".")) == 3
        self.diagnostics.update(token_kind="JWT" if jwt else "opaque",
                                jwt_claim_names=sorted(jwt_claims(token["token"])),
                                token_lifetime_hours=token.get("lifetime_hours"))
        if token.get("short_lifetime") and self.on_short_token and "short" not in self._notified:
            self._notified.add("short")
            self.on_short_token()
        return token["token"]

    @staticmethod
    def _check(response):
        if response.status_code == 429:
            raise PersonalRateLimited(response.headers.get("Retry-After"))
        if not 200 <= response.status_code < 300:
            raise PersonalHTTPError(response.status_code, response.headers.get("Retry-After"))

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError:
            raise PersonalDataError("Personal API returned invalid JSON") from None

    def _get(self, path: str, *, binary: bool = False, **params):
        token = self._token(scope=path)
        response = self._http("GET", path, "Bearer " + token, params=params)
        if response.status_code == 401:
            token = self._token(rejected=token, scope=path)
            response = self._http("GET", path, "Bearer " + token, params=params)
        if response.status_code == 401:
            self._save_failure("authorization", scope=path)
            self._blocked(path)
        self._check(response)
        result = (None if response.status_code == 204 else response.content if binary else self._json(response))
        failure = self._clear_failure(scope=path) if self.failure_path.exists() else {}
        if self.on_ok and not failure and "ok" not in self._notified:
            self._notified.add("ok")
            self.on_ok()
        return result

    def settings(self) -> dict:
        if self._settings is None:
            self._settings = self._get(SETTINGS_PATH)
        return self._settings

    def profile_id(self) -> Any:
        """The live daily summary's userProfileId equals the direct account's profile_id."""
        stats = self._get("/usersummary-service/usersummary/daily", calendarDate=self.today().isoformat())
        return stats.get("userProfileId") if isinstance(stats, dict) else None

    def login(self, interactive: bool = False):
        self._settings = self._get(SETTINGS_PATH)

    def check(self) -> dict:
        """Always issue a token so its contract can be inspected; retain no response values."""
        try:
            self._token(force=True)
            self._get(SETTINGS_PATH)
        except (GarminBlocked, requests.RequestException, ValueError):
            pass                                      # statuses/types only, including failures
        return self.diagnostics

    def activities(self, start: date, end: date) -> list[dict]:
        out, offset, seen_pages = [], 0, set()
        for _ in range(2000):
            page = self._get("/activitylist-service/activities/search/activities", start=offset, limit=99,
                             startDate=start.isoformat(), endDate=end.isoformat())
            if not isinstance(page, list):
                raise PersonalDataError("Unexpected Personal API activity list")
            if not page:
                return out
            signature = tuple(str(x.get("activityId")) for x in page if isinstance(x, dict))
            if signature in seen_pages:
                raise PersonalDataError("Personal API repeated an activity page")
            seen_pages.add(signature)
            offset += len(page)
            for item in page:
                if isinstance(item, dict) and item.get("activityId") is not None:
                    item = dict(item)
                    if "manualActivity" not in item and "isManualActivity" in item:
                        item["manualActivity"] = item["isManualActivity"]
                    local_day = _day(item.get("startTimeLocal") or item.get("startTimeGMT"))
                    if start.isoformat() <= local_day <= end.isoformat():
                        out.append(item)
            if len(page) < 99:
                return out
        raise PersonalDataError("Personal API activity pagination did not finish")

    @staticmethod
    def _id(activity_id):
        value = str(activity_id)
        if not value.isdecimal():
            raise ValueError("Garmin activity ID must be numeric")
        return value

    def activity(self, activity_id) -> dict:
        path = "/activity-service/activity/" + self._id(activity_id)
        raw = self._get(path)
        if not isinstance(raw, dict) or raw.get("activityId") is None:
            raise PersonalDataError("Unexpected Personal API activity detail")
        result = dict(raw)
        if isinstance(result.get("summaryDTO"), dict):
            result.setdefault("startTimeGMT", result["summaryDTO"].get("startTimeGMT"))
            result.setdefault("duration", result["summaryDTO"].get("duration"))
        result["_bridge"] = self._provenance(path)
        return result

    def original_fit(self, activity_id) -> bytes:
        gid = self._id(activity_id)
        path = "/download-service/files/activity/" + gid
        data = extract_original_fit(self._get(path, binary=True))
        provenance = {**self._provenance(path), "sha256": sha256(data)}
        Store.atomic_save(self.cache_dir.parent / "fits" / f"{gid}.provenance.json",
                          (json.dumps(provenance) + "\n").encode())
        return data

    def activity_extras(self, activity: dict, keys=None) -> dict:
        from .garmin import EXTRA_KEYS
        return {"unsupported": list(keys or EXTRA_KEYS), "errors": {}, "source": "personal"}

    def wellness_files(self, day: date):
        raise NotImplementedError("Personal API does not provide original wellness files")

    @staticmethod
    def _provenance(path: str, params: dict | None = None) -> dict:
        return {"source": "personal", "path": path, "params": params or {}, "adapter": 1,
                "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def snapshot(self, day: date, endpoints=None) -> dict:
        d = day.isoformat()
        routes = {
            "stats": ("/usersummary-service/usersummary/daily", {"calendarDate": d}),
            "sleep": ("/sleep-service/sleep/dailySleep", {"date": d}),
            "hrv": (f"/hrv-service/hrv/{d}", {}),
            "body_battery": ("/wellness-service/wellness/bodyBattery/reports/daily", {"startDate": d, "endDate": d}),
            "nutrition": (f"/nutrition-service/food/logs/{d}", {}),
            "hydration": (f"/usersummary-service/stats/hydration/daily/{d}/{d}", {}),
            "body_composition": (f"/weight-service/weight/range/{d}/{d}", {"includeAll": "true"}),
            "weigh_ins": (f"/weight-service/weight/range/{d}/{d}", {"includeAll": "true"}),
            "fitness_age": (f"/fitnessage-service/fitnessage/v2/{d}", {"fullHistoryScan": "true"}),
            "heart_rates": ("/wellness-service/wellness/dailyHeartRate", {"date": d}),
            "spo2": (f"/wellness-service/wellness/daily/spo2acclimation/{d}", {}),
        }
        wanted = set(DAY_ENDPOINTS if endpoints is None else endpoints)
        out = {"date": d, "source": "personal", "data": {}, "errors": {}, "provenance": {},
               "raw_personal": {}, "unsupported": sorted(wanted - self.day_keys)}
        consecutive, fetched = 0, {}
        for key in routes:
            if key not in wanted:
                continue
            path, params = routes[key]
            try:
                if path not in fetched:
                    fetched[path] = self._get(path, **params)
                payload = fetched[path]
                out["data"][key] = adapt(key, payload, day)
                if out["data"][key] != payload:
                    out["raw_personal"][key] = payload
                out["provenance"][key] = self._provenance(path, params)
                consecutive = 0
            except PersonalAuthorizationError:
                out["errors"][key] = "PersonalAuthorizationError 401"
            except GarminBlocked:
                raise
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 429:
                    raise
                out["errors"][key] = type(exc).__name__ + (f" {status}" if status else "")
                if _is_transient(exc) or isinstance(exc, (requests.ConnectionError, requests.Timeout)):
                    consecutive += 1
                    if consecutive >= 3:
                        raise GarminBlocked("Personal API endpoints unavailable") from None
        return out

    def account_snapshot(self, history_start=None, today=None) -> dict:
        routes = {"user_settings": (SETTINGS_PATH, {}),
                  "heart_rate_zones": ("/biometric-service/heartRateZones/v2", {"generateDefaults": "true"})}
        out = {"source": "personal", "taken": datetime.now(timezone.utc).isoformat(), "data": {}, "errors": {},
               "unsupported": sorted(set(ACCOUNT_ENDPOINTS) - routes.keys()) + ["account histories", "gear", "goals"],
               "provenance": {}, "raw_personal": {}}
        for key, (path, params) in routes.items():
            try:
                out["data"][key] = self._get(path, **params)
                out["provenance"][key] = self._provenance(path, params)
            except PersonalAuthorizationError:
                out["errors"][key] = "PersonalAuthorizationError 401"
            except GarminBlocked:
                raise
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                out["errors"][key] = type(exc).__name__ + (f" {status}" if status else "")
        return out
