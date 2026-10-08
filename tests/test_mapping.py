from datetime import date

from garmin_intervals_bridge.mapping import CUSTOM_FIELDS, map_wellness, merge_wellness


def sample():
    return {"data": {
        "stats": {"totalSteps": 9001, "restingHeartRate": 49, "bodyBatteryHighestValue": 91,
                  "bodyBatteryLowestValue": 19, "averageStressLevel": 38,
                  "activeKilocalories": 455, "totalKilocalories": 2355, "dailyStepGoal": 10000},
        "hrv": {"hrvSummary": {"lastNightAvg": 40, "lastNight5MinHigh": 78,
                               "weeklyAvg": 38}},
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27600,
                                     "sleepScores": {"overall": {"value": 88}}}},
        "morning_readiness": {"score": 72, "recoveryTime": 1440, "acuteLoad": 422},
        "endurance_score": [{"calendarDate": "2026-10-07", "overallScore": 5320}],
        "hill_score": [{"calendarDate": "2026-10-07", "overallScore": 73,
                        "strengthScore": 68, "enduranceScore": 80}],
        "body_battery": [{"date": "2026-10-07", "charged": 73, "drained": 64}],
        "nutrition": {"dailyNutritionContent": {"calories": 2410, "carbs": 280.5, "fat": 90.2, "protein": 118.0}},
    }}


def test_wellness_maps_scales_without_conflation():
    nat, custom = map_wellness(sample(), date(2026, 10, 7), date(2026, 10, 8))
    assert nat["sleepSecs"] == 27600
    assert nat["readiness"] == 72
    assert nat["hrv"] == 40
    assert nat["steps"] == 9001
    assert "stress" not in nat  # Garmin 0..100 != Intervals 0..4
    assert custom["GarminStressAvg"] == 38
    assert custom["GarminEnduranceScore"] == 5320
    assert custom["GarminHillScore"] == 73
    assert custom["GarminRecoveryTimeMinutes"] == 1440
    assert custom["GarminTotalCalories"] == 2355 and custom["GarminKcalBalance"] == 55
    assert custom["BodyBatteryMax"] == 91
    assert custom["GarminHRV5MinHigh"] == 78
    assert nat["kcalConsumed"] == 2410 and nat["carbohydrates"] == 280.5
    assert nat["protein"] == 118.0 and nat["fatTotal"] == 90.2
    assert custom["GarminCarbsKcal"] == 1122 and custom["GarminProteinKcal"] == 472 and custom["GarminFatKcal"] == 812


def test_today_not_written_incomplete_daily_totals():
    nat, custom = map_wellness(sample(), date(2026, 10, 8), date(2026, 10, 8))
    assert "steps" not in nat and "kcalConsumed" not in nat
    assert "BodyBatteryMax" not in custom
    assert "GarminEnduranceScore" not in custom
    assert nat["readiness"] == 72


def test_merge_preserves_manual_and_custom_values():
    # Custom codes are top-level keys in the live API (verified 2026-10-08).
    existing = {"locked": False, "restingHR": 47, "BodyBatteryMax": 88, "TrainingAdvice": 3,
                "BodyBatteryMin": None}
    patch = merge_wellness(existing, {"restingHR": 49, "hrv": 40},
                          {"BodyBatteryMax": 91, "BodyBatteryMin": 20, "GarminTrainingReadiness": 72})
    assert "restingHR" not in patch and "BodyBatteryMax" not in patch
    assert patch == {"hrv": 40, "BodyBatteryMin": 20, "GarminTrainingReadiness": 72}
    assert "customFields" not in patch
    assert merge_wellness({"locked": True}, {"hrv": 30}, {"GarminHillScore": 70}) == {}


def test_prevent_invalid_numeric_data_and_out_of_range():
    source = sample()
    source["data"]["stats"]["totalSteps"] = "9001"
    source["data"]["morning_readiness"]["score"] = 999
    source["data"]["stats"]["averageStressLevel"] = 99.99999
    nat, custom = map_wellness(source, date(2026, 10, 7), date(2026, 10, 8))
    assert "steps" not in nat
    assert "readiness" not in nat
    assert "GarminTrainingReadiness" not in custom
    assert custom["GarminStressAvg"] == 99.99999


def test_stable_camelcase_custom_codes():
    assert len(CUSTOM_FIELDS) >= 15
    assert all(k[0].isupper() and k.isalnum() for k in CUSTOM_FIELDS)


def test_extended_metrics_known_units():
    source = sample()
    source["data"].update({
        "max_metrics": [{"calendarDate": "2026-10-07", "generic": {"vo2MaxPreciseValue": 48.8},
                         "cycling": {"vo2MaxPreciseValue": 51.2}}],
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27500,
                                     "deepSleepSeconds": 5400,
                                     "remSleepSeconds": 4500,
                                     "avgSleepStress": 17,
                                     "averageSpO2Value": 96.7,
                                     "averageRespirationValue": 13.5},
                  "avgSkinTempDeviationC": -0.7},
        "fitness_age": {"fitnessAge": 32.5, "achievableFitnessAge": 30.0},
        "race_predictions": [{"calendarDate": "2026-10-07", "time5K": 1320,
                              "time10K": 2860, "timeHalfMarathon": 6200,
                              "timeMarathon": 13340}],
        "hydration": {"valueInML": 2100, "sweatLossInML": 1675, "goalInML": 2800},
    })
    nat, custom = map_wellness(source, date(2026, 10, 7), date(2026, 10, 8))
    assert nat["vo2max"] == 48.8
    assert custom["GarminVO2MaxCycling"] == 51.2
    assert custom["GarminSleepDeepMinutes"] == 90
    assert custom["GarminSkinTempDeviationC"] == -0.7
    assert custom["GarminFitnessAge"] == 32.5
    assert custom["GarminPredicted10KSeconds"] == 2860
    assert nat["hydrationVolume"] == 2.1
    assert nat["spO2"] == 96.7 and nat["respiration"] == 13.5
    assert custom["GarminSweatLossLitres"] == 1.675


def test_unlogged_nutrition_day_is_not_written_as_zero():
    source = sample()
    source["data"]["nutrition"] = {"dailyNutritionContent": {"calories": 0, "carbs": 0, "fat": 0, "protein": 0}}
    source["data"]["stats"]["consumedKilocalories"] = 0
    nat, _ = map_wellness(source, date(2026, 10, 7), date(2026, 10, 8))
    assert not {"kcalConsumed", "carbohydrates", "protein", "fatTotal"} & set(nat)


def test_sleep_heart_rate_floors_and_scale_natives():
    source = sample()
    source["data"]["sleep"]["dailySleepDTO"]["avgHeartRate"] = 52
    source["data"]["stats"]["floorsAscended"] = 14
    source["data"]["body_composition"] = {"dateWeightList": [{"calendarDate": "2026-10-07", "weight": 78400, "bodyFat": 17.3}]}
    nat, _ = map_wellness(source, date(2026, 10, 7), date(2026, 10, 8))
    assert nat["avgSleepingHR"] == 52 and nat["floorsClimbed"] == 14
    assert nat["weight"] == 78.4 and nat["bodyFat"] == 17.3


def test_recommended_profile_drops_duplicates_goals_and_subscores():
    source = sample()
    source["data"].update({"hill_score": [{"calendarDate": "2026-10-07", "overallScore": 73, "strengthScore": 68, "enduranceScore": 80}],
                           "hydration": {"valueInML": 2100, "goalInML": 2800},
                           "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27500, "averageSpO2Value": 96.7}}})
    _, all_custom = map_wellness(source, date(2026, 10, 7), date(2026, 10, 8), "all")
    nat, rec = map_wellness(source, date(2026, 10, 7), date(2026, 10, 8), "recommended")
    assert {"GarminHydrationGoalLitres", "GarminStepsGoal", "GarminSleepSpO2Avg"} <= set(all_custom)
    assert not {"GarminHydrationGoalLitres", "GarminStepsGoal", "GarminSleepSpO2Avg"} & set(rec)
    assert rec["GarminHillScore"] == 73 and rec["GarminHillStrength"] == 68 and nat["spO2"] == 96.7


def test_readiness_is_the_mornings_recovery_the_days_last_reading():
    from garmin_intervals_bridge.mapping import map_wellness, merge_wellness
    src = sample()
    src["data"].pop("morning_readiness", None)
    src["data"]["training_readiness"] = [
        {"timestampLocal": "2026-08-08T21:01:50.0", "inputContext": "AFTER_POST_EXERCISE_RESET", "score": 5,
         "recoveryTime": 5757, "acuteLoad": 795},
        {"timestampLocal": "2026-08-08T09:33:10.0", "inputContext": "AFTER_WAKEUP_RESET", "score": 85,
         "recoveryTime": 1, "acuteLoad": 192},
    ]
    nat, custom = map_wellness(src, date(2026, 8, 8), date(2026, 8, 9))
    assert custom["GarminTrainingReadiness"] == 85                       # the morning
    assert custom["GarminRecoveryTimeMinutes"] == 5757 and custom["GarminRecoveryTimeHours"] == 96.0
    assert custom["GarminAcuteLoad"] == 795                              # the evening
    _, today = map_wellness(src, date(2026, 8, 8), date(2026, 8, 8))
    assert today["GarminTrainingReadiness"] == 85 and "GarminRecoveryTimeMinutes" not in today   # waits for the day to end
    # a mapping correction may replace the bridge's own values, never native ones
    existing = {"GarminRecoveryTimeMinutes": 1, "GarminAcuteLoad": 192, "restingHR": 44}
    patch = merge_wellness(existing, {"restingHR": 50}, custom, rewrite={"GarminRecoveryTimeMinutes", "restingHR"})
    assert patch["GarminRecoveryTimeMinutes"] == 5757 and "restingHR" not in patch and "GarminAcuteLoad" not in patch


def test_values_final_only_at_the_end_of_the_day_and_unlogged_hydration_stays_empty():
    from garmin_intervals_bridge.mapping import map_wellness
    src = sample()
    src["data"]["hydration"] = {"calendarDate": "2026-10-07", "valueInML": 0.0, "goalInML": 3000.0, "sweatLossInML": 722.0}
    src["data"]["stats"]["floorsAscended"] = 14.4895
    src["data"]["max_metrics"] = [{"generic": {"vo2MaxPreciseValue": 47.4}, "cycling": {"vo2MaxPreciseValue": 51.2}}]
    src["data"]["body_composition"] = {"dateWeightList": [{"calendarDate": "2026-10-08", "weight": 83350, "bodyFat": 23.5},
                                                          {"calendarDate": "2026-10-07", "weight": 83100, "bodyFat": 23.6}]}
    today, past = date(2026, 10, 8), date(2026, 10, 7)
    nat_today, cus_today = map_wellness(src, today, today)
    nat_past, cus_past = map_wellness(src, past, today)
    assert "vo2max" not in nat_today and "vo2max" in nat_past               # a run later today may revise it
    assert "weight" in nat_today and "weight" in nat_past                    # a weigh-in is final at once
    assert "hydrationVolume" not in nat_past and cus_past["GarminSweatLossLitres"] == 0.722
    assert nat_past["floorsClimbed"] == 14
