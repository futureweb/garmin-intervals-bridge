"""What an athlete drank and ate during an activity, as Garmin records it.

Garmin keeps these in its activity summary (`waterConsumed` in ml, `caloriesConsumed`
in kcal), entered on the watch or in the app after the activity; the FIT file has
no time-stamped intake events and no sodium or grams of carbohydrate. Intervals has
no field for either, so the bridge adds two activity fields of its own and fills
them only where the activity has no value yet.
"""
from __future__ import annotations

from typing import Any

from .charts import BRIDGE_MARK
from .mapping import number

INTAKE_FIELDS: dict[str, dict] = {
    "GarminFluidIntake": {"name": "Garmin Fluid Intake", "units": "ml", "icon": "mdi-cup-water",
                          "summary": "waterConsumed", "high": 20000},
    "GarminCaloriesConsumed": {"name": "Garmin Calories Consumed", "units": "kcal", "icon": "mdi-food-apple",
                               "summary": "caloriesConsumed", "high": 20000},
}


def intake_values(summary: dict) -> dict[str, int]:
    """{code: value} from Garmin's activity summary; 0 means nothing was logged and is left out."""
    dto = summary.get("summaryDTO") if isinstance(summary.get("summaryDTO"), dict) else summary
    out: dict[str, int] = {}
    for code, spec in INTAKE_FIELDS.items():
        value = number(dto.get(spec["summary"]), low=0, high=spec["high"])
        if value:
            out[code] = round(value)
    return out


def field_item(code: str) -> dict:
    """A private numeric activity field without a FIT source: the bridge sets its value."""
    spec = INTAKE_FIELDS[code]
    return {"type": "ACTIVITY_FIELD", "visibility": "PRIVATE", "name": spec["name"], "description": BRIDGE_MARK,
            "content": {"code": code, "name": spec["name"], "type": "numeric", "units": spec["units"],
                        "suffix": spec["units"], "icon": spec["icon"], "aggregate": "SUM", "inline": True,
                        "number_format": ".0f", "fit_session_field": None, "script": None,
                        "processes_fit_messages": False}}


def ensure_fields(intervals: Any, needed: set[str]) -> list[str]:
    """Create the activity fields that do not exist yet; existing ones (any owner) are reused."""
    have = {str((it.get("content") or {}).get("code") or "") for it in intervals.custom_items()
            if it.get("type") == "ACTIVITY_FIELD"}
    created = []
    for code in sorted(set(needed) - have):
        intervals.create_custom_item(field_item(code))
        created.append(code)
    return created


def write_intake(intervals_id: str, values: dict[str, int], intervals: Any, *, apply: bool) -> dict:
    """Fill the empty intake fields of one Intervals activity; returns what was (or would be) written."""
    if not values:
        return {}
    existing = intervals.activity(intervals_id)
    patch = {code: value for code, value in values.items() if existing.get(code) is None}
    if patch and apply:
        ensure_fields(intervals, set(patch))
        intervals.update_activity(intervals_id, patch)
    return patch
