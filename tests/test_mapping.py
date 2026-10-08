from datetime import date

from garmin_intervals_bridge.mapping import map_wellness, merge_wellness, CUSTOM_FIELDS


def sample():
    return {"data": {
        "stats": {"totalSteps": 9001, "restingHeartRate": 49, "bodyBatteryHighestValue": 91,
                  "bodyBatteryLowestValue": 19, "averageStressLevel": 38,
                  "activeKilocalories": 455, "dailyStepGoal": 10000},
        "hrv": {"hrvSummary": {"lastNightAvg": 40, "lastNight5MinHigh": 78,
                               "weeklyAvg": 38}},
        "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27600,
                                     "sleepScores": {"overall": {"value": 88}}}},
        "morning_readiness": {"score": 72, "recoveryTime": 1440, "acuteLoad": 422},
        "endurance_score": [{"calendarDate": "2026-10-07", "overallScore": 5320}],
        "hill_score": [{"calendarDate": "2026-10-07", "overallScore": 73,
                        "strengthScore": 68, "enduranceScore": 80}],
        "body_battery": [{"date": "2026-10-07", "charged": 73, "drained": 64}],
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
    assert custom["BodyBatteryMax"] == 91
    assert custom["GarminHRV5MinHigh"] == 78


def test_today_not_written_incomplete_daily_totals():
    nat, custom = map_wellness(sample(), date(2026, 10, 8), date(2026, 10, 8))
    assert "steps" not in nat
    assert "BodyBatteryMax" not in custom
    assert "GarminEnduranceScore" not in custom
    assert nat["readiness"] == 72


def test_merge_preserves_manual_and_custom_values():
    existing = {"locked": False, "restingHR": 47,
                "customFields": {"BodyBatteryMax": 88, "TrainingAdvice": 3}}
    patch = merge_wellness(existing, {"restingHR": 49, "hrv": 40},
                          {"BodyBatteryMax": 91, "GarminTrainingReadiness": 72})
    assert "restingHR" not in patch
    assert patch["hrv"] == 40
    assert patch["customFields"] == {"BodyBatteryMax": 88,
                                      "TrainingAdvice": 3, "GarminTrainingReadiness": 72}
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
    assert custom["GarminSweatLossLitres"] == 1.675
