# Garmin → Intervals.icu Bridge

**Experimental, unofficial, self-hosted original-FIT and wellness importer.**

[![Unit tests](https://img.shields.io/badge/tests-automated-blue)](#tests)

Garmin Connect's partner Activity API may transmit FIT files with Garmin-specific messages removed. The **original FIT export** available through Garmin Connect's private API may contain those messages. This project fetches the original bytes, backs them up locally and (after explicitly enabling writes) uploads them to Intervals.icu. It also fills missing Intervals daily wellness values and provisions private custom wellness fields for Garmin-specific metrics.

> **Current stage: v0.1.1 developer preview. NOT yet live-tested against a Garmin account and Intervals upload together.** Do not switch off a working sync until your account's original FITs and Intervals imports have been validated.

## Key features

- Uses [`cyberjunky/python-garminconnect`](https://github.com/cyberjunky/python-garminconnect) v0.3.17+ and `ActivityDownloadFormat.ORIGINAL`.
- Saves the extracted **original FIT byte-for-byte** (no repackaging/reencoding).
- FIT structure checks and optional local message/field comparison via `fitparse`.
- Garmin ID-based local archival, SQLite state and single-instance locking.
- Conservatively blocks uploading activities with matching Intervals `external_id` or UTC start within 3 minutes. Never silently deletes/replaces existing activities.
- Persisted **pending** state before POST; network errors will not trigger a blind reupload.
- Wellness: read each day's Garmin API data; preserve original response JSON for future mappings; map verified Garmin numeric data to native or private Intervals custom daily fields.
- Existing manual/official-sync values and Intervals **locked** wellness days are never overwritten.
- **DRY RUN by default**. Activity upload additionally requires explicit `--allow-activity-upload` (after disabling Intervals' official Garmin *activity download*, if appropriate).
- Docker image, Docker Compose, test suite, and English documentation ready for a future public repository.

## Limits / what "all data" means

Intervals.icu supports native wellness fields, **numeric custom wellness fields**, and FIT activity streams; it does **not** offer an arbitrary time-series importer into the wellness data model. The bridge therefore:

1. Writes only well-defined scalar metrics (see [FIELD_MAPPING.md](docs/FIELD_MAPPING.md)).
2. Retains entire Garmin daily API JSON responses privately in `data/raw/YYYY-MM-DD.json`, including detailed HRV, stress, Body Battery, respiration, sleep stages, status and data the Intervals wellness API cannot represent as a single scalar.
3. Retains original FIT files in `data/fits/`, which carry activity streams and supported Garmin-specific FIT messages.

Some Garmin metrics may be unavailable for individual devices/accounts or fail if Garmin changes its private endpoints. Unsupported/unavailable metrics are **not invented** and the associated source error is recorded in the local archive.

## Quick start – local Linux/macOS

Python **3.12+** is required by the current `garminconnect` library.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
# Fill INTERVALS_API_KEY in .env (only needed for Intervals calls).
set -a; . ./.env; set +a

# First login requires a terminal, including Garmin MFA if applicable.
garmin-intervals-bridge login

# Safe first proof of concept: fetch a single original FIT. No Intervals auth needed.
garmin-intervals-bridge probe
# Or: garmin-intervals-bridge probe --activity-id 123456789

# Download the same "Export Original" via Garmin Connect web, extract the .fit,
# and compare both files locally to see whether fields are really present.
garmin-intervals-bridge compare-fit data/fits/123456789.fit original-from-browser.fit

# Read Garmin/Intervals and archive locally. Does not write to Intervals.
garmin-intervals-bridge sync

# Preview private Intervals custom wellness definitions.
garmin-intervals-bridge setup-fields

# Only after reviewing the dry-run: create needed fields and fill missing wellness.
garmin-intervals-bridge sync --scope wellness --apply
```

API key: Intervals.icu → **Settings** → Developer/API key. You may use `INTERVALS_ATHLETE_ID=0` for the authenticated owner (no athlete ID needs to be hardcoded in this public repository). The project never requires putting Garmin credentials in code or in `.env`. Garmin session tokens are saved to `GARMIN_TOKEN_DIR` after the interactive login.

**Never commit your `.env`, Garmin token directory, SQLite database, JSON snapshots or FIT files.** These are ignored by Git. Restrict access to your data volume and preferably encrypt it.

## Original-FIT import: separate safety switch

The official Intervals Garmin **activity download** must not run in parallel with this uploader unless you accept that duplicate detection is imperfect. We intentionally do **not** change Garmin account connections or Intervals settings programmatically. In particular, keep useful official **wellness syncing** enabled if your account supports it.

Only **after** the dry-run verified originals and duplicate handling:

```bash
# Start with one recent activity and check Intervals results carefully.
garmin-intervals-bridge sync --scope activities --activity-days 1 \
  --apply --allow-activity-upload

# Subsequent scheduled runs (both datasets):
garmin-intervals-bridge sync --apply --allow-activity-upload
```

The bridge searches Intervals for activity starts ±3 minutes around the Garmin UTC start, before and again immediately before uploads. This prioritizes **avoiding duplicate training load** over automatically importing every close-in-time activity. Activities without reliable UTC start timestamps are blocked. It never deletes activities or tries to merge Garmin FIT data into an existing filtered activity.

If an upload times out, it stays `pending` and future runs will not resubmit. After checking that the activity is *not* present in Intervals:

```bash
garmin-intervals-bridge status
garmin-intervals-bridge reset-pending --activity-id 123456789 --i-checked-intervals
```

**Never reset a pending upload without manually verifying that the Intervals activity does not exist.** Intervals' 200 response to an upload means no activity was created, while 201 means at least one activity was created. The client supports both the current OpenAPI UploadResponse object (`activities` array) and the older response-array format described in the forum.

### Wellness merge policy

- Respect Intervals `locked` days.
- Never replace non-null existing standard or custom values, including BodyBatteryMax/Min already imported by the official connection.
- Do not map Garmin 0–100 stress into Intervals 0–4 *subjective* stress.
- Do not copy Garmin training loads into Intervals CTL/ATL; they are different models.
- Do not import still-accumulating daily activity/step/hydration totals for **today**. These are filled on subsequent days, after the day is complete.
- Per-day Garmin raw payloads and response errors are archived, even if a metric has no Intervals mapping. The tool may skip API endpoints not supported on a particular account.
- New custom fields are **private** and created only when necessary during `sync --apply --scope wellness`. Adding them to your calendar views/charts may require adjusting your Intervals UI.

### Schedule via cron or systemd

Run the one-shot command every 30–60 minutes; *wellness* calls are automatically throttled by a default 8-hour freshness window. For example:

```cron
*/30 * * * * cd /opt/garmin-intervals-bridge && .venv/bin/garmin-intervals-bridge sync --apply --allow-activity-upload >> /var/log/garmin-bridge.log 2>&1
```

Only enable `--allow-activity-upload` if you have verified the import and configured Intervals to avoid parallel Garmin activity imports. For wellness-only operation, instead use `sync --apply --scope wellness`.

For cron, use a secured environment file or systemd `EnvironmentFile=` for the Intervals API key; cron does not automatically read `.env`. Make sure no plaintext credentials or raw Garmin responses are logged.

## Docker

```bash
cp .env.example .env
mkdir -p data
# The container runs as non-root UID 10001; mount must be writable by it.
sudo chown -R 10001:10001 data

docker compose build
docker compose run --rm bridge login            # interactive token setup
docker compose run --rm bridge probe            # read-only external
docker compose run --rm bridge sync             # dry run
docker compose run --rm bridge sync --scope wellness --apply
```

`compose.yaml` is deliberately **not** a permanently running service. For automation, call `docker compose run --rm -T bridge sync ...` from host cron/systemd, or a scheduler of your choice. Keep `.env` outside a public volume. Run the same data directory across all invocations so pending locks and session tokens persist.

## Configuration

See [.env.example](.env.example). Options:

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `INTERVALS_API_KEY` | empty | Intervals API password (Basic Auth username `API_KEY`) |
| `INTERVALS_ATHLETE_ID` | `0` | Authenticated owner by default |
| `BRIDGE_DATA_DIR` | `./data` | FITs, raw JSON, SQLite, lock |
| `GARMIN_TOKEN_DIR` | `./data/tokens` | Garmin session tokens; restrict permissions |
| `BRIDGE_TIMEZONE` | `Europe/Vienna` | Account-local day boundary; change to your timezone |
| `BRIDGE_ACTIVITY_LOOKBACK_DAYS` | `4` | Look back for new activities |
| `BRIDGE_WELLNESS_LOOKBACK_DAYS` | `3` | Backfill last three days including today |
| `BRIDGE_WELLNESS_REFRESH_HOURS` | `8` | Wellness API call throttle |
| `BRIDGE_GARMIN_REQUEST_DELAY` | `0.5` | Pause between Garmin API calls in seconds |

## Tests

```bash
pip install -e '.[dev]'
pytest -q
python -m compileall -q src
```

Tests use fully **synthetic fixtures** and fake Garmin/Intervals clients. They do not log into Garmin, use personal files, or write real Intervals records. They cover mapping semantics, wellness locks, partial updates, FIT structure, replay protection, remote activity conflict detection, and upload failures. Real two-platform integration still needs an opt-in acceptance test (see below).

## Real-account acceptance checklist (before public release)

1. `login` works with MFA and resumes without password afterward.
2. `probe` returns an original FIT; **compare with the same activity downloaded from Garmin Connect web**, including FIT message types and developer fields.
3. Dry-run sees existing Intervals activities and does **not** upload a duplicate.
4. Dry-run reports expected Garmin wellness values without writing anything.
5. With a *test day*, `sync --scope wellness --apply` creates private fields, writes only missing values, and does not overwrite manual/locked data.
6. After disabling the overlapping official activity import, import **one** fresh activity; verify correct FIT-derived messages, workout steps, sensors, power and normal training load.
7. Verify sleep, HRV, Body Battery and planned workouts continue to sync in the intended direction.
8. Restart the container / repeat the same sync; confirm no duplicate activities or custom fields.
9. Ensure all logs/configuration/history are free of Garmin tokens, Intervals API keys and personal health/route data before any public repo push.

## Sources / technical background

- [Garmin Grafana](https://github.com/arpanghosh8453/garmin-grafana) inspired original-FIT archival via `KEEP_FIT_FILES`.
- [Python Garmin Connect](https://github.com/cyberjunky/python-garminconnect) provides Garmin Connect private API authentication and original activity exports.
- [Intervals API docs](https://intervals.icu/api/v1/docs) documents upload, wellness partial update and custom-item endpoints.
- [Intervals custom wellness fields](https://forum.intervals.icu/t/custom-wellness-fields/23188) explains custom daily numeric fields.
- [Garmin FIT filtering discussion](https://forum.intervals.icu/t/garmin-fit-fields-not-coming-in-possible-garmin-is-filtering-data-prior-to-sending-to-intervals-icu/124287) explains the motivation.

This is an independent hobby project, not affiliated with Garmin or Intervals.icu. The Garmin Connect endpoints are private and undocumented: behavior, availability, account rate limits, and access rules can change without notice. Respect provider terms and avoid excessive polling.

## Roadmap

See [ROADMAP.md](docs/ROADMAP.md) and [SECURITY.md](docs/SECURITY.md). Contributions are welcome after the first verified end-to-end sync.

MIT licensed.
