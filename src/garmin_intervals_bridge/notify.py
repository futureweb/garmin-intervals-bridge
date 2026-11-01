"""BRIDGE_NOTIFY_URL: tell tools next to the bridge that there is something new in Intervals.

Two events, each one small POST to every address (comma-separated):
- `activities` after a run that saw new activities in Intervals (any source), or uploaded or
  enriched some: `{"source": "garmin-intervals-bridge", "version": …, "event": "activities", "new": 2}`;
- `wellness` after a run that wrote wellness values, the night's included:
  `{"source": …, "version": …, "event": "wellness", "days": ["2026-11-01"]}`.
No activity data, no values, no key. intervals-summits takes `activities` as `POST /sync` and looks at
once instead of asking Intervals every few minutes itself. A tool that is down or slow never holds the
bridge up.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

from . import __version__

log = logging.getLogger(__name__)
TIMEOUT = 5


def addresses(value: str | None) -> list[str]:
    return [url for url in re.split(r"[,\s]+", str(value or "")) if url]


def new_activities(record: dict) -> int:
    """How many activities a watch or sync record saw for the first time, uploaded or enriched."""
    part = record.get("activities") if isinstance(record.get("activities"), dict) else record
    return max((v for v in (part.get(k) for k in ("new", "uploaded", "enriched")) if isinstance(v, int)), default=0)


def wellness_days(record: dict) -> list[str]:
    """The days a watch or sync record wrote wellness values for."""
    wellness = record.get("wellness")
    if not isinstance(wellness, dict):
        return []
    return sorted({d for d in wellness.get("written_days") or [] if isinstance(d, str)})


def events(record: dict) -> list[dict]:
    out = []
    if (count := new_activities(record)) > 0:
        out.append({"event": "activities", "new": count})
    if days := wellness_days(record):
        out.append({"event": "wellness", "days": days})
    return out


def notify(settings: Any, record: dict, *, session: Any = None) -> int:
    """How many notes the tools took (an event to an address counts once). Never raises."""
    urls = addresses(getattr(settings, "notify_url", None))
    taken = 0
    for event in events(record) if urls else []:
        body = {"source": "garmin-intervals-bridge", "version": __version__, **event}
        for url in urls:
            try:
                response = (session or requests).post(url, json=body, timeout=TIMEOUT)
                response.raise_for_status()
            except requests.RequestException as exc:
                log.info("Notify URL %s not reached (%s); the tool there finds out on its own", url,
                         type(exc).__name__)
                continue
            taken += 1
            log.info("Notify URL %s told: %s", url, event["event"])
    return taken
