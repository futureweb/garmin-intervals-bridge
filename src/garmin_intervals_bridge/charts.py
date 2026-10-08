"""Private Intervals fitness charts for the values the bridge adds.

Intervals stores charts as custom items of type FITNESS_CHART whose `content`
lists plots. A plot of a custom wellness field embeds the field's own item
(`filter: "customInput"`), so the field must exist before the chart is made.
Native fields are referenced by Intervals' chart ids (`hrv`, `resting_hr`,
`sleep`, `weight`, `calories`, `readiness`); only ids seen in real chart
definitions are used here.
"""
from __future__ import annotations

import secrets
from typing import Any

# Native wellness chart ids, read from Intervals' app bundle (`{id:..., fn:..., scale:...}`
# definitions), so none of them is a guess: hrv, resting_hr, avg_sleeping_hr, readiness,
# steps, respiration, spo2, calories, kcal_consumed, carbohydrates, protein, fatTotal,
# weight, body_fat, vo2max, sleep, sleep_score, hydration_volume, systolic, diastolic.
# Each chart: name, title, y-axis label, height, plots. A plot is either
#   ("custom", code, text, type, agg, days, fill, stroke, stack[, scale[, filter]])
# Intervals draws one y axis per distinct plot `scale` (a second axis from two scales, a third
# from three), so plots with different units get different scale names. `filter` formats the
# legend value: dec0/dec1/dec2, hours, percent, interval_time (seconds as a duration).
#   ("native", field, scale, filter, text, type, agg, days, fill, stroke)
# Every chart is named "Garmin Bridge: <topic>": the chart picker's search finds them all with
# "Garmin" or "Bridge", and they sit together in its alphabetical list. `aliases` are earlier
# names of the same chart; a bridge chart found under one is renamed in place, never duplicated.
CHARTS: list[dict[str, Any]] = [
    {
        "name": "Garmin Bridge: Readiness & recovery",
        "aliases": ("Garmin readiness & recovery",),
        "title": "Readiness, recovery time, acute load (Garmin)",
        "y": "Readiness",
        "y2": "Recovery (min)",
        "height": 180,
        "plots": [
            ("custom", "GarminTrainingReadiness", "Ready", "bars", "none",
             None, "#009E0040", "#009E00FF", "", "score"),
            ("custom", "GarminRecoveryTimeMinutes", "Recov.", "line", "none",
             None, "#D6272800", "#D62728FF", "", "minutes"),
            ("custom", "GarminAcuteLoad", "Load7d", "line", "moving_avg", 7, "#1F77B400", "#1F77B4FF", "", "load"),
        ],
    },
    {
        "name": "Garmin Bridge: Sleep stages",
        "aliases": ("Garmin sleep stages",),
        "title": "Deep / light / REM / awake (minutes)",
        "y": "Minutes",
        "height": 180,
        "plots": [
            ("custom", "GarminSleepDeepMinutes", "Deep", "bars", "none", None, "#1F3A93CC", "#1F3A93FF", "sleep"),
            ("custom", "GarminSleepLightMinutes", "Light", "bars", "none", None, "#4C8BE2CC", "#4C8BE2FF", "sleep"),
            ("custom", "GarminSleepREMMinutes", "REM", "bars", "none", None, "#9B59B6CC", "#9B59B6FF", "sleep"),
            ("custom", "GarminSleepAwakeMinutes", "Awake", "bars", "none", None, "#E67E22CC", "#E67E22FF", "sleep"),
        ],
    },
    {
        "name": "Garmin Bridge: Stress & Body Battery",
        "aliases": ("Garmin stress & Body Battery",),
        "title": "Stress average, Body Battery max/min, charged/drained",
        "y": "0–100",
        "height": 180,
        "plots": [
            ("custom", "BodyBatteryMax", "BBmax", "line", "none", None, "#009E0030", "#009E00FF", ""),
            ("custom", "BodyBatteryMin", "BBmin", "line", "none", None, "#D6272830", "#D62728FF", ""),
            ("custom", "GarminStressAvg", "Stress", "bars", "none", None, "#FF7F0E40", "#FF7F0EFF", ""),
            ("custom", "GarminBodyBatteryCharged", "Charge", "dot", "none", None, "#2CA02C66", "#2CA02CFF", ""),
            ("custom", "GarminBodyBatteryDrained", "Drain", "dot", "none", None, "#8C564B66", "#8C564BFF", ""),
        ],
    },
    {
        "name": "Garmin Bridge: Nutrition intake vs. burn",
        "aliases": ("Nutrition: intake vs. burn (Garmin)",),
        "title": "kcal consumed (logged in Garmin) vs. Garmin's total daily burn (BMR + active); active part as bars",
        "y": "kcal",
        "height": 180,
        "plots": [
            # `kcal_consumed` follows Intervals' chart-id pattern (restingHR -> resting_hr,
            # spO2 -> spo2); it is the one id here not yet seen in a real chart definition.
            ("native", "kcal_consumed", "kcal", "dec0", "In",
             "line", "none", None, "#2CA02C00", "#2CA02CFF"),
            ("custom", "GarminTotalCalories", "Out", "line", "none", None, "#D6272800", "#D62728FF", ""),
            ("custom", "GarminActiveCalories", "Active", "bars", "none", None, "#D6272833", "#D6272880", ""),
        ],
    },
    {
        "name": "Garmin Bridge: Nutrition macros",
        "aliases": ("Nutrition: macros (Garmin)",),
        "title": "Energy from carbohydrates / protein / fat (g x 4/4/9); the percentage is the share of intake energy",
        "y": "kcal",
        "height": 180,
        "plots": [
            ("custom", "GarminCarbsKcal", "Carbs", "bars", "none", None, "#70663180", "#706631FF", "food"),
            ("custom", "GarminProteinKcal", "Prot.", "bars", "none", None, "#9B033280", "#9B0332FF", "food"),
            ("custom", "GarminFatKcal", "Fat", "bars", "none", None, "#DA6C0B80", "#DA6C0BFF", "food"),
        ],
    },
    {
        "name": "Garmin Bridge: Endurance & hill scores",
        "aliases": ("Garmin scores",),
        "title": "Endurance score, hill score (strength / endurance)",
        "y": "Endurance",
        "y2": "Hill",
        "height": 180,
        "plots": [
            ("custom", "GarminEnduranceScore", "Endur.", "line", "none", None, "#1F77B400", "#1F77B4FF", "",
             "endurance"),
            ("custom", "GarminHillScore", "Hill", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", "", "hill"),
            ("custom", "GarminHillStrength", "HillStr", "dot", "none", None, "#FF7F0E66", "#FF7F0E88", "", "hill"),
            ("custom", "GarminHillEndurance", "HillEnd", "dot", "none", None, "#FFBB7866", "#FFBB7888", "", "hill"),
        ],
    },
    {
        "name": "Garmin Bridge: VO2max & fitness age",
        "aliases": ("Garmin VO2max & fitness age",),
        "title": "Daily VO2max estimate (Garmin wellness: run / bike), fitness age",
        "y": "ml/kg/min",
        "y2": "Years",
        "height": 180,
        "plots": [
            ("native", "vo2max", "vo2max", "dec1", "VO2run", "line", "none", None, "#1F77B400", "#1F77B4FF"),
            ("custom", "GarminVO2MaxCycling", "VO2bike", "line", "none", None, "#9467BD00", "#9467BDFF", "", "vo2max"),
            # Fitness age moves in whole steps from day to day; a 7-day average shows the trend.
            ("custom", "GarminFitnessAge", "FitAge", "line", "moving_avg", 7, "#7F7F7F00", "#7F7F7FFF", "", "years"),
        ],
    },
    {
        # Each prediction gets its own axis: two series per chart, two scales per chart. Sharing
        # one axis flattens every line to the edge, since the four times differ by a factor of ten.
        "name": "Garmin Bridge: Race predictions 5K / 10K",
        "aliases": ("Garmin race predictions",),
        "title": "Predicted race times: 5K (left axis), 10K (right axis)",
        "y": "5K",
        "y2": "10K",
        "height": 150,
        "plots": [
            ("custom", "GarminPredicted5KSeconds", "5K", "line", "none", None, "#2CA02C00", "#2CA02CFF", "",
             "5k", "interval_time"),
            ("custom", "GarminPredicted10KSeconds", "10K", "line", "none", None, "#1F77B400", "#1F77B4FF", "",
             "10k", "interval_time"),
        ],
    },
    {
        "name": "Garmin Bridge: Race predictions half / marathon",
        "aliases": ("Garmin race predictions (half / marathon)",),
        "title": "Predicted race times: half marathon (left axis), marathon (right axis)",
        "y": "Half",
        "y2": "Marathon",
        "height": 150,
        "plots": [
            ("custom", "GarminPredictedHalfSeconds", "Half", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", "",
             "half", "interval_time"),
            ("custom", "GarminPredictedMarathonSeconds", "Mara.", "line", "none",
             None, "#D6272800", "#D62728FF", "", "marathon", "interval_time"),
        ],
    },
    {
        "name": "Garmin Bridge: Intensity minutes & sweat loss",
        "aliases": ("Garmin activity & hydration",),
        "title": "Intensity minutes (left axis) and estimated sweat loss (right axis)",
        "y": "Minutes",
        "y2": "Litres",
        "height": 180,
        "plots": [
            ("custom", "GarminIntensityModerateMinutes", "Mod", "bars", "none", None, "#1F77B466", "#1F77B4FF", "im",
             "minutes"),
            ("custom", "GarminIntensityVigorousMinutes", "Vig", "bars", "none", None, "#D6272866", "#D62728FF", "im",
             "minutes"),
            ("custom", "GarminSweatLossLitres", "Sweat", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", "", "L"),
        ],
    },
    {
        "name": "Garmin Bridge: Hydration & sweat loss",
        "aliases": ("Garmin hydration & sweat loss",),
        "title": "Estimated sweat loss per day; hydration if you log it (litres)",
        "y": "Litres",
        "height": 160,
        "plots": [
            ("custom", "GarminSweatLossLitres", "Sweat", "bars", "none", None, "#FF7F0E66", "#FF7F0EFF", ""),
            ("native", "hydration_volume", "L", "dec1", "Hydr.", "line", "none", None, "#17BECF40", "#17BECFFF"),
        ],
    },
    {
        "name": "Garmin Bridge: Skin temperature",
        "aliases": ("Garmin skin temperature",),
        "title": "Overnight skin temperature deviation (°C)",
        "y": "°C",
        "height": 140,
        "plots": [
            ("custom", "GarminSkinTempDeviationC", "SkinΔ", "bars", "none", None, "#E377C266", "#E377C2FF", ""),
            ("custom", "GarminSkinTempDeviationC", "Skin7d", "line", "moving_avg", 7, "#E377C200", "#E377C2FF", ""),
        ],
    },
    {
        "name": "Garmin Bridge: HRV detail",
        "aliases": ("HRV detail (Garmin)",),
        "title": "Overnight HRV, 5-min high, 7-day average",
        "y": "ms",
        "height": 180,
        "plots": [
            ("native", "hrv", "ms", "dec0", "HRV", "bars", "none", None, "#1F77B44D", "#1F77B4FF"),
            ("custom", "GarminHRV5MinHigh", "5min", "dot", "none", None, "#9467BD66", "#9467BDFF", ""),
            ("custom", "GarminHRV7DayAvg", "7d avg", "line", "none", None, "#2CA02C00", "#2CA02CFF", ""),
        ],
    },
]


def _plot(index: int, spec: tuple, inputs: dict[str, dict]) -> dict | None:
    kind = spec[0]
    if kind == "custom":
        _, code, text, ptype, agg, days, fill, stroke, stack = spec[:9]
        scale = spec[9] if len(spec) > 9 else None       # one y axis per distinct scale
        item = inputs.get(code)
        if item is None:
            return None                       # field not created yet: plot would be empty
        decimals = any(k in code for k in ("Litres", "TempDeviation", "VO2Max", "FitnessAge"))
        filt = spec[10] if len(spec) > 10 else ("dec1" if decimals else "dec0")
        plot = {"id": index, "field": code, "filter": filt, "customInput": item,
                "text": text, "title": item.get("name") or code, "type": ptype, "agg": agg,
                "fill": fill, "stroke": stroke, "strokeWidth": 1, "radius": 3, "gauge": True,
                "scale": scale, "stack": stack, "band": 0, "min": None, "extras": [], "filters": [],
                "markerValue": "right-inline"}
    else:
        _, field, scale, filt, text, ptype, agg, days, fill, stroke = spec
        plot = {"id": index, "field": field, "filter": filt, "scale": scale, "text": text,
                "title": text, "type": ptype, "agg": agg, "fill": fill, "stroke": stroke,
                "strokeWidth": 1, "radius": 3, "gauge": True, "stack": "", "band": 0,
                "extras": [], "filters": [], "markerValue": "right-inline"}
    if agg == "moving_avg":
        plot["aggArgs"] = {"days": days}
    return plot


BRIDGE_MARK = "Created by garmin-intervals-bridge for the values it syncs."


_PLOT_KEYS = ("field", "text", "type", "agg", "filter", "stack", "fill", "stroke", "aggArgs", "scale")


def _normalise(plots: list[dict]) -> list[tuple]:
    """What makes a plot definition: the keys the bridge sets, minus the embedded item."""
    return [tuple((k, str(p.get(k))) for k in _PLOT_KEYS) for p in plots]


def plan_charts(custom_items: list[dict]) -> dict:
    """Charts to create or complete.

    A chart is created when no chart of that name exists and at least one of
    its fields does. A chart the bridge created earlier (recognised by its
    description) is updated when fields it lacked have appeared since, e.g.
    after a backfill with every endpoint. Charts the athlete made themselves
    are never touched, even if the name matches.
    """
    existing = {it.get("name"): it for it in custom_items if it.get("type") == "FITNESS_CHART"}
    inputs = {it["content"]["code"]: it for it in custom_items
              if it.get("type") == "INPUT_FIELD" and isinstance(it.get("content"), dict)
              and it["content"].get("code")}
    create, update, skipped = [], [], {}
    for chart in CHARTS:
        plots = [p for i, spec in enumerate(chart["plots"], 1) if (p := _plot(i, spec, inputs))]
        if chart.get("stack"):
            for plot in plots:
                plot["stack"] = chart["stack"]
        missing = [spec[1] for spec in chart["plots"] if spec[0] == "custom" and spec[1] not in inputs]
        present = existing.get(chart["name"])
        if present is None:
            # The same chart under an earlier name, if it is ours: rename it instead of adding one.
            present = next((existing[a] for a in chart.get("aliases", ())
                            if a in existing and existing[a].get("description") == BRIDGE_MARK), None)
        if present is not None:
            if present.get("description") != BRIDGE_MARK:
                skipped[chart["name"]] = "exists and was not created by the bridge"
                continue
            current_plots = (present.get("content") or {}).get("plots", [])
            have = {p.get("field") for p in current_plots}
            gained = [p["field"] for p in plots if p["field"] not in have]
            renamed = present.get("name") != chart["name"]
            if _normalise(current_plots) == _normalise(plots) and not gained and not renamed:
                skipped[chart["name"]] = "already up to date"
                continue
            content = dict(present.get("content") or {})
            content["plots"] = plots
            content["name"] = chart["name"]
            content["title"] = chart["title"]
            content["yAxisLabel"] = chart["y"]
            content["y2AxisLabel"] = chart.get("y2")
            update.append({"id": present["id"], "name": chart["name"], "type": "FITNESS_CHART",
                           "visibility": present.get("visibility", "PRIVATE"),
                           "description": BRIDGE_MARK, "content": content,
                           "_gained_fields": gained, "_missing_fields": missing,
                           "_renamed_from": present.get("name") if renamed else None})
            continue
        if not plots:
            skipped[chart["name"]] = f"none of its fields exist yet: {missing}"
            continue
        content = {"id": secrets.token_hex(4), "name": chart["name"], "title": chart["title"],
                   "plots": plots, "height": chart["height"], "yAxisLabel": chart["y"],
                   "yAxisMin": None, "yAxisMax": None, "y2AxisLabel": chart.get("y2"), "y2AxisMin": None,
                   "y2AxisMax": None, "y3AxisMin": None, "y3AxisMax": None, "stackTo100Percent": None}
        create.append({"name": chart["name"], "type": "FITNESS_CHART", "visibility": "PRIVATE",
                       "description": BRIDGE_MARK, "content": content, "_missing_fields": missing})
    return {"create": create, "update": update, "skipped": skipped}


def setup_charts(intervals: Any, *, apply: bool) -> dict:
    plan = plan_charts(intervals.custom_items())
    out = {"apply": apply, "skipped": plan["skipped"],
           "charts": [{"name": c["name"], "plots": [p["field"] for p in c["content"]["plots"]],
                       "fields_missing": c["_missing_fields"]} for c in plan["create"]],
           "updates": [{"name": c["name"], "id": c["id"], "adds": c["_gained_fields"],
                        "fields_missing": c["_missing_fields"], "renamed_from": c["_renamed_from"]}
                       for c in plan["update"]],
           "created": [], "updated": []}
    if apply:
        for chart in plan["create"]:
            body = {k: v for k, v in chart.items() if not k.startswith("_")}
            result = intervals.create_custom_item(body)
            out["created"].append({"name": chart["name"], "id": (result or {}).get("id")})
        for chart in plan["update"]:
            body = {k: v for k, v in chart.items() if not k.startswith("_")}
            intervals.update_custom_item(chart["id"], body)
            out["updated"].append({"name": chart["name"], "id": chart["id"], "adds": chart["_gained_fields"]})
        out["reindexed"] = assign_indexes(intervals)
    return out


def assign_indexes(intervals: Any) -> list[int]:
    """Give the bridge's charts unique indexes. POST leaves new items at index 0 and
    Intervals' chart picker does not list colliding items; the UI reindexes on its own
    creations, the API client has to do it itself."""
    items = intervals.custom_items()
    taken = [it.get("index") or 0 for it in items]
    top = max(taken) if taken else 0
    seen: set[int] = set()
    fix = []
    for it in sorted((i for i in items if i.get("description") == BRIDGE_MARK), key=lambda i: i.get("id") or 0):
        idx = it.get("index") or 0
        if idx == 0 or idx in seen:
            top += 1
            it = dict(it)
            it["index"] = top
            fix.append(it)
        seen.add(it["index"])
    if fix:
        intervals.reorder_custom_items(fix)
    return [it["id"] for it in fix]
