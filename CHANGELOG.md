# Changelog

## 0.2.1 — 2026-10-09

The day Garmin's new partner terms surfaced: Intervals expects to have to
block Garmin-sourced data in its API; files an athlete uploads are expected
to stay accessible. This release makes the bridge's upload mode the
first-class answer and completes the local mirror.

### Added

- **Upload mode, first-class and live-verified.** `run --mode upload` polls
  Garmin every ten minutes and uploads the original of every new activity
  as the athlete's own file; `backfill --scope activities --mode upload`
  does the same for the past; a 10-minute upload timer in `deploy/`.
  Intervals computes every custom field and stream from the complete file
  itself, nothing is flagged as edited.
- A complete local mirror: eight more daily endpoints in the archive
  (all-day heart rate, intraday steps and floors, Body Battery and all-day
  events, four-week load balance, resting HR course, daily training status),
  `--endpoints` takes a list of keys to add to days fetched earlier,
  `backfill --scope activities --archive-only` downloads every original FIT
  plus Garmin's summary, weather, gear and exercise sets without touching
  Intervals, and `snapshot-account` archives profile, settings, devices,
  zones, gear, personal records, badges, workouts and training plans.
- Container image on ghcr.io (amd64 + arm64), built by GitHub Actions on
  every release; compose pulls it.
- Monthly `snapshot-account` timer in `deploy/`.
- README: legal note (your account, your data, Garmin's terms, trademarks),
  how login works in the container, screenshots.

### Changed

- Readiness: the score is the morning's, recovery time and acute load are
  the day's last reading (a hard session shows its recovery on its own day);
  `GarminRecoveryTimeHours` backs the chart; `backfill --rewrite` replaces
  the bridge's own `Garmin…` values from the archive after such a correction.
- VO₂max is written once the day is over, weigh-ins the same day, an
  unlogged hydration day stays empty, floors are whole numbers.
- Weight in the charts as a 7-day average; daily weigh-ins as dots.

### Fixed

- Chained FIT files (two complete files back to back in one Garmin
  original) are accepted and decoded together.
- A 4xx answer from a Garmin endpoint that did not exist for an old date no
  longer counts as an outage; a port number in an error message is no longer
  read as an HTTP status.
- The scheduled sync does the free scope while a backfill holds the other,
  instead of failing (and alerting).

## 0.2.0 — 2026-10-08

First version used in anger. Everything below was verified against a live
Garmin and Intervals.icu account; see `docs/PLAN.md` for the evidence.

### Changed

- **Enrich instead of upload.** The default mode keeps the official Garmin
  import and adds to the imported activity what its filtered file lacks,
  via `PUT /activity/{id}` and `PUT /activity/{id}/streams`. Intervals
  de-duplicates uploads by file hash, so the v0.1 upload created a
  duplicate whenever the official import was on. Upload remains available
  as `--mode upload` for accounts with the import switched off.
- **Field definitions come from the athlete's own custom items**
  (`fit_session_field`, `fit_record_field`, single-field stream scripts,
  first-import conversions). Nothing Garmin-specific is hard-coded.
- **`fitparse` replaced by Garmin's official FIT SDK.** CRC is verified;
  unknown messages and fields keep their numeric IDs with sample values,
  which is where the stripped data lives.
- Custom wellness fields are written as **top-level keys**; v0.1.1 used a
  nested `customFields` object that the API does not have, so every custom
  write would have been ignored.
- Activity matching recognises the official sync's bare Garmin ID, manual
  uploads (`<id>_ACTIVITY.fit`) and the bridge's own `garmin:<id>`.

### Added

- `gap`: original vs. the copy Intervals holds, per activity.
- `enrich`, `watch` (one-minute poll, Garmin only on new activities),
  `backfill` (paced, resumable), `--refresh-own-streams`.
- Per-activity failure isolation with exponential backoff; a Garmin-wide
  block aborts the run without blaming an activity; uploads with unknown
  outcome stay pending.
- Native wellness fields `spO2`, `respiration`, `avgSleepingHR`,
  `floorsClimbed`, `weight`, `bodyFat`, and `kcalConsumed`,
  `carbohydrates`, `protein`, `fatTotal` from Garmin's nutrition log.
  `BRIDGE_WELLNESS_PROFILE` (`recommended` / `all`).
- Per-scope instance locks, so a long wellness backfill does not stall the
  activity watcher.
- Hardened systemd units (`deploy/`), container image smoke-tested with
  Podman, CI with ruff, pip-audit and pytest.

- `setup-charts`: nineteen private fitness charts for the synced values, using
  Intervals' real chart field ids (read from its app bundle), unique item
  indexes (otherwise the chart picker hides them), and completion of the
  bridge's own charts when fields appear later.
- Past days are re-read once after local midnight so finished totals do not
  wait for the throttle; default throttle 4 h.
- The watcher logs in to Garmin lazily: a poll that finds nothing new makes
  no Garmin request at all.

- `health`: daily probe of both services with a configurable stale window
  and alert mail; failure alerts for the timers (`deploy/`).
- `run`: the three timers in one long-running process, for Windows and
  anything without systemd; `.env` file support; file locks on Windows;
  `python -m garmin_intervals_bridge`. Step-by-step Windows guide.

### Fixed after the code review

- A Garmin outage or rate limit at login time was reported as "login
  needed" and, in the watcher, booked against the activity being processed.
  It now ends the run as a Garmin block, and `health` tells the two apart.
- The library's own retries (4 attempts per endpoint) are off; a day whose
  endpoints fail three times in a row aborts the run instead of trying the
  remaining twenty.
- `watch` exits 0 when the sync or a backfill holds the activities lock; it
  used to fail, which fired the alert unit and used up its throttle.
- Dry runs now count as Garmin reads for the throttle, and the first
  `--apply` maps the archived snapshot instead of fetching again.
- Yesterday is re-read once after midnight and then every second refresh
  period, older days once after midnight; refreshes within a day read only
  the measurement endpoints. About 160 instead of 450 wellness requests a day.
- `backfill --scope wellness --from-archive` completes the past from the
  local archive after the mapping gained fields, without Garmin requests.
- An activity that failed once and then succeeded no longer stays "failed"
  in `status` and `health`.
- Intervals `Retry-After` is capped at 30 s; custom items are read once per
  process; Intervals activity ids are validated before they enter a URL.
- Matching: a bare numeric `external_id` counts only for `GARMIN_CONNECT`
  activities; Garmin's timer duration is compared with moving and elapsed
  time; numeric-string select options are understood.
- The alert unit template was committed empty; `deploy/` now ships it with
  the install step for `gib-alert`.
- Streams are only written when a stream both sides have (heart rate, else
  cadence) lines up point by point after the same alignment; a shifted
  origin is caught instead of written.
- Activities enriched earlier are revisited when the athlete's field
  definitions change; the partner copy is re-read when the Intervals
  activity was replaced.
- Today's resting HR is left to tomorrow (Garmin revises it during the
  day); a short activity window costs one Garmin request instead of two;
  the enrich candidate list is fetched with a field list; FIT downloads are
  capped at 32 MB.

### Known limitations

- Writing streams sets `icu_intervals_edited` on the activity; the API
  does not reset it.
- Garmin's endpoints are private; the mobile login path may answer 429.

## 0.1.1 — developer preview

Original-FIT download and archive, upload mode with pending state,
wellness mapping, Docker, synthetic tests. Never live-tested.
