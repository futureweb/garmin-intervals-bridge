# Wellness field mapping (v0.1)

Only scalars with reviewed meaning/units are mapped. All source JSON is stored in `data/raw/`, subject to your private data handling policy. Intervals data is only modified with `--apply` and is never overwritten if already present.

**Intervals native fields:**

| Intervals code | Garmin source | Notes |
| --- | --- | --- |
| `restingHR` | `stats.restingHeartRate` (fallback sleep.restingHeartRate) | bpm |
| `hrv` | `hrv.hrvSummary.lastNightAvg` | Garmin overnight HRV/rMSSD in milliseconds; does not overwrite existing |
| `sleepSecs` | `sleep.dailySleepDTO.sleepTimeSeconds` | seconds |
| `sleepScore` | `sleep.dailySleepDTO.sleepScores.overall.value` | 0–100 |
| `readiness` | morning readiness `score` | 0–100; *only missing* native readiness |
| `vo2max` | `max_metrics[0].generic.vo2MaxPreciseValue` | running VO2max if Garmin provides it |
| `steps` | `stats.totalSteps` | yesterday/older only |
| `hydrationVolume` | `hydration.valueInML / 1000` | litres, yesterday/older only |

**Private custom wellness fields:**

| Code(s) | Garmin metric |
| --- | --- |
| `BodyBatteryMax`, `BodyBatteryMin` | Daily max/min (retains already existing definitions/values) |
| `GarminBodyBatteryCharged`, `GarminBodyBatteryDrained` | Day's Body Battery charged/drained numbers |
| `GarminTrainingReadiness`, `GarminRecoveryTimeMinutes`, `GarminAcuteLoad` | Morning readiness, recovery in minutes, device acute load |
| `GarminHRV5MinHigh`, `GarminHRV7DayAvg` | HRV high 5-min reading, weekly average |
| `GarminEnduranceScore` | Endurance Score (daily) |
| `GarminHillScore`, `GarminHillStrength`, `GarminHillEndurance` | Hill Score and subcomponents |
| `GarminStressAvg` | Numeric Garmin stress 0–100. Not Intervals subjective `stress` |
| `GarminActiveCalories`, `GarminStepsGoal` | Daily active kcal and target steps |
| `GarminIntensityModerateMinutes`, `GarminIntensityVigorousMinutes` | Garmin intensity minutes |
| `GarminVO2MaxCycling` | Cycling VO2max (ml/kg/min) |
| `GarminSleepDeepMinutes`, `GarminSleepREMMinutes`, `GarminSleepLightMinutes`, `GarminSleepAwakeMinutes` | Sleep stage durations (minutes) |
| `GarminSleepStressAvg`, `GarminSleepSpO2Avg`, `GarminSleepRespirationAvg` | Numeric sleep stress, blood oxygen, respiration |
| `GarminSkinTempDeviationC` | Skin temperature deviation in °C, not absolute temperature |
| `GarminFitnessAge`, `GarminAchievableFitnessAge` | Garmin fitness ages in years |
| `GarminPredicted5KSeconds`, `GarminPredicted10KSeconds`, `GarminPredictedHalfSeconds`, `GarminPredictedMarathonSeconds` | Race-prediction times in seconds |
| `GarminHydrationGoalLitres`, `GarminSweatLossLitres` | Garmin water goal and estimated sweat loss (litres) |

**Only raw-archived so far (not mapped as wellness scalars):** Detailed HRV readings, sleep movement/stages time series, Body Battery time series, intraday SpO2/respiration, training-status enums and per-device variants, device identifiers, detailed lactate-threshold history, body-composition measurements with ambiguous units and timing, blood-pressure sample history, lifestyle logs, and more. JSON archive coverage depends on each supported Garmin endpoint and account.

**Native field overwrite**: Never overwrite existing values. Intervals Garmin partner Wellness sync can stay enabled; the bridge fills missing fields and custom ones. Locked days are skipped. Today's totals are excluded because they are not final.

**Device-specific data**: Some metrics come from Garmin watches, others from the Edge or scales. If multiple devices disagree on a day's value, mapping will need further validation on real user data; v0.1 does not invent a combined number. Health metrics saved as raw JSON may contain intimate medical/route information — protect the archive.

**Custom field visibility**: `INPUT_FIELD` definitions are created as `PRIVATE`, with `content.code` (stable CamelCase) and `content.type=numeric`. Enable the desired fields in Intervals calendar/wellness Settings or charts to visualize them. Provisioning happens only with `--apply`.
