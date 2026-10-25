"""Provider selection and capability checks shared by commands and scheduling."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import requests

from . import notices
from .config import effective_source, source_label
from .garmin import DAY_ENDPOINTS, EXTRA_KEYS, GarminBlocked, GarminLoginNeeded, GarminSource
from .personal import (
    AUTH_WAIT_KEY,
    HYBRID_KEYS,
    PAUSE_KEY,
    PersonalError,
    PersonalGarminSource,
    PersonalKeyNeeded,
    PersonalRateLimited,
)


def supports(source: Any, key: str) -> bool:
    if key == "wellness_files":
        return getattr(source, "has_wellness_files", True)
    if key in EXTRA_KEYS:
        return key in getattr(source, "extras_keys", EXTRA_KEYS)
    if getattr(source, "direct_unavailable", False) and key not in HYBRID_KEYS:
        return False
    return key in getattr(source, "day_keys", DAY_ENDPOINTS)


def merge_snapshot(previous: dict | None, fresh: dict) -> dict:
    """Retain whole direct datasets when Personal supplies a smaller representation.

    Retained data keeps its provenance and age; fresh Personal responses remain separately
    available for later adapters. Old archives without provenance came from direct login.
    """
    if fresh.get("final"):
        incoming_provenance = fresh.get("provenance") or {}
        fresh = {**fresh, "provenance": {k: {**incoming_provenance.get(k, {"source": "direct"}), "final": True}
                                         for k in fresh["data"]}}
    if not previous:
        return fresh
    data = dict(previous.get("data") or {})
    old_provenance = previous.get("provenance") or {}
    old_default = "personal" if previous.get("source") == "personal" else "direct"
    provenance = {k: old_provenance.get(k, {"source": old_default, "final": bool(previous.get("final"))})
                  for k in data}
    incoming = fresh.get("provenance") or {}
    for key, value in fresh["data"].items():
        old_source = provenance.get(key, {}).get("source", previous.get("source", "direct"))
        new_source = incoming.get(key, {}).get("source", fresh.get("source", "direct"))
        if (key in data and old_source == "direct" and new_source == "personal" and data[key] is not None
                and key in {"sleep", "body_composition", "weigh_ins", "hydration", "spo2"}
                and provenance.get(key, {}).get("final", previous.get("final", False))):
            provenance[key] = {**provenance[key], "retained": True}
            continue
        data[key] = value
        provenance[key] = incoming.get(key, {"source": new_source, "final": bool(fresh.get("final"))})
    errors = {k: v for k, v in (previous.get("errors") or {}).items()
              if k not in fresh["data"] and k not in fresh.get("unsupported", [])
              and k not in fresh.get("deferred", [])}
    result = {**previous, **fresh, "data": data, "errors": {**errors, **fresh.get("errors", {})}}
    if "source" in previous or "source" in fresh:
        result["source"] = fresh.get("source", "direct")
    touched = set(fresh["data"]) | set(fresh.get("errors", {}))
    for kind in ("deferred", "unsupported"):
        if kind in previous or kind in fresh:
            result[kind] = sorted((set(previous.get(kind, [])) - touched) | set(fresh.get(kind, [])))
    # Preserve the shape of direct-only archives; attach metadata when a provider uses it.
    if "provenance" in fresh or "provenance" in previous:
        result["provenance"] = provenance
    if "raw_personal" in previous or "raw_personal" in fresh:
        result["raw_personal"] = {**previous.get("raw_personal", {}), **fresh.get("raw_personal", {})}
    return result


def remember_direct_profile(store, garmin):
    """Keep the last authenticated direct identity, including explicit sign-ins and health probes."""
    profile = getattr(getattr(garmin, "client", None), "profile_id", None)
    if profile is not None and store.get_meta(HybridGarminSource.PROFILE_KEY) != str(profile):
        store.set_meta(HybridGarminSource.PROFILE_KEY, str(profile))


def _source_mode(settings, store):
    mode = effective_source(settings)
    if store.get_meta("source:mode") != mode:
        store.delete_meta("source:served:*", float("inf"))
        store.set_meta("source:mode", mode)
    # Remove composite keys left by earlier versions.
    for key in ("snapshot", "login", "account_snapshot", "activity_extras"):
        if store.get_meta("source:served:" + key):
            store.delete_meta("source:served:" + key, float("inf"))
    return mode


def source_status(settings, store) -> dict:
    """Credential health and the last actual provider of each routed dataset."""
    mode = _source_mode(settings, store)
    expiry = getattr(settings, "garmin_personal_expires", None)
    today = datetime.now(settings.timezone).date()
    return {"garmin_source": source_label(settings), "effective_source": mode,
            "trigger_warning": getattr(settings, "ui_trigger_warning", ""),
            "key_expires": expiry.isoformat() if expiry else None,
            "key_days_left": (expiry - today).days if expiry else None,
            "served_by": {key.removeprefix("source:served:"): value
                          for key, value in store.meta_items("source:served:*")},
            "providers": {name: {"last_ok": store.meta_updated(ok), "waiting": bool(store.get_meta(waiting)),
                                  **({"authorization_failed": bool(store.get_meta(AUTH_WAIT_KEY)),
                                      "paused_until": store.get_meta(PAUSE_KEY)} if name == "personal" else {})}
                          for name, ok, waiting in (("direct", "garmin_last_ok", notices.WAITING_KEY),
                                                    ("personal", "personal_last_ok", notices.PERSONAL_WAITING_KEY))
                          if mode in (name, "hybrid")}}


class AccountMismatch(PersonalError, GarminBlocked):
    """Credentials belong to different accounts; no data may be combined."""


def activity_step(settings, store, operation, *args, **kwargs):
    """A Personal failure in hybrid defers activities while direct wellness can continue."""
    try:
        return operation(*args, **kwargs)
    except PersonalError as exc:
        if effective_source(settings) != "hybrid":
            raise
        if isinstance(exc, PersonalRateLimited) and exc.resume_at is None:
            from .intervals import rate_limited
            store.set_meta(PAUSE_KEY, str(time.time() + max(900, rate_limited(exc) or 0)))
        logging.getLogger(__name__).warning("Personal activities deferred (%s); continuing direct wellness",
                                            type(exc).__name__)
        return {"deferred": type(exc).__name__}


class HybridGarminSource:
    """Route shared data to Personal and other data to direct, verifying each credential pairing.

    A verified key/profile pair survives provider outages and restarts. Only an unverified
    Personal result needs both identities; direct-only work never contacts Personal.
    """
    source = "hybrid"
    day_keys = frozenset(DAY_ENDPOINTS)
    PROFILE_KEY = "source:direct_profile_id"
    RETRY_KEY = "source:direct_retry_after"
    BLOCKED_KEY = "source:direct_blocked_until"

    def __init__(self, direct, personal, on_login_needed=None, on_direct_ok=None, *, store=None):
        self.direct, self.personal, self.store = direct, personal, store
        self._direct_waiting = bool(store and store.get_meta(notices.WAITING_KEY))
        self.on_login_needed, self.on_direct_ok = on_login_needed, on_direct_ok
        self._pair_key = "source:pair:" + personal._fingerprint[:16]
        self._profile = store.get_meta(self.PROFILE_KEY) if store else None
        self._paired = store.get_meta(self._pair_key) if store else None
        self._retry_after = float(store.get_meta(self.RETRY_KEY) or 0) if store else 0
        self._blocked_until = float(store.get_meta(self.BLOCKED_KEY) or 0) if store else 0
        self._prepared = False

    @property
    def direct_waiting(self):
        # Retry even when capability checks would otherwise skip all direct work.
        if self._direct_waiting:
            self._prepare()
        return self._direct_waiting

    @direct_waiting.setter
    def direct_waiting(self, value):
        self._direct_waiting = value

    @property
    def direct_unavailable(self):
        return time.time() < self._blocked_until or self.direct_waiting

    @property
    def client(self):
        return self.direct.client if not self.direct_unavailable else None

    @property
    def requests(self):
        return self.direct.requests + self.personal.requests

    @property
    def extras_keys(self):
        return frozenset() if self.direct_unavailable else frozenset(EXTRA_KEYS)

    @property
    def has_wellness_files(self):
        return not self.direct_unavailable

    def _record(self, operation, provider, result=None):
        if not self.store or operation == "login":
            return
        keys = (result.get("data", {}) if operation in ("snapshot", "account_snapshot") else
                (k for k in result if k in EXTRA_KEYS) if operation == "activity_extras" else (operation,))
        for key in keys:
            if self.store.get_meta("source:served:" + key) != provider:
                self.store.set_meta("source:served:" + key, provider)

    def _wait(self):
        self._direct_waiting, self._prepared = True, False
        self._retry_after = time.time() + 3600
        if self.store:
            self.store.set_meta(self.RETRY_KEY, str(self._retry_after))
            if not self.store.get_meta(notices.WAITING_KEY):
                self.store.set_meta(notices.WAITING_KEY, "yes")
        if self.on_login_needed:
            self.on_login_needed()

    def _prepare(self):
        """Prepare only direct, allowing one hourly retry while its session needs attention."""
        if time.time() < self._blocked_until:
            return
        if self._direct_waiting:
            cleared = self.store and not self.store.get_meta(notices.WAITING_KEY)
            if not cleared and time.time() < self._retry_after:
                return
        if self._prepared and not self._direct_waiting:
            return
        try:
            if self.direct.client is None or self._direct_waiting:
                self.direct.login(interactive=False)
        except GarminLoginNeeded:
            self._wait()
            return
        except AccountMismatch:
            raise
        except (GarminBlocked, requests.RequestException):
            self._block_direct()
            return
        direct_id = getattr(self.direct.client, "profile_id", None)
        if direct_id is not None:
            self._profile = str(direct_id)
            if self.store and self.store.get_meta(self.PROFILE_KEY) != self._profile:
                self.store.set_meta(self.PROFILE_KEY, self._profile)
        self._prepared, self._direct_waiting = True, False
        if self.on_direct_ok:
            self.on_direct_ok()

    def _block_direct(self):
        self._blocked_until, self._prepared = time.time() + 900, False
        if self.store:
            self.store.set_meta(self.BLOCKED_KEY, str(self._blocked_until))

    def _verify(self):
        """Verify a new key using daily userProfileId, or reuse its saved direct profile pairing."""
        if not self.personal._key:
            self.personal._key_needed(malformed=True)
        if self.store:
            self._profile = self.store.get_meta(self.PROFILE_KEY) or self._profile
        if self._paired and self._paired == self._profile:
            return
        if self._profile is None:
            self._prepare()
        if self._profile is None:
            raise GarminBlocked("Cannot verify that Personal API and direct login belong to the same account; "
                                "sign in to direct once to verify the pairing")
        pid = self.personal.profile_id()
        if pid is None:
            raise GarminBlocked("Cannot verify Personal API account identity from its daily summary")
        if str(pid) != self._profile:
            raise AccountMismatch("Personal API and direct login belong to different Garmin accounts")
        self._paired = self._profile
        if self.store:
            self.store.set_meta(self._pair_key, self._paired)

    def _direct(self, method, *args, _record=True, **kwargs):
        """Use direct without contacting Personal; missing sessions defer only direct work."""
        self._prepare()
        if self.direct_unavailable:
            return None
        try:
            result = getattr(self.direct, method)(*args, **kwargs)
        except GarminLoginNeeded:
            self._wait()
            return None
        except AccountMismatch:
            raise
        except (GarminBlocked, requests.RequestException):
            self._block_direct()
            return None
        if self.on_direct_ok:
            self.on_direct_ok()
        if _record:
            self._record(method, "direct", result)
        return result

    def _shared(self, method, *args, **kwargs):
        """Use verified Personal data; only a rejected/missing key permits direct fallback."""
        try:
            self._verify()
            result = getattr(self.personal, method)(*args, **kwargs)
        except PersonalKeyNeeded:
            result = self._direct(method, *args, **kwargs)
            if result is None:
                raise PersonalKeyNeeded() from None
            if method == "snapshot":
                result["provenance"] = {k: {"source": "direct"} for k in result["data"]}
            return result
        self._record(method, "personal", result)
        return result

    def login(self, interactive=False):
        """Each provider authenticates lazily when its first dataset is requested."""

    def activities(self, start, end):
        return self._shared("activities", start, end)

    def activity(self, activity_id):
        return self._shared("activity", activity_id)

    def original_fit(self, activity_id):
        return self._shared("original_fit", activity_id)

    def activity_extras(self, activity, keys=None):
        result = self._direct("activity_extras", activity, keys=keys)
        return result if result is not None else {"errors": {}, "deferred": list(keys or EXTRA_KEYS)}

    def wellness_files(self, day):
        return self._direct("wellness_files", day)

    def device_sync_stats(self, day):
        """The direct daily summary, only for the device's last sync: Personal stats carry none.

        Not archived and not recorded as the source of the day's stats. None while direct waits, and
        unless the direct account is the key's: another watch's sync is no evidence for this account."""
        try:
            self._prepare()
            if self.direct_unavailable:
                return None
            self._verify()
            return self._direct("snapshot", day, endpoints=("stats",), _record=False)
        except PersonalRateLimited:
            raise                                           # the caller pauses Personal for it
        except (PersonalError, GarminBlocked, requests.RequestException):
            return None

    def snapshot(self, day, endpoints=None):
        wanted = set(DAY_ENDPOINTS if endpoints is None else endpoints)
        personal = tuple(k for k in DAY_ENDPOINTS if k in wanted & HYBRID_KEYS)
        direct = tuple(k for k in DAY_ENDPOINTS if k in wanted - HYBRID_KEYS)
        out = {"date": day.isoformat(), "data": {}, "errors": {}, "provenance": {}}
        # Establish the current direct identity before accepting shared Personal data.
        raw = self._direct("snapshot", day, endpoints=direct) if direct else None
        personal_error = None
        if personal:
            try:
                out = self._shared("snapshot", day, endpoints=personal)
            except (GarminBlocked, requests.RequestException, ValueError) as exc:
                if not direct:
                    raise
                personal_error = exc
                out["errors"].update({k: type(exc).__name__ for k in personal})
                if isinstance(exc, PersonalRateLimited) and self.store and exc.resume_at is None:
                    from .intervals import rate_limited
                    self.store.set_meta(PAUSE_KEY, str(time.time() + max(900, rate_limited(exc) or 0)))
        out["source"] = "hybrid"
        if direct:
            if raw is None:
                out["deferred"] = list(direct)
            else:
                out["data"].update(raw["data"])
                out["errors"].update(raw.get("errors", {}))
                out["provenance"].update({k: {"source": "direct"} for k in raw["data"]})
        if personal_error and not out["data"]:
            raise personal_error
        for key, provenance in out["provenance"].items():
            self._record(key, provenance["source"])
        return out

    def account_snapshot(self, **kwargs):
        return self._direct("account_snapshot", **kwargs) or {"data": {}, "errors": {}, "deferred": ["account"]}


def garmin_source(settings, *, direct_factory=GarminSource, store=None, intervals=None, apply=False):
    mode = _source_mode(settings, store) if store is not None else effective_source(settings)
    if mode == "direct":
        return direct_factory(settings.token_dir, settings.garmin_request_delay)

    def today():
        return datetime.now(settings.timezone).date()

    def key_needed(*, malformed=False):
        if store is not None:
            notices.personal_key_needed(settings, store, intervals, today(), apply=apply, malformed=malformed)

    def short_token():
        if store is not None:
            notices.personal_expiry_reminders(settings, store, intervals, today(), apply=apply, short=True)

    def authorization():
        if store is not None:
            notices.personal_authorization_needed(settings, store)

    if store is not None:
        notices.personal_expiry_reminders(settings, store, intervals, today(), apply=apply)
    personal = PersonalGarminSource(settings.data_dir, settings.garmin_personal_api_key,
                                    settings.garmin_request_delay,
                                    on_ok=(lambda: notices.personal_ok(store)) if store is not None else None,
                                    on_key_needed=key_needed, on_short_token=short_token,
                                    on_authorization=authorization,
                                    expires=settings.garmin_personal_expires, today=today)
    personal.pause_remaining = (lambda: max(0, float(store.get_meta(PAUSE_KEY) or 0) - time.time())
                               if store is not None else 0)
    if mode == "personal":
        return personal

    def login_needed():
        if store is not None:
            notices.login_needed(settings, store, intervals, today(), apply=apply)

    return HybridGarminSource(direct_factory(settings.token_dir, settings.garmin_request_delay), personal,
                              on_login_needed=login_needed, store=store,
                              on_direct_ok=(lambda: notices.garmin_ok(store)) if store is not None else None)
