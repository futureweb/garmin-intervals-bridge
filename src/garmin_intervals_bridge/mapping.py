"""Explicit, unit-checked Garmin -> Intervals DAILY wellness field mapping.

Only scalar values with known meaning are mapped. We never convert Garmin's
0-100 stress to Intervals' categorical stress or Garmin training load to CTL.
All unrepresentable Garmin data stays in the local raw archive.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class CustomField:
    name: str
    units: str | None = None
    maximum: float | None = None


# Stable, public CamelCase codes. Existing Intervals fields of these codes win.
CUSTOM_FIELDS: dict[str, CustomField] = {
    "BodyBatteryMax": CustomField("Body Battery Max", None, 100),
    "BodyBatteryMin": CustomField("Body Battery Min", None, 100),
    "GarminBodyBatteryCharged": CustomField("Garmin Body Battery Charged", None, 100),
    "GarminBodyBatteryDrained": CustomField("Garmin Body Battery Drained", None, 100),
    "GarminTrainingReadiness": CustomField("Garmin Morning Training Readiness", None, 100),
    "GarminRecoveryTimeMinutes": CustomField("Garmin Recovery Time", "min"),
    "GarminHRV5MinHigh": CustomField("Garmin HRV 5-min High", "ms"),
    "GarminHRV7DayAvg": CustomField("Garmin HRV 7-day Average", "ms"),
    "GarminEnduranceScore": CustomField("Garmin Endurance Score"),
    "GarminHillScore": CustomField("Garmin Hill Score", None, 100),
    "GarminHillStrength": CustomField("Garmin Hill Strength", None, 100),
    "GarminHillEndurance": CustomField("Garmin Hill Endurance", None, 100),
    "GarminStressAvg": CustomField("Garmin Stress Average", None, 100),
    "GarminActiveCalories": CustomField("Garmin Active Calories", "kcal"),
    "GarminIntensityModerateMinutes": CustomField("Garmin Moderate Intensity", "min"),
    "GarminIntensityVigorousMinutes": CustomField("Garmin Vigorous Intensity", "min"),
    "GarminStepsGoal": CustomField("Garmin Daily Steps Goal", "steps"),
    "GarminAcuteLoad": CustomField("Garmin Acute Training Load"),
    "GarminVO2MaxCycling": CustomField("Garmin Cycling VO2max", "ml/kg/min"),
    "GarminSleepDeepMinutes": CustomField("Garmin Deep Sleep", "min"),
    "GarminSleepREMMinutes": CustomField("Garmin REM Sleep", "min"),
    "GarminSleepLightMinutes": CustomField("Garmin Light Sleep", "min"),
    "GarminSleepAwakeMinutes": CustomField("Garmin Awake During Sleep", "min"),
    "GarminSleepStressAvg": CustomField("Garmin Sleep Stress Avg", None, 100),
    "GarminSleepSpO2Avg": CustomField("Garmin Sleep SpO2 Avg", "%", 100),
    "GarminSleepRespirationAvg": CustomField("Garmin Sleep Respiration Avg", "breaths/min"),
    "GarminSkinTempDeviationC": CustomField("Garmin Skin Temperature Deviation", "°C"),
    "GarminFitnessAge": CustomField("Garmin Fitness Age", "years"),
    "GarminAchievableFitnessAge": CustomField("Garmin Achievable Fitness Age", "years"),
    "GarminPredicted5KSeconds": CustomField("Garmin Predicted 5K", "s"),
    "GarminPredicted10KSeconds": CustomField("Garmin Predicted 10K", "s"),
    "GarminPredictedHalfSeconds": CustomField("Garmin Predicted Half Marathon", "s"),
    "GarminPredictedMarathonSeconds": CustomField("Garmin Predicted Marathon", "s"),
    "GarminHydrationGoalLitres": CustomField("Garmin Daily Hydration Goal", "L"),
    "GarminSweatLossLitres": CustomField("Garmin Estimated Sweat Loss", "L"),
}


# Custom codes the "recommended" profile leaves out: duplicates of native fields,
# goals rather than measurements, and sub-scores of a score that is kept.
RECOMMENDED_EXCLUDES = frozenset({
    "GarminSleepSpO2Avg", "GarminSleepRespirationAvg",      # native spO2 / respiration carry these
    "GarminStepsGoal", "GarminHydrationGoalLitres",         # targets, not measurements
    "GarminAchievableFitnessAge",                           # derived from GarminFitnessAge
})


def get(obj: Any, *path: str, default: Any = None) -> Any:
    for key in path:
        if not isinstance(obj, dict):
            return default
        obj = obj.get(key)
    return obj if obj is not None else default


def number(value: Any, *, low: float = 0, high: float = 1000000) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def choose(obj: Any, *paths: tuple[str, ...], low=0, high=1000000) -> float | int | None:
    for path in paths:
        v = number(get(obj, *path), low=low, high=high)
        if v is not None:
            return v
    return None


def _day_record(raw: Any, target: date) -> dict:
    """Select a day-specific score rather than an unrelated weekly aggregate."""
    if isinstance(raw, dict):
        for key in ("data", "items", "scores", "metrics", "entries"):
            if isinstance(raw.get(key), list):
                return _day_record(raw[key], target)
        known_date = raw.get("calendarDate") or raw.get("date")
        return raw if known_date in (None, target.isoformat()) else {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and (item.get("calendarDate") or item.get("date")) == target.isoformat():
                return item
        if len(raw) == 1 and isinstance(raw[0], dict):
            return _day_record(raw[0], target)
    return {}


def map_wellness(snapshot: dict, target: date, today: date, profile: str = "all") -> tuple[dict, dict]:
    """Return (native, custom) value dicts; absent/invalid values are omitted.

    Today's accumulating totals are excluded; their final values can be
    imported tomorrow. Sleep/HRV/Readiness may be imported today.
    `profile` "recommended" drops the custom codes in RECOMMENDED_EXCLUDES.
    """
    data = snapshot.get("data", {})
    stats = data.get("stats") or {}
    sleep = get(data, "sleep", "dailySleepDTO") or {}
    hrv = get(data, "hrv", "hrvSummary") or {}
    morning = data.get("morning_readiness")
    if not isinstance(morning, dict) or not morning:
        morning = next((x for x in data.get("training_readiness", []) or []
                        if isinstance(x, dict) and x.get("inputContext") == "MORNING_REPORT"), {})
    battery = _day_record(data.get("body_battery"), target)
    endurance = _day_record(data.get("endurance_score"), target)
    hill = _day_record(data.get("hill_score"), target)
    maximum = _day_record(data.get("max_metrics"), target)
    race = _day_record(data.get("race_predictions"), target)
    age = _day_record(data.get("fitness_age"), target)
    hydration = _day_record(data.get("hydration"), target)

    native: dict[str, int | float] = {}
    custom: dict[str, int | float] = {}

    def put(target_map: dict, key: str, val: Any, *, low=0, high=1000000) -> None:
        scalar = number(val, low=low, high=high)
        if scalar is not None:
            target_map[key] = scalar

    put(native, "restingHR", choose(stats, ("restingHeartRate",), high=220)
        or choose(data.get("sleep"), ("restingHeartRate",), high=220), high=220)
    put(native, "hrv", choose(hrv, ("lastNightAvg",), high=400), high=400)
    put(native, "avgSleepingHR", choose(sleep, ("avgHeartRate",), low=20, high=220), low=20, high=220)
    put(native, "sleepSecs", choose(sleep, ("sleepTimeSeconds",), high=86400), high=86400)
    sleep_score = choose(sleep, ("sleepScores", "overall", "value"),
                         ("sleepScores", "overall", "score"), high=100)
    put(native, "sleepScore", sleep_score, high=100)
    put(custom, "GarminHRV5MinHigh", choose(hrv, ("lastNight5MinHigh",), high=500), high=500)
    put(custom, "GarminHRV7DayAvg", choose(hrv, ("weeklyAvg",), high=400), high=400)

    readiness_score = choose(morning, ("score",), high=100)
    if readiness_score is not None:
        put(native, "readiness", readiness_score, high=100)
        put(custom, "GarminTrainingReadiness", readiness_score, high=100)
    put(custom, "GarminRecoveryTimeMinutes", choose(morning, ("recoveryTime",), high=30000), high=30000)
    put(custom, "GarminAcuteLoad", choose(morning, ("acuteLoad",)), high=3000)
    put(native, "vo2max", choose(maximum, ("generic", "vo2MaxPreciseValue"),
                                  ("generic", "vo2MaxValue"), high=100), high=100)
    put(custom, "GarminVO2MaxCycling", choose(maximum, ("cycling", "vo2MaxPreciseValue"),
                                               ("cycling", "vo2MaxValue"), high=100), high=100)
    for key, source_name in (
        ("GarminSleepDeepMinutes", "deepSleepSeconds"),
        ("GarminSleepREMMinutes", "remSleepSeconds"),
        ("GarminSleepLightMinutes", "lightSleepSeconds"),
        ("GarminSleepAwakeMinutes", "awakeSleepSeconds"),
    ):
        seconds = choose(sleep, (source_name,), high=86400)
        if seconds is not None:
            put(custom, key, round(seconds / 60, 2), high=1440)
    put(custom, "GarminSleepStressAvg", choose(sleep, ("avgSleepStress",), high=100), high=100)
    # Intervals has native fields for these two; use them (only when empty, as always)
    # and keep the Garmin-named copies for people who chart them separately.
    put(native, "spO2", choose(sleep, ("averageSpO2Value",), high=100), high=100)
    put(native, "respiration", choose(sleep, ("averageRespirationValue",), high=60), high=60)
    put(custom, "GarminSleepSpO2Avg", choose(sleep, ("averageSpO2Value",), high=100), high=100)
    put(custom, "GarminSleepRespirationAvg", choose(sleep, ("averageRespirationValue",), high=60), high=60)
    put(custom, "GarminSkinTempDeviationC", choose(data.get("sleep"), ("avgSkinTempDeviationC",),
                                                     low=-20, high=20), low=-20, high=20)

    # Energy totals change all day; only import after the day has finished.
    if target < today:
        put(native, "steps", choose(stats, ("totalSteps",), high=100000), high=100000)
        put(native, "floorsClimbed", choose(stats, ("floorsAscended",), high=5000), high=5000)
        # Logged food. A day without entries reports 0, which is "not logged", not "ate nothing".
        nutrition = get(data, "nutrition", "dailyNutritionContent") or {}
        put(native, "kcalConsumed", choose(nutrition, ("calories",), low=1, high=20000)
            or choose(stats, ("consumedKilocalories",), low=1, high=20000), low=1, high=20000)
        put(native, "carbohydrates", choose(nutrition, ("carbs",), low=0.1, high=3000), low=0.1, high=3000)
        put(native, "protein", choose(nutrition, ("protein",), low=0.1, high=1500), low=0.1, high=1500)
        put(native, "fatTotal", choose(nutrition, ("fat",), low=0.1, high=1500), low=0.1, high=1500)
        # Scale data, only where Intervals has nothing yet (the official sync usually brings weight).
        # get_body_composition() answers {"dateWeightList": [{calendarDate, weight (g), bodyFat, ...}], ...}
        composition = (_day_record(get(data, "body_composition", "dateWeightList"), target)
                       or _day_record(data.get("body_composition"), target))
        put(native, "weight", choose(composition, ("weight",), low=20000, high=400000), low=20000, high=400000)
        if "weight" in native:
            native["weight"] = round(native["weight"] / 1000, 2)        # Garmin stores grams
        put(native, "bodyFat", choose(composition, ("bodyFat",), low=1, high=70), low=1, high=70)
        put(custom, "GarminStepsGoal", choose(stats, ("dailyStepGoal",), high=100000), high=100000)
        put(custom, "GarminStressAvg", choose(stats, ("averageStressLevel",), high=100), high=100)
        put(custom, "GarminActiveCalories", choose(stats, ("activeKilocalories",), high=30000), high=30000)
        put(custom, "GarminIntensityModerateMinutes", choose(stats, ("moderateIntensityMinutes",), high=1440), high=1440)
        put(custom, "GarminIntensityVigorousMinutes", choose(stats, ("vigorousIntensityMinutes",), high=1440), high=1440)
        put(custom, "BodyBatteryMax", choose(stats, ("bodyBatteryHighestValue",), high=100), high=100)
        put(custom, "BodyBatteryMin", choose(stats, ("bodyBatteryLowestValue",), high=100), high=100)
        put(custom, "GarminBodyBatteryCharged", choose(battery, ("charged",), high=100), high=100)
        put(custom, "GarminBodyBatteryDrained", choose(battery, ("drained",), high=100), high=100)
        put(custom, "GarminEnduranceScore", choose(endurance, ("overallScore",), ("enduranceScore",), high=10000), high=10000)
        put(custom, "GarminHillScore", choose(hill, ("overallScore",), high=100), high=100)
        put(custom, "GarminHillStrength", choose(hill, ("strengthScore",), high=100), high=100)
        put(custom, "GarminHillEndurance", choose(hill, ("enduranceScore",), high=100), high=100)
        put(custom, "GarminFitnessAge", choose(age, ("fitnessAge",), high=130), high=130)
        put(custom, "GarminAchievableFitnessAge", choose(age, ("achievableFitnessAge",), high=130), high=130)
        for code, key, max_sec in (
            ("GarminPredicted5KSeconds", "time5K", 15000),
            ("GarminPredicted10KSeconds", "time10K", 30000),
            ("GarminPredictedHalfSeconds", "timeHalfMarathon", 60000),
            ("GarminPredictedMarathonSeconds", "timeMarathon", 120000),
        ):
            put(custom, code, choose(race, (key,), high=max_sec), high=max_sec)
        volume_ml = choose(hydration, ("valueInML",), high=30000)
        if volume_ml is not None:
            put(native, "hydrationVolume", round(volume_ml / 1000, 3), high=30)
        goal_ml = choose(hydration, ("goalInML",), high=30000)
        if goal_ml is not None:
            put(custom, "GarminHydrationGoalLitres", round(goal_ml / 1000, 3), high=30)
        sweat_ml = choose(hydration, ("sweatLossInML",), high=30000)
        if sweat_ml is not None:
            put(custom, "GarminSweatLossLitres", round(sweat_ml / 1000, 3), high=30)

    if profile == "recommended":
        custom = {k: v for k, v in custom.items() if k not in RECOMMENDED_EXCLUDES}
    return native, custom


def merge_wellness(existing: dict, native: dict, custom: dict) -> dict:
    """Add missing values only. Never overwrite locked, manually entered, or synced values.

    Custom wellness fields are ordinary top-level keys named by their code:
    verified against the live API on 2026-10-08, where `BodyBatteryMax` sits
    next to `restingHR` and no `customFields` object exists. v0.1.1 assumed a
    nested object; every custom write would have been silently ignored.
    """
    if existing.get("locked") is True:
        return {}
    patch: dict = {}
    for key, value in {**native, **custom}.items():
        if existing.get(key) is None:
            patch[key] = value
    return patch
