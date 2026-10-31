"""What an athlete drank and ate during an activity, and the sweat it cost, as Garmin records it.

Garmin keeps these in its activity summary (`waterConsumed` and its estimate `waterEstimated`
in ml, `caloriesConsumed` in kcal), entered on the device right after the activity or in the
app later. What the device knows is in the FIT file too, in session fields the public profile
does not name: 177 (kcal consumed), 178 (estimated sweat loss, ml), 179 (fluid consumed, ml).
There are no time-stamped intake events (the device's eat and drink markers are its reminders)
and no sodium or grams of carbohydrate. Intervals has no field for any of these, so the bridge
adds activity fields of its own – plus the fluid balance and the sweat rate per hour derived
from them – and fills them only where the activity has no value yet. The sweat loss is left
to a field of the athlete's own when one reads session field 178 from the file already.
"""
from __future__ import annotations

from typing import Any, Callable

from .charts import BRIDGE_MARK
from .mapping import number

INTAKE_FIELDS: dict[str, dict] = {
    "GarminFluidIntake": {"name": "Garmin Fluid Intake", "units": "ml", "icon": "mdi-cup-water",
                          "summary": "waterConsumed", "fit": 179, "high": 20000},
    "GarminCaloriesConsumed": {"name": "Garmin Calories Consumed", "units": "kcal", "icon": "mdi-food-apple",
                               "summary": "caloriesConsumed", "fit": 177, "high": 20000},
    "GarminSweatLoss": {"name": "Garmin Sweat Loss", "units": "ml", "icon": "mdi-water",
                        "summary": "waterEstimated", "fit": 178, "high": 20000},
    # derived from the two above: drunk minus sweated, and sweat per hour of timer time
    "GarminFluidBalance": {"name": "Garmin Fluid Balance", "units": "ml", "icon": "mdi-scale-balance"},
    "GarminSweatRate": {"name": "Garmin Sweat Rate", "units": "ml/h", "icon": "mdi-water-percent",
                        "aggregate": "AVERAGE"},
}
SWEAT_FIT_FIELD = "178"
MIN_RATE_SECONDS = 600          # a shorter activity gives no sensible rate per hour


def _values(read: Callable[[str], Any], seconds: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for code, spec in INTAKE_FIELDS.items():
        if "high" in spec and (value := number(read(code), low=0, high=spec["high"])):
            out[code] = round(value)
    if "GarminFluidIntake" in out and "GarminSweatLoss" in out:
        out["GarminFluidBalance"] = out["GarminFluidIntake"] - out["GarminSweatLoss"]
    if "GarminSweatLoss" in out and (seconds := number(seconds, low=MIN_RATE_SECONDS, high=7 * 86400)):
        out["GarminSweatRate"] = round(out["GarminSweatLoss"] * 3600 / seconds)
    return out


def intake_values(summary: dict) -> dict[str, int]:
    """{code: value} from Garmin's activity summary; 0 means nothing was logged and is left out."""
    dto = summary.get("summaryDTO") if isinstance(summary.get("summaryDTO"), dict) else summary
    return _values(lambda code: dto.get(INTAKE_FIELDS[code]["summary"]), dto.get("duration"))


def fit_intake_values(messages: dict[str, list[dict]]) -> dict[str, int]:
    """The same from a decoded FIT file: what the device knows right after the activity. A multisport
    file has a session per sport whose values run on from one to the next: the largest is the
    activity's; the timer time is the sessions' sum."""
    sessions = [s for s in messages.get("session") or [] if isinstance(s, dict)]

    def found(key: Any, high: float) -> list[float]:
        return [v for s in sessions if (v := number(s.get(key), high=high)) is not None]
    seconds = found("total_timer_time", 7 * 86400)
    return _values(lambda code: max(found(INTAKE_FIELDS[code]["fit"], INTAKE_FIELDS[code]["high"]), default=None),
                   sum(seconds) if seconds else None)


def own_sweat_field(items: list[dict]) -> bool:
    """An activity field of the athlete's own already reads the sweat loss from the file."""
    return any(it.get("type") == "ACTIVITY_FIELD"
               and str((it.get("content") or {}).get("fit_session_field") or "") == SWEAT_FIT_FIELD
               and (it.get("content") or {}).get("code") != "GarminSweatLoss" for it in items)


def field_item(code: str) -> dict:
    """A private numeric activity field without a FIT source: the bridge sets its value."""
    spec = INTAKE_FIELDS[code]
    return {"type": "ACTIVITY_FIELD", "visibility": "PRIVATE", "name": spec["name"], "description": BRIDGE_MARK,
            "content": {"code": code, "name": spec["name"], "type": "numeric", "units": spec["units"],
                        "suffix": spec["units"], "icon": spec["icon"], "aggregate": spec.get("aggregate", "SUM"),
                        "inline": True, "number_format": ".0f", "fit_session_field": None, "script": None,
                        "processes_fit_messages": False}}


def ensure_fields(intervals: Any, needed: set[str], items: list[dict] | None = None) -> list[str]:
    """Create the activity fields that do not exist yet; existing ones (any owner) are reused.
    `items`: the account's custom items read once by the caller; created ones are added to it."""
    items = intervals.custom_items() if items is None else items
    have = {str((it.get("content") or {}).get("code") or "") for it in items if it.get("type") == "ACTIVITY_FIELD"}
    created = []
    for code in sorted(set(needed) - have):
        intervals.create_custom_item(field_item(code))
        items.append(field_item(code))
        created.append(code)
    return created


def write_intake(intervals_id: str, values: dict[str, int], intervals: Any, *, apply: bool,
                 items: list[dict] | None = None) -> dict:
    """Fill the empty intake fields of one Intervals activity; returns what was (or would be) written."""
    if not values:
        return {}
    items = intervals.custom_items() if items is None else items
    if own_sweat_field(items):
        values = {code: value for code, value in values.items() if code != "GarminSweatLoss"}
    existing = intervals.activity(intervals_id)
    patch = {code: value for code, value in values.items() if existing.get(code) is None}
    if patch and apply:
        ensure_fields(intervals, set(patch), items)
        intervals.update_activity(intervals_id, patch)
    return patch
