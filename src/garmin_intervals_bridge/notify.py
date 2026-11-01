"""BRIDGE_NOTIFY_URL: tell a tool next to the bridge that there are new activities in Intervals.

intervals-summits, for one, takes it as `POST /sync` and looks at once instead of asking Intervals
every few minutes itself – the bridge looks anyway. One small POST after a run that saw or brought
new activities: a count, no activity data, no key. A tool that is down or slow never holds the
bridge up.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from . import __version__

log = logging.getLogger(__name__)
TIMEOUT = 5


def new_activities(record: dict) -> int:
    """How many activities a watch or sync record saw for the first time, uploaded or enriched."""
    part = record.get("activities") if isinstance(record.get("activities"), dict) else record
    return max((v for v in (part.get(k) for k in ("new", "uploaded", "enriched")) if isinstance(v, int)), default=0)


def notify(settings: Any, record: dict, *, session: Any = None) -> bool:
    """True when the tool took the note. Never raises."""
    url = str(getattr(settings, "notify_url", None) or "").strip()
    count = new_activities(record)
    if not url or count <= 0:
        return False
    try:
        response = (session or requests).post(
            url, json={"source": "garmin-intervals-bridge", "version": __version__, "new": count}, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.info("Notify URL not reached (%s); the tool there finds the activities on its own", type(exc).__name__)
        return False
    log.info("Notify URL told about %d new activit%s", count, "y" if count == 1 else "ies")
    return True
