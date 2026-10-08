"""Small, explicit Intervals.icu REST client based on published API schema."""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .mapping import CUSTOM_FIELDS

log = logging.getLogger(__name__)


class IntervalsClient:
    def __init__(self, api_key: str, athlete_id: str = "0", session: requests.Session | None = None):
        if not api_key:
            raise ValueError("INTERVALS_API_KEY required for Intervals requests")
        self.base = f"https://intervals.icu/api/v1/athlete/{athlete_id}"
        self.session = session or requests.Session()
        self.session.auth = ("API_KEY", api_key)
        self.session.headers["User-Agent"] = "garmin-intervals-bridge/0.1"
        if session is None:
            # Only GET retries; never automatically replay a non-idempotent upload.
            retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 502, 503, 504],
                            allowed_methods=frozenset(["GET"]), respect_retry_after_header=True)
            self.session.mount("https://", HTTPAdapter(max_retries=retries))

    def _request(self, method: str, path: str, **kwargs) -> Any:
        response = self.session.request(method, f"{self.base}{path}", timeout=(10, 75), **kwargs)
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def activities(self, oldest: date, newest: date) -> list[dict]:
        result = self._request("GET", "/activities", params={"oldest": oldest.isoformat(), "newest": newest.isoformat()})
        if not isinstance(result, list):
            raise ValueError("Intervals activities API did not return a list (fail closed)")
        return result

    def nearby_activities(self, activity_day: date) -> list[dict]:
        return self.activities(activity_day - timedelta(days=1), activity_day + timedelta(days=1))

    def wellness(self, day: date) -> dict:
        # A day-scoped GET is preferred to an all-fields range where some
        # custom fields may be omitted unless explicitly requested.
        response = self.session.get(f"{self.base}/wellness/{day.isoformat()}", timeout=(10, 60))
        if response.status_code == 404:
            return {"id": day.isoformat()}
        response.raise_for_status()
        obj = response.json()
        if not isinstance(obj, dict):
            raise ValueError("Intervals wellness API returned invalid response")
        return obj

    def write_wellness(self, day: date, changes: dict) -> Any:
        if not changes:
            return None
        return self._request("PUT", f"/wellness/{day.isoformat()}", json=changes)

    def upload_fit(self, garmin_id: str, path: Path) -> dict:
        """An Intervals upload returns 201 if an activity was created, 200 otherwise.

        The response is normally an array, not an Activity object. This method
        deliberately distinguishes 'created' from 'accepted but not created'.
        """
        with path.open("rb") as f:
            response = self.session.post(
                f"{self.base}/activities",
                params={"external_id": f"garmin:{garmin_id}"},
                files={"file": (path.name, f, "application/octet-stream")},
                timeout=(10, 75),
            )
        response.raise_for_status()
        if response.status_code not in (200, 201):
            raise ValueError(f"Unexpected Intervals upload HTTP status {response.status_code}")
        payload = response.json()
        # Current OpenAPI defines UploadResponse as {"id", "icu_athlete_id",
        # "activities": [{"id", ...}]}. Older forum examples document
        # a top-level array. Accept both without losing replay protection.
        if isinstance(payload, dict):
            items = payload.get("activities")
            if not isinstance(items, list):
                raise ValueError("Unexpected Intervals upload response: UploadResponse.activities missing")
        elif isinstance(payload, list):
            items = payload
        else:
            raise ValueError("Unexpected Intervals upload response type")
        if not all(isinstance(item, dict) and item.get("id") for item in items):
            raise ValueError("Unexpected Intervals upload response: invalid ActivityId entry")
        if response.status_code == 201 and not items:
            raise ValueError("Intervals returned 201 with no created activities")
        return {"created": response.status_code == 201, "items": items}

    def custom_items(self) -> list[dict]:
        items = self._request("GET", "/custom-item")
        if not isinstance(items, list):
            raise ValueError("Intervals custom items response was not a list")
        return items

    def field_provision_plan(self) -> list[tuple[str, str]]:
        codes = {x.get("content", {}).get("code") for x in self.custom_items()
                 if isinstance(x, dict) and x.get("type") == "INPUT_FIELD" and isinstance(x.get("content"), dict)}
        return [(code, spec.name) for code, spec in CUSTOM_FIELDS.items() if code not in codes]

    def provision_fields(self, apply: bool, needed: set[str] | None = None) -> list[str]:
        """Create only missing, private numeric wellness INPUT_FIELD definitions.

        Deliberately no updates/deletes of existing definitions.
        """
        existing = self.custom_items()
        by_code = {item.get("content", {}).get("code"): item
                   for item in existing if isinstance(item, dict) and item.get("type") == "INPUT_FIELD"
                   and isinstance(item.get("content"), dict)}
        plan: list[str] = []
        for code, spec in CUSTOM_FIELDS.items():
            if needed is not None and code not in needed:
                continue
            if code in by_code:
                content = by_code[code]["content"]
                if content.get("type") != "numeric":
                    raise ValueError(f"Custom field {code} exists but is not numeric; refusing to overwrite")
                continue
            plan.append(code)
            if apply:
                content = {"code": code, "name": spec.name, "type": "numeric"}
                if spec.units:
                    content["units"] = spec.units
                self._request("POST", "/custom-item", json={
                    "name": spec.name, "type": "INPUT_FIELD", "visibility": "PRIVATE",
                    "description": "Automatically imported from Garmin Connect by garmin-intervals-bridge.",
                    "content": content,
                })
        return plan
