"""Daily health probe: is the Garmin session still alive, is Intervals reachable?

Run from a daily timer with OnFailure wired to the alert mail. Exit status:
  0  both services answered, or a failure is younger than the stale window
  2  a service has not answered for longer than BRIDGE_STALE_HOURS (default 24)

That keeps the noise down: one bad night does not mail, a dead Garmin
session does, once a day (the alert unit itself throttles to one mail per
six hours). The probe costs two Garmin requests (token login) and one
Intervals request per run.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Any

log = logging.getLogger(__name__)


def probe(settings: Any, garmin: Any, intervals: Any, store: Any, *, now: float | None = None) -> dict:
    now = now or time.time()
    stale_after = settings.stale_hours * 3600
    report: dict[str, Any] = {"stale_hours": settings.stale_hours, "services": {}, "exit": 0}
    for name, call in (("garmin", lambda: garmin.login(interactive=False)),
                       ("intervals", lambda: intervals.activities(date.today() - timedelta(days=1), date.today(),
                                                                   fields=["id"], limit=1))):
        entry: dict[str, Any] = {}
        try:
            call()
            store.set_meta(f"{name}_last_ok")
            entry.update(ok=True, last_ok_age_hours=0.0)
        except Exception as exc:                       # every failure counts here
            last_ok = store.meta_updated(f"{name}_last_ok")
            age = (now - last_ok) if last_ok else None
            entry.update(ok=False, error=f"{type(exc).__name__}: {exc}"[:200],
                         last_ok_age_hours=round(age / 3600, 1) if age is not None else None)
            if age is None or age > stale_after:
                entry["stale"] = True
                report["exit"] = 2
                log.error("%s has not answered for %s h (limit %d h): %s", name,
                          entry["last_ok_age_hours"] if age is not None else "ever",
                          settings.stale_hours, entry["error"])
            else:
                log.warning("%s probe failed (%s); last success %.1f h ago, inside the %d h window",
                            name, type(exc).__name__, age / 3600, settings.stale_hours)
        report["services"][name] = entry
    return report
