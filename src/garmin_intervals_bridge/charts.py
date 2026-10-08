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

# Each chart: name, title, y-axis label, height, plots. A plot is either
#   ("custom", code, text, type, agg, days, fill, stroke, stack)
#   ("native", field, scale, filter, text, type, agg, days, fill, stroke)
CHARTS: list[dict[str, Any]] = [
    {
        "name": "Garmin readiness & recovery",
        "title": "Readiness, recovery time, acute load (Garmin)",
        "y": "Score / hours",
        "height": 180,
        "plots": [
            ("custom", "GarminTrainingReadiness", "Training readiness", "bars", "none",
             None, "#009E0040", "#009E00FF", ""),
            ("custom", "GarminRecoveryTimeMinutes", "Recovery time (min)", "line", "none",
             None, "#D6272800", "#D62728FF", ""),
            ("custom", "GarminAcuteLoad", "Acute load", "line", "moving_avg", 7, "#1F77B400", "#1F77B4FF", ""),
        ],
    },
    {
        "name": "Garmin sleep stages",
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
        "name": "Garmin stress & Body Battery",
        "title": "Stress average, Body Battery max/min, charged/drained",
        "y": "0–100",
        "height": 180,
        "plots": [
            ("custom", "BodyBatteryMax", "BB max", "line", "none", None, "#009E0030", "#009E00FF", ""),
            ("custom", "BodyBatteryMin", "BB min", "line", "none", None, "#D6272830", "#D62728FF", ""),
            ("custom", "GarminStressAvg", "Stress avg", "bars", "none", None, "#FF7F0E40", "#FF7F0EFF", ""),
            ("custom", "GarminBodyBatteryCharged", "Charged", "dot", "none", None, "#2CA02C66", "#2CA02CFF", ""),
            ("custom", "GarminBodyBatteryDrained", "Drained", "dot", "none", None, "#8C564B66", "#8C564BFF", ""),
        ],
    },
    {
        "name": "Nutrition: intake vs. burn (Garmin)",
        "title": "kcal consumed (logged in Garmin) vs. calories burned",
        "y": "kcal",
        "height": 180,
        "plots": [
            # `kcal_consumed` follows Intervals' chart-id pattern (restingHR -> resting_hr,
            # spO2 -> spo2); it is the one id here not yet seen in a real chart definition.
            ("native", "kcal_consumed", "kcal", "dec0", "kcal consumed",
             "bars", "none", None, "#2CA02C66", "#2CA02CFF"),
            ("native", "calories", "kcal", "dec0", "Calories burned", "line", "none", None, "#D6272800", "#D62728FF"),
        ],
    },
    {
        "name": "Garmin scores",
        "title": "Endurance score, hill score (strength / endurance)",
        "y": "Score",
        "height": 180,
        "plots": [
            ("custom", "GarminEnduranceScore", "Endurance", "line", "none", None, "#1F77B400", "#1F77B4FF", ""),
            ("custom", "GarminHillScore", "Hill", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", ""),
            ("custom", "GarminHillStrength", "Hill strength", "dot", "none", None, "#FF7F0E66", "#FF7F0E88", ""),
            ("custom", "GarminHillEndurance", "Hill endurance", "dot", "none", None, "#FFBB7866", "#FFBB7888", ""),
        ],
    },
    {
        "name": "Garmin VO2max & fitness age",
        "title": "VO2max run / bike, fitness age",
        "y": "ml/kg/min · years",
        "height": 180,
        "plots": [
            ("native", "vo2max", "vo2max", "dec1", "VO2max (run)", "line", "none", None, "#1F77B400", "#1F77B4FF"),
            ("custom", "GarminVO2MaxCycling", "VO2max (bike)", "line", "none", None, "#9467BD00", "#9467BDFF", ""),
            ("custom", "GarminFitnessAge", "Fitness age", "line", "none", None, "#7F7F7F00", "#7F7F7FFF", ""),
        ],
    },
    {
        "name": "Garmin race predictions",
        "title": "Predicted 5K / 10K / half / marathon (seconds)",
        "y": "Seconds",
        "height": 180,
        "plots": [
            ("custom", "GarminPredicted5KSeconds", "5K", "line", "none", None, "#2CA02C00", "#2CA02CFF", ""),
            ("custom", "GarminPredicted10KSeconds", "10K", "line", "none", None, "#1F77B400", "#1F77B4FF", ""),
            ("custom", "GarminPredictedHalfSeconds", "Half", "line", "none", None, "#FF7F0E00", "#FF7F0EFF", ""),
            ("custom", "GarminPredictedMarathonSeconds", "Marathon", "line", "none",
             None, "#D6272800", "#D62728FF", ""),
        ],
    },
    {
        "name": "Garmin activity & hydration",
        "title": "Intensity minutes, sweat loss, hydration",
        "y": "Minutes · litres",
        "height": 180,
        "plots": [
            ("custom", "GarminIntensityModerateMinutes", "Moderate min", "bars", "none",
             None, "#1F77B466", "#1F77B4FF", "im"),
            ("custom", "GarminIntensityVigorousMinutes", "Vigorous min", "bars", "none",
             None, "#D6272866", "#D62728FF", "im"),
            ("custom", "GarminSweatLossLitres", "Sweat loss (L)", "dot", "none", None, "#17BECF66", "#17BECFFF", ""),
            ("native", "hydration_volume", "litres", "dec1", "Hydration (L)",
             "line", "none", None, "#17BECF00", "#17BECF88"),
        ],
    },
    {
        "name": "Garmin skin temperature",
        "title": "Overnight skin temperature deviation (°C)",
        "y": "°C",
        "height": 140,
        "plots": [
            ("custom", "GarminSkinTempDeviationC", "Skin temp Δ", "bars", "none", None, "#E377C266", "#E377C2FF", ""),
            ("custom", "GarminSkinTempDeviationC", "7d", "line", "moving_avg", 7, "#E377C200", "#E377C2FF", ""),
        ],
    },
    {
        "name": "HRV detail (Garmin)",
        "title": "Overnight HRV, 5-min high, 7-day average",
        "y": "ms",
        "height": 180,
        "plots": [
            ("native", "hrv", "ms", "dec0", "HRV rMSSD", "bars", "none", None, "#1F77B44D", "#1F77B4FF"),
            ("custom", "GarminHRV5MinHigh", "5-min high", "dot", "none", None, "#9467BD66", "#9467BDFF", ""),
            ("custom", "GarminHRV7DayAvg", "Garmin 7d avg", "line", "none", None, "#2CA02C00", "#2CA02CFF", ""),
        ],
    },
]


def _plot(index: int, spec: tuple, inputs: dict[str, dict]) -> dict | None:
    kind = spec[0]
    if kind == "custom":
        _, code, text, ptype, agg, days, fill, stroke, stack = spec
        item = inputs.get(code)
        if item is None:
            return None                       # field not created yet: plot would be empty
        plot = {"id": index, "field": code, "filter": "customInput", "customInput": item,
                "text": text, "title": item.get("name") or code, "type": ptype, "agg": agg,
                "fill": fill, "stroke": stroke, "strokeWidth": 1, "radius": 3, "gauge": True,
                "scale": None, "stack": stack, "band": 0, "min": None, "extras": [], "filters": [],
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


def plan_charts(custom_items: list[dict]) -> dict:
    """Charts to create: those not yet present by name, with plots for fields that exist."""
    existing = {it.get("name") for it in custom_items if it.get("type") == "FITNESS_CHART"}
    inputs = {it["content"]["code"]: it for it in custom_items
              if it.get("type") == "INPUT_FIELD" and isinstance(it.get("content"), dict) and it["content"].get("code")}
    create, skipped = [], {}
    for chart in CHARTS:
        if chart["name"] in existing:
            skipped[chart["name"]] = "already exists"
            continue
        plots = [p for i, spec in enumerate(chart["plots"], 1) if (p := _plot(i, spec, inputs))]
        missing = [spec[1] for spec in chart["plots"] if spec[0] == "custom" and spec[1] not in inputs]
        if not plots:
            skipped[chart["name"]] = f"none of its fields exist yet: {missing}"
            continue
        content = {"id": secrets.token_hex(4), "name": chart["name"], "title": chart["title"],
                   "plots": plots, "height": chart["height"], "yAxisLabel": chart["y"],
                   "yAxisMin": None, "yAxisMax": None, "y2AxisLabel": None, "y2AxisMin": None,
                   "y2AxisMax": None, "y3AxisMin": None, "y3AxisMax": None, "stackTo100Percent": None}
        create.append({"name": chart["name"], "type": "FITNESS_CHART", "visibility": "PRIVATE",
                       "description": "Created by garmin-intervals-bridge for the values it syncs.",
                       "content": content, "_missing_fields": missing})
    return {"create": create, "skipped": skipped}


def setup_charts(intervals: Any, *, apply: bool) -> dict:
    plan = plan_charts(intervals.custom_items())
    out = {"apply": apply, "skipped": plan["skipped"],
           "charts": [{"name": c["name"], "plots": [p["field"] for p in c["content"]["plots"]],
                       "fields_missing": c["_missing_fields"]} for c in plan["create"]],
           "created": []}
    if apply:
        for chart in plan["create"]:
            body = {k: v for k, v in chart.items() if not k.startswith("_")}
            result = intervals.create_custom_item(body)
            out["created"].append({"name": chart["name"], "id": (result or {}).get("id")})
    return out
