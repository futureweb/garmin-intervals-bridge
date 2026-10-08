"""Garmin Connect private API adapter. No private API assumptions leak to core."""
from __future__ import annotations

import getpass
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from .fit import extract_original_fit

log = logging.getLogger(__name__)


class GarminBlocked(RuntimeError):
    """Garmin refused the session or rate-limited us.

    This is a property of the *run*, not of one activity: the caller must stop
    the whole run and keep local state untouched instead of marking the
    current activity as failed and hammering the next one.
    """


def _is_blocked(exc: BaseException) -> bool:
    name = type(exc).__name__
    text = str(exc)
    return ("TooManyRequests" in name or "Authentication" in name
            or "401" in text or "429" in text)


# Source snapshot: all available fields are retained in a *local* JSON archive.
# Only confirmed scalar metrics are mapped into Intervals wellness.
DAY_ENDPOINTS = {
    "stats": "get_stats",
    "sleep": "get_sleep_data",
    "hrv": "get_hrv_data",
    "body_battery": "get_body_battery",
    "morning_readiness": "get_morning_training_readiness",
    "training_readiness": "get_training_readiness",
    "training_status": "get_training_status",
    "endurance_score": "get_endurance_score",
    "hill_score": "get_hill_score",
    "max_metrics": "get_max_metrics",
    "respiration": "get_respiration_data",
    "spo2": "get_spo2_data",
    "stress": "get_stress_data",
    "intensity_minutes": "get_intensity_minutes_data",
    "hydration": "get_hydration_data",
    "body_composition": "get_body_composition",
    "weigh_ins": "get_daily_weigh_ins",
    "fitness_age": "get_fitnessage_data",
    "race_predictions": "get_race_predictions",
    "lifestyle": "get_lifestyle_logging_data",
    "blood_pressure": "get_blood_pressure",
    "lactate_threshold": "get_lactate_threshold",
}


class GarminSource:
    def __init__(self, token_dir: Path, delay: float = 0.5):
        self.token_dir = token_dir
        self.delay = delay
        self.client: Any | None = None

    def login(self, interactive: bool = True) -> None:
        from garminconnect import Garmin, GarminConnectAuthenticationError
        self.token_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.token_dir.chmod(0o700)
        try:
            api = Garmin()
            api.login(str(self.token_dir))
            self.client = api
            return
        except (GarminConnectAuthenticationError, FileNotFoundError):
            if not interactive or not sys.stdin.isatty():
                raise RuntimeError("Garmin login needed: run 'garmin-intervals-bridge login' in an interactive terminal")
        email = input("Garmin Connect email: ").strip()
        password = getpass.getpass("Garmin Connect password: ")
        api = Garmin(email=email, password=password,
                     prompt_mfa=lambda: input("Garmin MFA code: ").strip())
        try:
            api.login(str(self.token_dir))
            self.client = api
        finally:
            password = None

    def _client(self) -> Any:
        if self.client is None:
            raise RuntimeError("Not logged in")
        return self.client

    def activities(self, start: date, end: date) -> list[dict]:
        result = self._client().get_activities_by_date(start.isoformat(), end.isoformat())
        time.sleep(self.delay)
        if not isinstance(result, list):
            raise ValueError("Unexpected Garmin activity response")
        return [x for x in result if isinstance(x, dict) and x.get("activityId") is not None]

    def activity(self, activity_id: int | str) -> dict:
        result = self._client().get_activity(str(activity_id))
        time.sleep(self.delay)
        if not isinstance(result, dict) or result.get("activityId") is None:
            raise ValueError("Unexpected Garmin activity response")
        # get_activity() nests the start time differently from the list endpoint.
        if "startTimeGMT" not in result and isinstance(result.get("summaryDTO"), dict):
            result["startTimeGMT"] = result["summaryDTO"].get("startTimeGMT")
            result.setdefault("duration", result["summaryDTO"].get("duration"))
        return result

    def original_fit(self, activity_id: int | str) -> bytes:
        api = self._client()
        try:
            raw = api.download_activity(str(activity_id), dl_fmt=api.ActivityDownloadFormat.ORIGINAL)
        except Exception as exc:
            if _is_blocked(exc):
                raise GarminBlocked(f"Garmin API blocked request: {type(exc).__name__}") from exc
            raise
        finally:
            time.sleep(self.delay)
        return extract_original_fit(raw)

    def snapshot(self, day: date) -> dict:
        """Fetch every supported day endpoint, preserving raw responses locally.

        Individual unavailable endpoints are recorded as errors; 429/auth failures
        abort the day to avoid hammering Garmin or masking an expired session.
        """
        raw: dict = {"date": day.isoformat(), "data": {}, "errors": {}}
        for key, method in DAY_ENDPOINTS.items():
            fn = getattr(self._client(), method, None)
            if fn is None:
                raw["errors"][key] = "MethodNotAvailable"
                continue
            try:
                if key == "race_predictions":
                    result = fn(startdate=day.isoformat(), enddate=day.isoformat(), _type="daily")
                elif key == "blood_pressure":
                    result = fn(day.isoformat(), day.isoformat())
                elif key == "lactate_threshold":
                    result = fn(latest=False, start_date=day.isoformat(), end_date=day.isoformat(), aggregation="daily")
                else:
                    result = fn(day.isoformat())
                raw["data"][key] = result
            except Exception as exc:
                name = type(exc).__name__
                if _is_blocked(exc):
                    raise GarminBlocked(f"Garmin API blocked request: {name}") from exc
                raw["errors"][key] = name
                log.warning("Garmin endpoint %s unavailable: %s", key, name)
            finally:
                time.sleep(self.delay)
        return raw
