# Garmin → Intervals.icu Bridge

**Garmin started filtering the FIT files it sends to Intervals.icu. This puts the data back — and syncs a lot more.**

<!-- screenshots: docs/images/activity-before-after.png, docs/images/wellness-day.png, docs/images/fitness-chart.png -->

Since the end of September 2026 the file Garmin hands to partners is not
the file your device recorded. (Garmin first switched the filter on in
March 2026, rolled it back after a week of protest, and switched it back
on half a year later.) The recording survives (power, heart rate, GPS, cadence,
developer fields); Garmin's own metrics do not. Open the same activity in
Intervals.icu and in Garmin Connect and you will miss Performance
Condition, Stamina, Recovery Time, VO₂max, Training Effect and Sweat Loss.
The original file you can download from Garmin Connect still has them.

This bridge is a small self-hosted service that:

- fetches the **original FIT** through Garmin Connect's private API, byte-identical to the browser export;
- finds the activity the **official sync already created** in Intervals.icu — the sync stays on, nothing is duplicated, deleted or recomputed;
- adds exactly **what Garmin stripped**, into the custom fields and streams *you* have configured (Stamina curve, Recovery Time, VO₂max, Performance Condition, Sweat Loss, Training Effect …);
- fills the **daily wellness values the official sync does not deliver**: night SpO₂, respiration, sleeping heart rate, Body Battery, HRV details, sleep stages, stress, readiness, floors, scale data — and your **logged nutrition** (kcal, carbs, protein, fat);
- can **backfill the past** (on the reference account SpO₂ had silently stopped arriving in mid-2025);
- reacts within about a minute of the official import, with Garmin contacted only when there is something new.

Everything is a dry run until you say `--apply`. Values that exist are never
overwritten unless they demonstrably came from a filtered file.

## What gets synced

| | Activities (enrich mode) | Daily wellness |
| --- | --- | --- |
| **Source** | Original FIT from Garmin Connect | Garmin's daily endpoints (23 of them) |
| **Target** | Your Intervals custom activity fields and custom streams, defined by you, read from your own definitions | Native Intervals wellness fields first, private `Garmin…` custom fields for the rest |
| **Examples** | Stamina / Potential Stamina streams, Recovery Time, VO₂max, Performance Condition, Sweat Loss, Aerobic/Anaerobic Effect, Stamina at start/end, EPOC, Training Load, grade-adjusted speed | SpO₂, respiration, sleeping HR, resting HR, HRV (+5-min high, 7-day avg), sleep seconds/score/stages, Body Battery max/min/charged/drained, training readiness, recovery time, acute load, stress, intensity minutes, floors, steps, hydration, sweat loss, weight, body fat, kcal consumed, total and active burn, carbohydrates, protein, fat (g and kcal), endurance & hill scores, fitness age, race predictions, VO₂max (run/bike) |
| **Rule** | Only what the partner copy lacks; aligned by timestamp; idempotent | Only empty values; locked days skipped; today's running totals wait until tomorrow |
| **Archive** | Original + partner copy of every activity | Raw JSON of every endpoint, every day |

Version 0.2.0. Verified end to end on one account (fenix 8, Edge 1040);
the first live writes and the evidence are recorded in
[docs/PLAN.md](docs/PLAN.md).

## How it works

1. **Original FIT.** `garminconnect` downloads the activity's original
   export; the ZIP is unpacked in memory, the FIT is CRC-checked with
   Garmin's official FIT SDK and archived unchanged.
2. **Match.** The Intervals activity is found by `external_id` (the official
   sync stores the Garmin activity ID) or, failing that, by source, start
   time (±120 s) and duration (±5 %). Anything ambiguous is left alone.
3. **Gap.** The file Intervals received (`GET /activity/{id}/file`) is
   decoded next to the original. What only the original carries is the gap:
   whole message types, record fields (stream candidates), session fields
   (scalar candidates), developer fields, and Garmin-internal numeric IDs.
4. **Write back.** *Your own custom items* say what to write and from where.
   Intervals fills custom activity fields and custom streams from the FIT
   itself, and every definition names its source (`fit_session_field:
   "140.9"`, `fit_record_field: "stance_time"`, or a one-line script reading
   `m.f_138`), plus an optional first-import conversion. The bridge reuses
   those definitions verbatim: same fields, same source, same units, nothing
   Garmin-specific hard-coded. Scalars go through `PUT /activity/{id}`,
   streams through `PUT /activity/{id}/streams`, aligned to the activity's
   `time` stream by timestamp.
5. **Never overwrite real data.** An existing value is replaced only when
   its FIT source is present in the original and absent from the partner
   copy; then it cannot have come from data (Intervals stores `0` for a
   stripped source). A second run writes nothing.

Garmin does not filter every activity. The gap is measured per activity
and never assumed.

## Quick start

Python 3.12 or newer.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q                                   # synthetic tests, no accounts needed

export INTERVALS_API_KEY=...                # Intervals → Settings → Developer
export INTERVALS_ATHLETE_ID=0               # 0 = the key's owner
export BRIDGE_DATA_DIR=./data               # FITs, raw JSON, SQLite, tokens

garmin-intervals-bridge login               # once, interactive, MFA supported; stores tokens only
garmin-intervals-bridge probe               # newest original FIT, archived; no Intervals access
garmin-intervals-bridge gap --activity-id <garmin id>      # what did Garmin strip? read-only
garmin-intervals-bridge enrich --activity-id <garmin id>   # dry run: the exact plan
garmin-intervals-bridge enrich --activity-id <garmin id> --apply
```

Then let it run on its own:

```bash
garmin-intervals-bridge watch --apply       # one cheap Intervals poll; Garmin only on new activities
garmin-intervals-bridge sync --apply        # full run over the last days, activities + wellness
```

Everything is a dry run until `--apply` is given. The plan printed by a dry
run is exactly what `--apply` would send.

## Commands

| Command | What it does |
| --- | --- |
| `login` | Interactive Garmin sign-in (email, password, MFA). Only session tokens are stored. |
| `probe [--activity-id]` | Download and archive one original FIT. |
| `gap --activity-id` | Original vs. the copy Intervals holds; lists everything only the original carries. |
| `compare-fit A B` | Same comparison for two local files (e.g. against a manual browser export). |
| `enrich --activity-id [--apply] [--refresh-own-streams]` | Plan or perform the enrichment of one activity. |
| `watch [--apply]` | Poll Intervals once (~450 bytes); enrich activities seen for the first time. |
| `sync [--scope all\|activities\|wellness] [--mode enrich\|upload] [--apply]` | Scheduled run over the last days. |
| `backfill --scope wellness\|activities --from DATE [--to DATE] [--apply]` | Paced, resumable run over the past. |
| `setup-fields [--apply]` | Create the private custom wellness fields the mapping uses. |
| `setup-charts [--apply]` | Create (and later complete) private fitness charts for the synced values. |
| `status` | Pending uploads and failed activities with their retry time. |
| `health` | Daily digest: probes both services and reports errors that would otherwise stay silent (dead timers, endpoints failing for days, fields Garmin stopped delivering, activities failing repeatedly). Exit 2 only when a human is needed. |

## Modes

**enrich** (default). The official Garmin import stays on. Each imported
activity is completed with what its file lacks. This is the mode for
everyone who wants to keep Intervals' own import, training load and workout
pairing.

**upload** (`--mode upload`). The v0.1 behaviour for accounts that switched
the official activity import off: originals are posted as new activities,
guarded by a persistent *pending* state so a timed-out upload is never
replayed blindly, and blocked when an activity already exists nearby.
Intervals de-duplicates uploads by file hash, so with the official import
on this mode *would* create duplicates. Use it only with the import off.

## Wellness

Garmin's daily endpoints (sleep, HRV, stress, Body Battery, readiness,
respiration, SpO₂, hydration, scale, nutrition, intensity minutes, scores,
predictions, fitness age, lactate threshold, …) are fetched per day; every
raw response is archived as JSON. Values are written to Intervals only where
the day has nothing yet, locked days are skipped, and running totals of the
current day wait for tomorrow.

Native Intervals fields filled when empty: `restingHR`, `hrv`, `sleepSecs`,
`sleepScore`, `readiness`, `vo2max`, `steps`, `floorsClimbed`,
`hydrationVolume`, `spO2`, `respiration`, `avgSleepingHR`, `weight`,
`bodyFat`, and from logged food `kcalConsumed`, `carbohydrates`, `protein`,
`fatTotal`. Everything else goes to private custom fields named `Garmin…`,
listed in [docs/FIELD_MAPPING.md](docs/FIELD_MAPPING.md).
`BRIDGE_WELLNESS_PROFILE=recommended` (default) leaves out a handful of
duplicates and goals; `all` keeps every code.

Backfill the past when the official sync has gaps (on the reference account
SpO₂ stopped arriving in mid-2025):

```bash
garmin-intervals-bridge backfill --scope wellness --from 2025-01-01            # dry run
garmin-intervals-bridge backfill --scope wellness --from 2025-01-01 --pause 3 --apply
```

A backfill uses the twelve per-day measurement endpoints by default
(`--endpoints all` for every endpoint), pauses between days, and skips days
already fetched, so it can be interrupted and resumed.

## Charts

`setup-charts --apply` creates nineteen private fitness charts in your account
for the values the bridge syncs: readiness & recovery, sleep stages, stress
& Body Battery, HRV detail, nutrition intake vs. burn, Garmin Bridge: Endurance & hill scores, VO₂max
& fitness age, race predictions, intensity & hydration, skin temperature.
A chart only gets the fields that exist; run the command again after a
backfill with every endpoint and the bridge's own charts are completed.
Charts you made yourself are never touched, even with the same name.

Intervals' API cannot place a chart on a Fitness tab, so that last step is
yours: Fitness page → the tab you want → *custom charts* → tick the
`Garmin …` charts. If you like them, you can share them in Intervals'
chart library from the same dialog.

## Running it

**systemd (recommended, no container):** an unprivileged service user, a
one-minute `watch` timer and a 30-minute full run, secrets in an
`EnvironmentFile`, hardened units. See [deploy/README.md](deploy/README.md).

**Docker / Podman:** the image runs as a non-root user and takes the same
environment variables.

```bash
cp .env.example .env            # fill INTERVALS_API_KEY
mkdir -p data && chown 10001:10001 data
docker compose build
docker compose run --rm bridge login
docker compose run --rm bridge enrich --activity-id <garmin id>
docker compose run --rm bridge sync --apply
```

The compose service is deliberately not an always-on daemon; schedule
`docker compose run --rm -T bridge watch --apply` from cron or a timer.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `INTERVALS_API_KEY` | | Intervals API key (HTTP Basic, user `API_KEY`) |
| `INTERVALS_ATHLETE_ID` | `0` | `0` addresses the key's owner |
| `BRIDGE_DATA_DIR` | `./data` | Originals, partner copies, raw wellness JSON, SQLite state, lock files |
| `GARMIN_TOKEN_DIR` | `$BRIDGE_DATA_DIR/tokens` | Garmin session tokens (mode 0700) |
| `BRIDGE_TIMEZONE` | `Europe/Vienna` | Your local day boundary |
| `BRIDGE_ACTIVITY_LOOKBACK_DAYS` | `4` | `sync` window for activities (1–30) |
| `BRIDGE_WELLNESS_LOOKBACK_DAYS` | `3` | `sync` window for wellness (1–30) |
| `BRIDGE_WELLNESS_REFRESH_HOURS` | `4` | Re-read today this often; yesterday once after midnight and then every second period; older days once after midnight |
| `BRIDGE_WELLNESS_PROFILE` | `recommended` | `recommended` or `all` custom fields |
| `BRIDGE_GARMIN_REQUEST_DELAY` | `0.5` | Seconds between Garmin requests (minimum 0.25) |
| `BRIDGE_STALE_HOURS` | `24` | `health` alerts once a service has failed this long |

## Known limitations

- **Garmin's endpoints are private and undocumented.** They can change,
  rate-limit or block without notice. The mobile login path answers 429
  for some clients; the web path with MFA works. Keep request delays.
- **Writing streams marks the activity's intervals as edited**
  (`icu_intervals_edited = true`, not resettable through the API).
  Intervals then no longer regenerates that activity's intervals on
  re-analysis. Harmless for an imported activity whose laps exist, but
  permanent. Writing scalar fields does not trigger it.
- **Only definitions the bridge can read safely are used.** Field scripts
  of the forms `activity.X / 60`, `activity.X == 0 ? NaN : activity.X / 36`
  and stream scripts reading one `m.f_<n>` with constant factors are
  understood; anything else is listed as unsupported and left alone.
- **Manual activities** (no device file) are skipped. **Strava-sourced**
  activities are never touched.
- One Garmin account and one Intervals athlete per data directory.
- Intraday series (stress, heart rate, Body Battery curves) have no place
  in Intervals' one-value-per-day wellness model; they stay in the raw
  archive.

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `Garmin login needed` | Run `login` interactively in the same environment the service uses. |
| `429` during `login` | The mobile login path was refused; the web path usually follows. Do not retry in a loop. |
| `Another bridge instance is running (wellness)` | A backfill holds that scope's lock; activities keep running. |
| `outcome: unmatched` | The official import has not created the activity yet; the next run looks again. |
| `status` lists a *pending* upload | Upload mode: the request's outcome is unknown. Check Intervals before `reset-pending`. |
| `203/EXEC Permission denied` under systemd | SELinux: the virtualenv must not live under a web document root. |

## Security and privacy

Read [docs/SECURITY.md](docs/SECURITY.md). In short: no passwords stored,
tokens and the API key readable by the service user only, the data
directory is personal (routes, health data) and must never be committed or
shared, dry run by default, no deletions ever.

## Acknowledgements

[cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect),
Garmin's [FIT SDK](https://developer.garmin.com/fit/), the
[Intervals.icu API](https://intervals.icu/api/v1/docs), and the
[forum thread](https://forum.intervals.icu/t/garmin-fit-fields-not-coming-in-possible-garmin-is-filtering-data-prior-to-sending-to-intervals-icu/124287)
that documented the problem. Independent hobby project, not affiliated with
Garmin or Intervals.icu.

MIT licensed.
