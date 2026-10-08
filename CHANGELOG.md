# Changelog

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

- `setup-charts`: fifteen private fitness charts for the synced values, using
  Intervals' real chart field ids (read from its app bundle), unique item
  indexes (otherwise the chart picker hides them), and completion of the
  bridge's own charts when fields appear later.
- Past days are re-read once after local midnight so finished totals do not
  wait for the throttle; default throttle 4 h.
- The watcher logs in to Garmin lazily: a poll that finds nothing new makes
  no Garmin request at all.

- `health`: daily probe of both services with a configurable stale window
  and alert mail; failure alerts for the timers (`deploy/`).

### Known limitations

- Writing streams sets `icu_intervals_edited` on the activity; the API
  does not reset it.
- Garmin's endpoints are private; the mobile login path may answer 429.

## 0.1.1 — developer preview

Original-FIT download and archive, upload mode with pending state,
wellness mapping, Docker, synthetic tests. Never live-tested.
