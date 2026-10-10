# Field mapping

Two very different mechanisms, deliberately.

## Activities: your custom items are the mapping

The bridge does not carry a table of Garmin FIT fields. It reads the
definitions of your own Intervals custom items (`GET /athlete/{id}/custom-item`)
and uses them exactly as Intervals does when it processes a file itself:

| Custom item | Definition field | Example | Bridge reads |
| --- | --- | --- | --- |
| `ACTIVITY_FIELD` | `content.fit_session_field` | `"total_training_effect"` | that `session` field |
| | | `"178"` | `session` field number 178 |
| | | `"140.9"` | message 140, field 9 (last non-null) |
| | `content.script` (optional) | `activity.isNew ? activity.RecoveryTime / 60 : activity.RecoveryTime` | first-import conversion `/ 60` |
| | | `activity.LTHRdetected == 0 ? NaN : activity.LTHRdetected` | `0` means "no value" |
| `ACTIVITY_STREAM` | `content.fit_record_field` | `"stance_time"` | that `record` field, per record |
| | `content.script` | `for (let m of icu.fit.record) { let f = m.f_138; if (f) data.setAt(m.timestamp.value, f.value/1000) }` | record field 138, `/ 1000` |

Definitions the bridge cannot interpret safely (scripts over other
messages, arithmetic beyond constant factors, fields computed from other
fields) are reported as *unsupported* in every plan and never written.
Select fields accept only their configured option values.

Values are aligned to the activity's `time` stream by timestamp and refused
when fewer than 95 % of the points line up. Streams already present on the
activity are skipped unless `--refresh-own-streams` names one the bridge
wrote itself.

Public community items such as *Recovery Time*, *Sweat loss*, *Stamina at
start/end*, *Minimum Stamina* and the *Stamina* / *Potential Stamina*
streams carry the Garmin-internal IDs (message 140, session fields 178 and
205–207, record fields 137/138); adding them to your account is all it
takes for the bridge to fill them.

## Wellness: an explicit, unit-checked table

Only scalars with reviewed meaning are written, and only where the day has
no value yet. Locked days are skipped. Running totals of the current day
wait until tomorrow.

### Native Intervals fields

| Intervals | Garmin source | Unit / note |
| --- | --- | --- |
| `restingHR` | `stats.restingHeartRate` (fallback sleep) | bpm |
| `hrv` | `hrv.hrvSummary.lastNightAvg` | ms (overnight rMSSD average) |
| `avgSleepingHR` | `sleep.dailySleepDTO.avgHeartRate` | bpm |
| `sleepSecs` | `sleep.dailySleepDTO.sleepTimeSeconds` | s |
| `sleepScore` | `sleep.dailySleepDTO.sleepScores.overall.value` | 0–100 |
| `spO2` | `sleep.dailySleepDTO.averageSpO2Value` | % |
| `respiration` | `sleep.dailySleepDTO.averageRespirationValue` | breaths/min |
| `readiness` | morning training readiness `score` | 0–100 |
| `vo2max` | `max_metrics.generic.vo2MaxPreciseValue` | ml/kg/min (running) |
| `steps` | `stats.totalSteps` | yesterday or older |
| `floorsClimbed` | `stats.floorsAscended` | yesterday or older |
| `hydrationVolume` | `hydration.valueInML / 1000` | litres |
| `kcalConsumed` | nutrition `dailyNutritionContent.calories` (fallback `stats.consumedKilocalories`) | kcal; `0` = not logged, never written |
| `carbohydrates`, `protein`, `fatTotal` | nutrition `dailyNutritionContent.carbs/protein/fat` | g |
| `GarminCarbsKcal`, `GarminProteinKcal`, `GarminFatKcal` (custom) | the same grams x 4 / 4 / 9 (Atwater factors) | kcal; only for the stacked macro chart, whose percentages are then the energy share Garmin shows |
| `GarminTotalCalories` (custom) | `stats.totalKilocalories` (BMR + active) | kcal; the "burn" side of intake vs. burn |
| `GarminKcalBalance` (custom) | `kcalConsumed - GarminTotalCalories`, finished days with logged food only | kcal; weekly / monthly totals in the energy-balance charts |
| `weight` | `body_composition.dateWeightList[].weight / 1000` | kg |
| `bodyFat` | `body_composition.dateWeightList[].bodyFat` | % |

Deliberately **not** mapped: Garmin stress 0–100 → Intervals `stress`
(a 1–4 subjective scale); Garmin training load → Intervals CTL/ATL
(different models); intraday series of any kind.

### Private custom fields (`Garmin…`)

Created as private numeric `INPUT_FIELD`s on first `--apply`; existing
definitions with the same code are reused, never changed.

| Code | Source | Profile |
| --- | --- | --- |
| `BodyBatteryMax`, `BodyBatteryMin` | `stats.bodyBatteryHighestValue/LowestValue` | both |
| `GarminBodyBatteryCharged`, `GarminBodyBatteryDrained` | `body_battery[].charged/drained` | both |
| `GarminTrainingReadiness`, `GarminRecoveryTimeMinutes`, `GarminAcuteLoad` | morning readiness | both |
| `GarminHRV5MinHigh`, `GarminHRV7DayAvg` | `hrv.hrvSummary` | both |
| `GarminSleepDeepMinutes`, `…REMMinutes`, `…LightMinutes`, `…AwakeMinutes` | sleep stages / 60 | both |
| `GarminSleepStressAvg`, `GarminSkinTempDeviationC` | sleep | both |
| `GarminStressAvg`, `GarminActiveCalories`, `GarminTotalCalories`, `GarminIntensityModerateMinutes`, `GarminIntensityVigorousMinutes` | `stats` | both |
| `GarminEnduranceScore`, `GarminHillScore`, `GarminHillStrength`, `GarminHillEndurance` | scores | both |
| `GarminFitnessAge` | fitness age | both |
| `GarminPredicted5KSeconds`, `…10KSeconds`, `…HalfSeconds`, `…MarathonSeconds` | race predictions | both |
| `GarminSweatLossLitres` | `hydration.sweatLossInML / 1000` | both |
| `GarminVO2MaxCycling` | `max_metrics.cycling` | both |
| `GarminSleepSpO2Lowest` | `sleep.dailySleepDTO.lowestSpO2Value` | both |
| `GarminSleepRespirationLowest`, `…Highest` | `sleep.dailySleepDTO.lowest/highestRespirationValue` | both |
| `GarminSleepBodyBatteryChange` | `sleep.bodyBatteryChange` (may be negative) | both |
| `GarminHRVBaselineLow`, `…High` | `hrv.hrvSummary.baseline.balancedLow/balancedUpper` (Garmin's "balanced" band) | both |
| `GarminSnapshotHR`, `…RMSSD`, `…SDRR`, `…Respiration`, `…SpO2`, `…Stress` | the day's first Health Snapshot, from the device's original wellness files (session averages) | both |
| `GarminSleepSpO2Avg`, `GarminSleepRespirationAvg` | duplicates of native `spO2` / `respiration` | `all` only |
| `GarminStepsGoal`, `GarminHydrationGoalLitres` | targets, not measurements | `all` only |
| `GarminAchievableFitnessAge` | derived from fitness age | `all` only |

Everything Garmin returns is archived as `raw/YYYY-MM-DD.json` regardless
of whether it is mapped, so a future mapping can be added without
re-fetching the past.

### Health Snapshots and the native fields

The official integration writes a Health Snapshot's heart rate, RMSSD and
SpO2 into Intervals' resting HR, HRV and SpO2, replacing the night's values
with two minutes in a chair. When a native value equals one of that day's
snapshots and the night's value differs, the bridge puts the night's value
back (the only case besides a filtered file in which it replaces an existing
value); the snapshot keeps its own `GarminSnapshot…` fields. This happens on
the same day: after the morning read the bridge keeps an eye on today's HRV in
Intervals (no Garmin request), and when it moves away from the night's, it
reads today's files once to see whether a snapshot explains it. Untick SpO2
in Intervals' Garmin wellness settings: the official integration only ever
delivers it from snapshots, the bridge brings the overnight average. Resting
HR and HRV can stay ticked, they bring the night's values early.

## Notes on enrich mode

- A value you typed by hand into a field whose FIT source Garmin filtered
  out cannot be told apart from Intervals' own `0` placeholder for a missing
  source, so the original's value replaces it. Fields whose source the
  partner copy still carries are never changed.
- Before a stream is written, the bridge aligns a stream both sides already
  have (heart rate, else cadence) the same way and compares it with what
  Intervals holds. A mismatch means the time origins differ; the streams are
  then skipped and the journal says so. Activities without a shared stream
  are written on the strength of the timestamp alignment alone.
- An activity enriched earlier is looked at again when the athlete's field
  definitions change (a new custom field or stream); only what is still
  missing gets added.
