> **Superseded 2026-10-08.** This file documents the v0.1.1 preview. The audited
> state, the revised architecture (enrich existing activities instead of uploading
> duplicates) and the current phases are in [PLAN.md](PLAN.md).

# Roadmap

## v0.1 developer preview (this archive)
- [x] Original FIT exporter, local archive and optional FIT comparison
- [x] Garmin wellness snapshot collector (source JSON archival)
- [x] Core and extended numeric Intervals wellness mapping
- [x] Custom-field creation (private and idempotent)
- [x] Default dry-run, no-duplicate / pending-upload protections, locked-day handling
- [x] Docker, basic docs, synthetic offline unit tests
- [ ] **Live original-FIT vs Garmin Connect web download comparison**
- [ ] **Live Intervals FIT upload 201/200 response verification**
- [ ] **Live custom-field creation + wellness round-trip**
- [ ] Live one-week test of OAuth refresh, source error patterns and Garmin private API rate limits

## v0.2 after live account validation
- [ ] Verify every Garmin JSON shape and unit; add captured redacted fixtures
- [ ] Better Garmin multiple-device training status selection (do not conflate Garmin acute/chronic metrics with Intervals ATL/CTL)
- [ ] Normalize historic body-composition, lactate-threshold, blood-pressure histories with timestamps
- [ ] Add admin-only reconciliation workflow for pending FIT uploads
- [ ] Add time-window backfill without excessive private API requests
- [ ] Document Intervals 200 duplicate response and metadata restoration behavior
- [ ] API error taxonomy, controlled retry for GET, metrics for cron alerts
- [ ] Consider structured local time-series export (optional InfluxDB/Grafana), keep Intervals scalars separate
- [ ] Optional OAuth-based Intervals auth for shared multi-user/public releases
- [ ] Add offline contract tests from sanitized live data, never from raw private exports
- [ ] Rename fields in UI as needed without changing stable `content.code`

## Not planned without API support
- Auto-replacing old Garmin-filtered Intervals activities or deleting any activity.
- Synthesizing full FIT files from Grafana time-series. Garmin device FITs are kept untouched.
- Mapping arbitrarily structured Garmin JSON or intraday streams to Intervals single daily scalar slots.
