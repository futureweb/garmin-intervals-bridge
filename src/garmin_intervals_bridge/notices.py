"""What a bridge running unseen in the background has to tell its owner.

  - A newer release on PyPI: one log line and a health finding a day, with the update
    command (BRIDGE_UPDATE_CHECK=false turns the check off; it asks pypi.org, nothing else
    is sent).
  - Garmin's login expired: everything stops until someone signs in again, and nobody
    reads a log file. One NOTE in the Intervals calendar on that day, once per episode,
    only by a bridge that may write (--apply); never deleted by the bridge.
    BRIDGE_INTERVALS_NOTES=false turns it off. And an e-mail or push message, when
    set up (alerts.py).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date
from typing import Any

import requests

from . import __version__, alerts
from .config import effective_source, key_fingerprint

log = logging.getLogger(__name__)

PYPI_URL = "https://pypi.org/pypi/garmin-intervals-bridge/json"
LOGIN_NOTE_KEY = "notice:garmin_login"
WAITING_KEY = "notice:garmin_sign_in_due"    # Garmin wants a new sign-in: set until it answers again
# Set by the web page after a Garmin sign-in: a `run` waiting for one goes on at once.
SIGNED_IN = threading.Event()
LOGIN_NOTE_NAME = "Garmin bridge: Garmin login needed"
LOGIN_NOTE_TEXT = ("The garmin-intervals-bridge can no longer sign in to Garmin Connect: the saved session "
                   "expired or was revoked. Nothing is synced until you sign in again: on the bridge's web page "
                   "('garmin-intervals-bridge ui', then Garmin) or with 'garmin-intervals-bridge login' on the "
                   "computer the bridge runs on. The bridge does not delete this note; remove it whenever you "
                   "like.")


def _version(text: str) -> tuple[int, ...] | None:
    """'0.8.1' -> (0, 8, 1); pre-releases and anything else -> None (never offered)."""
    return tuple(int(p) for p in text.split(".")) if re.fullmatch(r"\d+(\.\d+){1,3}", text or "") else None


def newer_release(current: str = __version__, *, session: Any = None, timeout: float = 10) -> str | None:
    """The newest release on PyPI if it is newer than this one; None otherwise or when unreachable."""
    try:
        response = (session or requests).get(PYPI_URL, timeout=timeout)
        response.raise_for_status()
        latest = str(response.json()["info"]["version"])
    except Exception as exc:
        log.debug("Update check failed: %s", type(exc).__name__)
        return None
    mine, theirs = _version(current), _version(latest)
    return latest if mine and theirs and theirs > mine else None


def update_finding(settings: Any, *, in_container: bool, session: Any = None) -> dict | None:
    if not getattr(settings, "update_check", False):
        return None
    latest = newer_release(session=session)
    if latest is None:
        return None
    how = ("pull the image futurewebat/garmin-intervals-bridge:latest and recreate the container" if in_container
           else "python -m pip install --upgrade garmin-intervals-bridge, then restart it "
                "(log out and in, or: garmin-intervals-bridge autostart), and an open web page too")
    return {"alert": False, "what": f"update available: {latest}",
            "detail": f"this is {__version__}; to update: {how}"}


def login_needed(settings: Any, store: Any, intervals: Any, today: date, *, apply: bool) -> bool:
    """Tell the owner once per episode: an e-mail / push message (when set up), and the note in
    the calendar (a bridge that may write); True when the note was written."""
    if not store.get_meta(WAITING_KEY):
        store.set_meta(WAITING_KEY, str(today or "yes"))
    alerts.login_needed(settings, store)
    if not apply or not getattr(settings, "intervals_notes", False) or intervals is None:
        return False
    if store.get_meta(LOGIN_NOTE_KEY):
        return False                                      # this episode is in the calendar already
    try:
        intervals.create_note(today, LOGIN_NOTE_NAME, LOGIN_NOTE_TEXT if effective_source(settings)
                              != "hybrid" else LOGIN_NOTE_TEXT.replace("Nothing is synced until you sign in again:",
                              "Personal API work continues. Direct-only data waits until you sign in again:"))
    except Exception as exc:
        log.warning("Could not put the 'Garmin login needed' note in the Intervals calendar: %s",
                    type(exc).__name__)
        return False
    store.set_meta(LOGIN_NOTE_KEY, today.isoformat())
    log.info("Put a 'Garmin login needed' note in the Intervals calendar on %s", today)
    return True


def garmin_ok(store: Any) -> None:
    """Garmin answered: the episode is over, the next expiry gets its own note."""
    if time.time() - (store.meta_updated("garmin_last_ok") or 0) >= 60:
        store.set_meta("garmin_last_ok")
    for key in (LOGIN_NOTE_KEY, alerts.LOGIN_KEY, WAITING_KEY):
        if store.get_meta(key):
            store.set_meta(key, "")


PERSONAL_WAITING_KEY = "notice:personal_key_due"
PERSONAL_NOTE_KEY = "notice:personal_key"
PERSONAL_ALERT_KEY = "alert:personal_key"
PERSONAL_TEXT = ("Personal API key needs replacement. Set a new GARMIN_PERSONAL_API_KEY in the bridge settings, "
                 "or select direct mode. Garmin may have expired or revoked the key.")


def personal_key_needed(settings: Any, store: Any, intervals: Any, today: date, *, apply: bool = False,
                        malformed: bool = False) -> None:
    """Report a rejected or missing key once per episode, separately from direct login."""
    text = ("Personal API key is malformed or empty. Check GARMIN_PERSONAL_API_KEY in the bridge settings."
            if malformed else PERSONAL_TEXT)
    if effective_source(settings) == "hybrid":
        text += (" Direct login also needs attention; sign in to resume shared data."
                 if store.get_meta(WAITING_KEY) else " Direct login serves shared data when available.")
    if not store.get_meta(PERSONAL_WAITING_KEY):
        store.set_meta(PERSONAL_WAITING_KEY, today.isoformat())
        log.warning(text)
    if alerts.channels(settings) and not store.get_meta(PERSONAL_ALERT_KEY):
        store.set_meta(PERSONAL_ALERT_KEY, "attempted")
        result = alerts.send(settings, "Garmin bridge: Personal API key needs replacement", text)
        if "sent" in result.values():
            store.set_meta(PERSONAL_ALERT_KEY, "sent")
    if apply and getattr(settings, "intervals_notes", False) and intervals is not None and not store.get_meta(
            PERSONAL_NOTE_KEY):
        try:
            store.set_meta(PERSONAL_NOTE_KEY, "attempted")
            intervals.create_note(today, "Garmin bridge: Personal API key needs replacement", text)
            store.set_meta(PERSONAL_NOTE_KEY, today.isoformat())
        except Exception:
            log.warning("Could not put the Personal API key notice in the calendar")


def personal_ok(store: Any) -> None:
    """A successful Personal data request clears only Personal credential notices."""
    from .personal import AUTH_WAIT_KEY
    if time.time() - (store.meta_updated("personal_last_ok") or 0) >= 60:
        store.set_meta("personal_last_ok")
    for key in (PERSONAL_WAITING_KEY, PERSONAL_NOTE_KEY, PERSONAL_ALERT_KEY, AUTH_WAIT_KEY):
        if store.get_meta(key):
            store.set_meta(key, "")


def personal_authorization_needed(settings: Any, store: Any) -> None:
    """A fresh token still gets 401: one visible incident, no repeated token issuance."""
    from .personal import AUTH_WAIT_KEY
    if not store.get_meta(AUTH_WAIT_KEY):
        text = ("Personal API authorization failed after token renewal (HTTP 401). "
                "The affected endpoint waits six hours; a second endpoint or settings failure pauses all Personal "
                "requests. Validate the key in settings, "
                "run personal-check or replace it to retry sooner.")
        log.warning(text)
        if alerts.channels(settings):
            alerts.send(settings, "Garmin bridge: Personal API authorization failed", text)
        store.set_meta(AUTH_WAIT_KEY, "waiting")


def personal_expiry_reminders(settings: Any, store: Any, intervals: Any, today: date, *,
                               apply: bool = False, short: bool = False) -> None:
    """Reminders once per key/date/threshold; calendar notes wait for an applying run."""
    key = getattr(settings, "garmin_personal_api_key", "")
    expiry = getattr(settings, "garmin_personal_expires", None)
    if not key or (expiry and today > expiry):
        return
    fingerprint = key_fingerprint(key)[:16]
    identity = f"{fingerprint}:{expiry or 'unknown'}"
    days = (expiry - today).days if expiry else None
    thresholds = [n for n in (14, 3) if days is not None and 0 <= days <= n]
    if short and not store.get_meta(f"personal:short:{identity}"):
        store.set_meta(f"personal:short:{identity}", "yes")
    if store.get_meta(f"personal:short:{identity}"):
        thresholds.append(1)
    for threshold in ([min(thresholds)] if thresholds else []):
        prefix = f"notice:personal_expiry:{identity}:{threshold}"
        text = ("Personal API key expires within a day (the freshly issued token has a short lifetime)."
                if threshold == 1 else f"Personal API key expires on {expiry} ({days} days left).")
        text += " Replace it in Garmin Connect and update the bridge settings."
        title = "Garmin bridge: Personal API key expiry reminder"
        if not store.get_meta(prefix + ":logged"):
            log.warning(text)
            store.set_meta(prefix + ":logged", "yes")
        if alerts.channels(settings) and not store.get_meta(prefix + ":alert"):
            store.set_meta(prefix + ":alert", "attempted")
            result = alerts.send(settings, title, text)
            if "sent" in result.values():
                store.set_meta(prefix + ":alert", "sent")
        if (apply and getattr(settings, "intervals_notes", False) and intervals is not None
                and not store.get_meta(prefix + ":note")):
            try:
                intervals.create_note(today, title, text)
                store.set_meta(prefix + ":note", "sent")
            except Exception:
                log.warning("Could not put the Personal API expiry reminder in the calendar")
