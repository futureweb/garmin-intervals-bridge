# Plan: from v0.1.1 preview to a releasable bridge

Status: written 2026-10-08 after auditing the v0.1.1 archive, the live
Intervals.icu OpenAPI document, the `garminconnect` 0.3.17 source and the
Intervals forum thread that motivated this project. Supersedes the roadmap
in `ROADMAP.md`, which documents the v0.1.1 state.

## 1. What the audit found

**The craftsmanship of v0.1.1 is sound.** Fail-closed defaults, a durable
`pending` state before every upload, atomic file writes, a single-instance
lock, synthetic tests, no secrets in code. Every Garmin method it calls
exists in `garminconnect` 0.3.17 with the signature it assumes (checked
against the library source, including `ActivityDownloadFormat.ORIGINAL`,
`get_race_predictions(..., _type=)` and `get_lactate_threshold(*, latest=,
start_date=, end_date=, aggregation=)`).

**The strategy is the problem.** v0.1.1 uploads the original FIT as a *new*
activity. Intervals de-duplicates uploads by a hash of the file (stated in
the API description of `POST /athlete/{id}/activities`). The original FIT
and the Garmin-filtered FIT have different bytes, so the upload always
creates a duplicate while the official Garmin import is active. That is why
the v0.1.1 README tells users to switch the official activity import off.
It is exactly the workflow the forum thread calls painful: every activity
must then be imported by the tool, and a missed one is simply gone.

**The API already offers a better path.** Two endpoints v0.1.1 does not use:

| Endpoint | Capability |
| --- | --- |
| `PUT /api/v1/activity/{id}/streams` | Add streams to an *existing* activity. Body is `ActivityStream[]`; the schema carries an explicit `custom: true` flag. |
| `PUT /api/v1/activity/{id}` | Update fields of an existing activity. |
| `GET /api/v1/activity/{id}/file` | Download the file Intervals *received from Garmin*. |

So the official sync can stay on. It creates the activity, computes training
load and pairs workouts, as it does today. The bridge then fetches the
original FIT, finds the matching Intervals activity and **adds only what
Garmin stripped**. No duplicates, nothing to disable, no change to training
load, and a missed activity is just the filtered version. `GET .../file` even
allows an automatic per-activity gap report (original vs. what Intervals has)
without a manual browser export.

### Concrete defects in v0.1.1

1. **Livelock.** One failing original-FIT download (manual activity without a
   file, a transient 5xx) raises out of `sync_activities` and aborts the whole
   loop, on every run. Later activities are never processed. Needs per-activity
   isolation and a `failed` state with backoff.
2. **Guessed wellness shape.** Custom wellness values are written as a nested
   `customFields` object. The tests only assert the project's own guess. Forum
   evidence suggests custom codes appear as ordinary top-level keys. If the
   guess is wrong, every custom write silently does nothing. One read-only GET
   of a day that already has `BodyBatteryMax` settles it.
3. **`fitparse` is unmaintained** (last release 2020-09). Replace with the
   official `garmin-fit-sdk`, which decodes developer fields and keeps unknown
   message/field IDs. That matters: `stamina`, `potential_stamina`,
   `recovery_time`, `sweat_loss`, `training_status` are *not* in the public FIT
   profile. They arrive as Garmin-internal numeric IDs and get named by
   inspecting a real original file. `performance_condition`,
   `total_training_effect`, `total_anaerobic_training_effect`, `vo2_max` and
   `workout_step` are in the profile.
4. **Matching is too loose for writing.** A start time within +/-3 minutes is
   fine for *blocking* an upload, not for *writing into* an activity. Add
   `source == GARMIN_CONNECT`, a duration tolerance, and `external_id` once its
   real format is known.
5. Smaller: compose lacks `cap_drop`/`no-new-privileges`/`read_only`; CI has
   no lint or dependency audit; HTTP 429 on writes is not handled.

### Open questions: answered 2026-10-08 with read-only calls

- **Original download is genuine.** The file fetched through the private
  Garmin API is byte-identical (same SHA-256) to the file the user exported
  manually from Garmin Connect web for the same activity.
- **`external_id` of the official sync** is the plain Garmin activity ID
  (`24544097680`, `source: GARMIN_CONNECT`). Manually uploaded files carry
  `<garmin id>_ACTIVITY.fit` with `source: UPLOAD`. Matching uses both.
- **Custom fields are top-level keys named by their code**, on wellness days
  (`BodyBatteryMax` next to `restingHR`) and on activities (`AerobicEffect`,
  `RecoveryTime`, `Sweatloss`, `PerformanceCondition`, `VO2MaxGarmin`,
  `Staminaatstart`, ... as keys of `GET /activity/{id}`). No `customFields`
  object exists; v0.1.1's nested shape was wrong and is fixed.
- **Streams** come back as `[{type, name, custom, data: [N values], ...}]`,
  every stream aligned to the `time` stream. Custom streams carry
  `custom: true` and use their custom-item code as `type` (`Battery`,
  `Elapsedtime`). That is the template for `PUT /activity/{id}/streams`.
- **Athlete id `0`** addresses the key owner on every athlete-scoped endpoint.
- **Garmin does not filter every activity.** A hike synced on 2026-09-29 is
  byte-identical between original and partner copy, `user_profile` included.
  The gap must be measured per activity, which `gap` does; it must never
  assume a gap exists.
- **The account already defines the target fields.** The user's Intervals
  account has custom activity fields and custom streams for exactly the
  stripped data (`AerobicEffect`, `AnaerobicEffect`, `RecoveryTime`,
  `PerformanceCondition`, `VO2MaxGarmin`, `Sweatloss`, `Stamina`,
  `PotentialStamina`, `Staminaatstart`, `Staminaatend`, `MinimumStamina`,
  ...). Many are public community items. Intervals fills them itself when
  the file contains the data; the bridge's job in enrich mode is to supply
  the values when the partner copy does not.
- **Current workflow on this account:** the official activity import is
  **on**. The user deletes each auto-imported (filtered) activity and uploads
  the Garmin original by hand, which is why recent activities show
  `source: UPLOAD`. That is the workaround the forum thread calls painful,
  and it is what `enrich` mode removes: the filtered copy stays, the bridge
  fills in what it lacks. `upload` mode remains for accounts that switch the
  official import off.

## 2. Phases

### Phase 0: foundation (done 2026-10-08)

- Checkout at `<checkout>/`, v0.1.1
  committed verbatim as the baseline so every change is a reviewable diff.
- The checkout sits below an Apache document root: `.htaccess` denies all HTTP
  access.
- Runtime data lives **outside** the checkout in
  `/var/lib/garmin-intervals-bridge/` (mode 0700), owned by the dedicated
  unprivileged service user `gib` (no login shell). `gib` has read/execute on
  the checkout and no write access to it.
- Python 3.12 venv in `.venv/`, all 22 synthetic tests pass.

### Phase 1: the actual problem (core)

1. Swap `fitparse` for `garmin-fit-sdk`; extend the FIT inventory to list
   unknown message and field IDs with sample values.
2. Fix the livelock: per-activity error isolation, `failed` state, backoff.
3. Read-only verification with the API key (the three open questions above).
4. `enrich` mode (new default): original FIT -> match the Intervals activity ->
   gap analysis against `GET /activity/{id}/file` -> add missing streams via
   `PUT /streams` and missing scalars via `PUT /activity` (or the description
   fallback) -> record in SQLite, idempotent by file hash and field list.
5. Keep the v0.1.1 upload path as the explicit opt-in `upload` mode for users
   who deliberately disable the official import; keep its pending/201/200
   safety logic.
6. Acceptance: `probe` on a known activity, byte-hash comparison with the
   manual Garmin Connect export of the same activity, gap report, then a
   dry run that prints exactly what it would write. **The first real write
   touches one activity and happens only after explicit approval.**

### Phase 2: wellness

Verify the custom-field shape with a GET, then reuse the v0.1.1 mapping,
which is mostly done. Start with a short field list; the raw JSON archive
keeps everything else. Keep every v0.1.1 safety rule: never overwrite a
non-null value, respect locked days, never finalise today's running totals.

### Phase 3: operation and release

- systemd service + timer (every 30 minutes) running as `gib`, secrets in an
  `EnvironmentFile` outside the checkout, dry run unless the write switch is
  set.
- Keep the Dockerfile and compose file as deliverables for other users; build
  and smoke-test the image once with `podman build` on this host.
- Docs in English: README, architecture, field mapping, troubleshooting,
  security and privacy, known limitations, changelog. CI with ruff and
  pip-audit. MIT. Secret scan of the whole history before the first push.
- Public repository and a versioned release once the end-to-end test has been
  approved.

## 3. Why no container on this host

The development host has Podman 5 but no Docker and no compose provider,
SELinux is enforcing, and the job runs for a few seconds every 30 minutes.
A venv plus a systemd timer under a dedicated user gives the same isolation
with the fewest moving parts. The image is still built and tested for
everyone else.

## 4. Rules that do not change

- Dry run by default. Real writes need the explicit switch.
- Never delete or replace an Intervals activity. Never touch training load.
- Never overwrite a non-null wellness value. Respect locked days.
- No credentials in the chat, in logs, in Git or in test fixtures. Tests use
  synthetic data only.
- Garmin endpoints are private and undocumented: rate-limit, back off, and
  never invent a metric a device did not provide.
