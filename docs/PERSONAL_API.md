# Garmin Connect Personal API

The default `GARMIN_SOURCE=auto` selects direct login without a Personal key,
Personal with a key and no saved direct session, or hybrid when both exist.
Existing installations without a key keep their direct-login behavior. You can
also explicitly select `direct`, `personal`, or `hybrid`.

Create a key while signed in to Garmin Connect Web, following Garmin's
[Personal API instructions](https://developer.garmin.com/gc-developer-program/personal/personal-api/).
Copy the key and the expiry date shown next to it. Treat the key like a password.

## Configuration and setup

| Environment setting | Default | Meaning |
| --- | --- | --- |
| `GARMIN_SOURCE` | `auto` | `auto`, `direct`, `personal`, or `hybrid` |
| `GARMIN_PERSONAL_API_KEY` | empty | Secret Personal API key |
| `GARMIN_PERSONAL_API_KEY_EXPIRES` | empty | Optional expiry date, strictly `YYYY-MM-DD` |

Use the same private settings file or container environment as your Intervals
key. The wizard and web Settings offer these choices. A blank password field
keeps the saved key; explicit removal clears it. Replacement keys are validated
in an isolated temporary cache before saving. The key is never returned to the
browser. Explicit Personal/hybrid settings in the web form require a key;
select auto or direct to remove it. When replacing a key, enter its new expiry or
leave it blank for unknown; the old date is not carried over. The date is only
for reminders: Garmin decides whether a key is valid. Restart after changes.
To remove an expiry without replacing the key, select **Clear the saved expiry (keep the key)**.

Home Assistant offers `garmin_source` (default auto),
`garmin_personal_api_key` (password), and `garmin_personal_api_key_expires`.
Auto and hybrid also offer direct sign-in when a saved session is missing. Only
explicit Personal mode disables direct sign-in. Both credentials must belong to
the same Garmin account. The bridge compares the authenticated daily summary's
`userProfileId` with the direct client's profile ID before using shared Personal
data. It remembers the verified key fingerprint and direct profile ID across
runs. Direct-only work does not require Personal to answer. A new key is checked
against the saved direct profile even while direct sign-in is waiting; a mismatch
blocks Personal activities and marks only the Personal keys in mixed wellness
snapshots as failed. Direct wellness, extras, files and account data continue.
The first pairing needs a known direct account identity, checked before contacting Personal.

## Credential recovery and reminders

A missing direct login leaves Personal activity and the four hybrid wellness
keys running. Direct-only work waits for sign-in, a successful health probe, or an hourly
automatic sign-in retry.
The wait survives timer-process restarts. Deferred wellness days resume oldest
first, at most three per run, with a two-second pause per day; deferred activity
extras also resume at most three per full run. Completed queue entries are
removed, and missing/corrupt activity archives cannot block later entries.
Direct 429s and connection failures defer direct work for 15 minutes without a
sign-in notice. This backoff also survives restarts; verified Personal work continues.

A missing key or token endpoint HTTP 401 triggers
**Personal API key needs replacement**, once per episode through configured
alerts and, on an applying run, an Intervals note. Hybrid then uses direct login
for discovery, detail, original FIT, and its four shared wellness keys. Rejected
keys are tried at most hourly; token HTTP 400 follows the same recovery path
with a missing/malformed-key explanation; a changed key retries immediately. Personal takes
over when it works again. Status, health, and the web page show the effective
source (for example `auto → hybrid`) and the last providers used for routed data.

No fallback occurs on HTTP 403, 429, 5xx, timeouts, or persistent data HTTP 401.
A data endpoint 403 is recorded for that endpoint, allowing the other wellness
keys to finish; health reports errors persisting across three archived days.
A data 401 renews and replays once. If that also returns 401, a separate visible
authorization episode suspends that endpoint for six hours. Other endpoints
can continue, including other keys in the same wellness snapshot. Failures are
tracked by endpoint across dates and activities. A second endpoint failing in
that window, or a settings endpoint failure, suspends all Personal requests for
six hours to prevent repeated token issuance. Successful key validation in web settings or setup, or an explicit
`personal-check`, clears the suspension even for the same key. Other token-service
4xx responses and invalid token replies stop the whole Personal operation and
back off for an hour, avoiding one token request per dataset. Token-service 5xx,
timeouts and connection failures back off for 15 minutes; scheduled sync/watch
commands log a warning and exit successfully during this short outage.
HTTP 429 uses a Personal-only pause with Retry-After and a 15-minute minimum.
Hybrid runs continue direct wellness and account work when Personal activities
fail or are paused; Intervals-only work retains its independent pause state.

With a configured expiry date, the bridge sends one reminder from 14 days
before expiry and one more from three days before, deduplicated by key
fingerprint, expiry date and threshold. If several thresholds are already due,
only the most urgent reminder is sent. Calendar notes wait for an applying run
with Intervals notes enabled. The date and days remaining appear in status,
health and the web page. A fresh token with a JWT lifetime clearly below 24 hours
(more than five minutes shorter) also triggers one “expires within a day”
reminder per key/date. This catches the FAQ behavior where tokens end with the
key. An ordinary 24-hour token does not reveal the key's expiry date.

## Coverage

Personal mode is a partial mirror. Direct mode retains the existing coverage.
Only measurements Garmin actually supplies are mapped; missing fields are not
invented as zeros.

| Data | Personal mode | Normal hybrid route |
| --- | --- | --- |
| Activity discovery, detail, original FIT | Supported, with pagination and FIT/ZIP validation | Personal |
| Stats, HRV, Body Battery, nutrition | Supported; HRV 204 means no data | Personal |
| Sleep duration, score, stages, sleeping HR, respiration, SpO₂ | Bare DTO wrapped for the mapper; all-null DTO means no data | Direct |
| Sleep skin-temperature deviation, overnight battery change, wrapper RHR and full sleep charts | Missing | Direct |
| Hydration | Requested day's entry; absent sweat loss remains absent | Direct |
| Weight, body composition, weigh-ins | `weight/range/S/E?includeAll=true`; all target-day `dailyWeightSummaries[].allWeightMetrics`, latest first | Direct |
| Fitness age | Single dictionary, also accepting the older array shape | Direct |
| Daily heart rate, SpO₂/acclimation | Supported archive endpoints | Direct |
| Readiness, training status/load, VO₂max, endurance/hill scores, predictions | Missing as daily datasets | Direct |
| Lifestyle, blood pressure, lactate threshold, intraday steps/stress/floors, separate RHR and event feeds | Missing | Direct |
| Activity weather, gear, exercise sets, typed splits and split-summary extras | Missing as separate datasets | Direct |
| Original wellness files and FIT Health Snapshots | Missing | Direct |
| Account settings, heart-rate zones | Supported subset | Direct account snapshot |
| Account profile, devices, gear, records, badges, plans, workouts, goals and histories | Missing | Direct |

Garmin documents the weight in kilograms, but the live API answers in grams,
like the direct login; only positive values below 1000 are converted from
kilograms. The bridge writes kilograms to Intervals. Range averages and
adjacent-day entries are not measurements for the requested day. The broken `weight/all` route is
not used. Personal stats have a null `lastSyncTimestampGMT`; personal/hybrid
skip that probe and use sleep evidence or the existing 20:00-next-day fallback
for final reads. Direct scheduling is unchanged.

Unsupported capabilities do not create retry loops or field-dropout alarms.
Health still monitors unexpected loss of supported fields.

## Token contract and storage

The measured token endpoint returns HTTP 200, `text/plain`: the body itself is
a JWT. The bridge strips it and reads base64url JSON claim names and scheduling
claims without verifying the signature; claims are not used to authenticate
account identity. With both `exp` and `iat`, usability is scheduled from local
time for `min(exp - iat, 24 hours)` minus a margin, tolerating clock differences.
Without `iat`, expiry is capped at 24 hours from local time; unreadable/missing
expiry falls back to 24 hours. JSON access-token envelopes
remain accepted for robustness. Requests use Bearer authentication, no redirects
or .netrc credentials, the bridge User-Agent, and the normal proxy/CA environment.

The private cache is `$BRIDGE_DATA_DIR/personal-api/token.json`, separate from
direct session tokens (directory 0700, file 0600, atomic writes). A valid token
is read before taking a lock. Renewals wait up to 90 seconds and recheck inside
the lock, so concurrent commands reuse a single issuance. Request pacing occurs
after releasing the lock. The cache records a key fingerprint, never the key;
private failure state persists rejected-key backoff and authorization incidents.

Wellness archives record source/provenance. `raw_personal` stores only payloads
changed by an adapter. Fresh stats, HRV, Body Battery and nutrition always
replace old totals. Only completed direct reads of lossy sleep, weight,
hydration and SpO₂ payloads are retained; their provenance says they were
retained, and adapted Personal raw data remains available. Intraday direct
payloads never override fresh final Personal data.

Activity detail has `_bridge` provenance without a duplicate raw summary;
original FIT downloads have a `.provenance.json` sidecar. A Personal detail next
to an existing direct detail uses `<id>.personal.json`, excluded from activity
polling counts. Personal account snapshots live in `raw/account-personal/`, so
they cannot displace the direct gear catalogue. Deferred account snapshots are
not saved.

## Diagnostics and evidence

Run `garmin-intervals-bridge personal-check` after configuring a key. It explicitly
issues a token and checks user settings. Output includes HTTP status, content
type, response field names/types for JSON, token kind, JWT claim names, lifetime
in hours, and key-set state. It prints no key, token, claim values or settings
values, and exits nonzero if settings access fails.

The maintainer's October 24, 2026 live measurements establish the text JWT,
Bearer access, inclusive activity dates, listed daily response shapes and a
byte-identical original FIT for one compared activity. Automated tests use
synthetic responses only. Other devices, account permissions and quota limits
still require operational observation. Unused Personal endpoints are left for
later work.
