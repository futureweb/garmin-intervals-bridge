"""Fail-closed Garmin -> Intervals sync orchestration."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .fit import sha256, validate_fit
from .enrich import enrich_activity, load_field_mappings
from .garmin import GarminBlocked
from .mapping import map_wellness, merge_wellness
from .store import Store
from .times import external_id_names, parse_utc

log = logging.getLogger(__name__)


_time = parse_utc


def activity_match(garmin: dict, remote: list[dict], tolerance_seconds: int = 180) -> dict | None:
    """Conservative safeguard: a remote activity starting near Garmin's UTC start
    blocks automatic upload even if metadata are missing or sport differs.
    """
    gid = str(garmin.get("activityId", ""))
    gt = _time(garmin.get("startTimeGMT"), garmin_gmt=True)
    if gt is None:
        # No reliable timestamp means we cannot prove absence of duplicates.
        raise ValueError("Garmin activity lacks startTimeGMT; refusing automatic upload")
    for item in remote:
        if not isinstance(item, dict):
            continue
        external = str(item.get("external_id") or "")
        if external_id_names(external, gid):
            return item
        it = _time(item.get("start_date"), garmin_gmt=True)
        if not it and not external:
            # Unknown start data => absence of duplicates cannot be established.
            raise ValueError("Intervals returned an activity without usable UTC start/external ID")
        if it and abs((it - gt).total_seconds()) <= tolerance_seconds:
            return item
    return None


def _garmin_day(garmin: dict) -> date:
    ts = _time(garmin.get("startTimeGMT"), garmin_gmt=True)
    if ts is None:
        raise ValueError("Activity missing startTimeGMT")
    return ts.date()


def _sync_one_activity(activity: dict, gid: str, garmin: Any, intervals: Any, store: Store,
                       remote: list[dict], metrics: dict, *, apply: bool, allow_upload: bool) -> None:
    path = store.fit_path(gid)
    if not path.is_file():
        fit = garmin.original_fit(gid)
        store.atomic_save(path, fit)
        metrics["downloaded"] += 1
    else:
        try:
            validate_fit(path.read_bytes())
        except ValueError:
            fit = garmin.original_fit(gid)
            store.atomic_save(path, fit)
            metrics["downloaded"] += 1
    digest = sha256(path.read_bytes())
    status = store.activity_status(gid)
    if status in ("uploaded", "pending"):
        metrics["pending"] += int(status == "pending")
        log.info("FIT %s already tracked as %s; not reposting", gid, status)
        return
    # Always re-evaluate remote existence; old manual imports need preservation.
    duplicate = activity_match(activity, remote)
    if duplicate is not None:
        store.record_activity(gid, "remote_exists", digest, str(duplicate.get("id") or ""))
        metrics["remote_exists"] += 1
        log.info("FIT %s already present remotely; not replacing", gid)
        return
    store.record_activity(gid, "downloaded", digest)
    if not apply or not allow_upload:
        metrics["would_upload"] += 1
        log.info("DRY/SAFE FIT %s: new original ready; upload NOT performed", gid)
        return
    # Query Intervals again immediately before upload for race protection.
    nearby = intervals.nearby_activities(_garmin_day(activity))
    duplicate = activity_match(activity, nearby)
    if duplicate is not None:
        store.record_activity(gid, "remote_exists", digest, str(duplicate.get("id") or ""))
        metrics["remote_exists"] += 1
        return
    # Pending is durable: after a timeout/crash, NEVER automatically retry
    # a potentially successful upload. Manual reconciliation required.
    store.record_activity(gid, "pending", digest)
    result = intervals.upload_fit(gid, path)
    if not result.get("created"):
        # 200 means *no* new activity. Keep the pending lock; diagnose
        # what Intervals considered a duplicate before trying again.
        log.warning("Intervals did not create Garmin activity %s; manual reconciliation needed", gid)
        metrics["pending"] += 1
        return
    entries = result.get("items", [])
    imported_id = str(entries[0].get("id", "")) if entries and isinstance(entries[0], dict) else ""
    store.record_activity(gid, "uploaded", digest, imported_id)
    metrics["uploaded"] += 1
    log.info("Uploaded original FIT for Garmin ID %s", gid)


def sync_activities(settings: Settings, garmin: Any, intervals: Any, store: Store,
                    *, apply: bool, allow_upload: bool, activity_days: int | None = None,
                    today: date | None = None) -> dict:
    current = today or datetime.now(settings.timezone).date()
    lookback = activity_days or settings.activity_days
    start = current - timedelta(days=lookback - 1)
    activities = garmin.activities(start, current)
    metrics = {"seen": 0, "downloaded": 0, "remote_exists": 0, "uploaded": 0, "pending": 0,
               "would_upload": 0, "skipped": 0, "deferred": 0, "failed": 0}
    # Never rely solely on the hash of partner-filtered and original FIT files.
    remote = intervals.activities(start - timedelta(days=1), current + timedelta(days=1))
    for activity in sorted(activities, key=lambda a: str(a.get("startTimeGMT") or "")):
        gid = str(activity["activityId"])
        if not gid.isdecimal():
            log.warning("Ignoring invalid Garmin activity ID")
            continue
        metrics["seen"] += 1
        if activity.get("manualActivity") is True:
            # Manual entries carry no device recording; Garmin's original export
            # has nothing to return for them and would fail on every run.
            if store.activity_status(gid) != "skipped":
                store.record_activity(gid, "skipped")
            metrics["skipped"] += 1
            continue
        if store.is_deferred(gid):
            metrics["deferred"] += 1
            continue
        # One broken activity must never stop the others. Errors are recorded
        # per activity with backoff; only a Garmin-wide block aborts the run.
        try:
            _sync_one_activity(activity, gid, garmin, intervals, store, remote, metrics,
                               apply=apply, allow_upload=allow_upload)
        except GarminBlocked:
            raise
        except Exception as exc:
            if store.activity_status(gid) == "pending":
                # The upload request itself failed after the pending lock was taken:
                # its outcome is unknown, so it stays pending and is never retried blindly.
                metrics["pending"] += 1
                log.error("Upload of %s ended with %s; outcome unknown, left pending for manual check",
                          gid, type(exc).__name__)
                continue
            attempts = store.record_failure(gid, f"{type(exc).__name__}: {exc}")
            metrics["failed"] += 1
            log.warning("Activity %s failed with %s (attempt %d); retrying later with backoff",
                        gid, type(exc).__name__, attempts)
    return metrics

def sync_enrich(settings: Settings, garmin: Any, intervals: Any, store: Store, *, apply: bool,
                activity_days: int | None = None, today: date | None = None) -> dict:
    """Scheduled enrich mode: for every recent Garmin activity, add to the officially
    synced Intervals activity what the partner copy lacks. Never uploads, never deletes."""
    current = today or datetime.now(settings.timezone).date()
    lookback = activity_days or settings.activity_days
    start = current - timedelta(days=lookback - 1)
    activities = garmin.activities(start, current)
    metrics = {"seen": 0, "skipped": 0, "deferred": 0, "failed": 0, "unmatched": 0,
               "already_enriched": 0, "nothing_to_add": 0, "planned": 0, "enriched": 0,
               "fields_written": 0, "streams_written": 0}
    remote = intervals.activities(start - timedelta(days=1), current + timedelta(days=1))
    mappings = load_field_mappings(intervals.custom_items())
    for activity in sorted(activities, key=lambda a: str(a.get("startTimeGMT") or "")):
        gid = str(activity["activityId"])
        if not gid.isdecimal():
            continue
        metrics["seen"] += 1
        if activity.get("manualActivity") is True:
            metrics["skipped"] += 1
            continue
        if store.is_deferred(gid):
            metrics["deferred"] += 1
            continue
        try:
            result = enrich_activity(gid, garmin, intervals, store, apply=apply,
                                     remote_candidates=remote, mappings=mappings)
        except GarminBlocked:
            raise
        except Exception as exc:
            attempts = store.record_failure(gid, f"{type(exc).__name__}: {exc}")
            metrics["failed"] += 1
            log.warning("Enrich %s failed with %s (attempt %d); retrying later", gid, type(exc).__name__, attempts)
            continue
        outcome = result["outcome"]
        metrics[outcome] += 1
        if outcome == "unmatched":
            log.info("Activity %s has no Intervals counterpart yet; will look again next run", gid)
        elif outcome in ("planned", "enriched"):
            fields = sorted(result["fields"]["writes"])
            streams = [w["type"] for w in result["streams"]["writes"]]
            metrics["fields_written" if outcome == "enriched" else "fields_written"] += len(fields) if outcome == "enriched" else 0
            metrics["streams_written"] += len(streams) if outcome == "enriched" else 0
            log.info("%s %s -> %s: fields %s, streams %s", "ENRICHED" if outcome == "enriched" else "DRY-RUN would enrich",
                     gid, result["intervals_id"], fields, streams)
    return metrics


def sync_wellness(settings: Settings, garmin: Any, intervals: Any, store: Store,
                  *, apply: bool, force: bool = False, wellness_days: int | None = None,
                  today: date | None = None) -> dict:
    current = today or datetime.now(settings.timezone).date()
    lookback = wellness_days or settings.wellness_days
    metrics = {"days_checked": 0, "days_skipped_recent": 0, "days_locked": 0,
               "days_with_changes": 0, "writes": 0, "new_fields": []}
    for offset in reversed(range(lookback)):
        day = current - timedelta(days=offset)
        if not force and store.wellness_recent(day, settings.wellness_refresh_hours):
            metrics["days_skipped_recent"] += 1
            continue
        raw = garmin.snapshot(day)
        store.save_snapshot(day, raw)
        metrics["days_checked"] += 1
        if not raw.get("data"):
            log.warning("No Garmin wellness payload for %s; not marking complete", day)
            continue
        native, custom = map_wellness(raw, day, current)
        if not native and not custom:
            log.info("No supported Garmin scalar wellness data for %s", day)
            if apply:
                store.mark_wellness(day)
            continue
        existing = intervals.wellness(day)
        if existing.get("locked") is True:
            metrics["days_locked"] += 1
            log.info("Skipping locked wellness day %s", day)
            continue
        changes = merge_wellness(existing, native, custom)
        if changes:
            metrics["days_with_changes"] += 1
            log.info("Wellness %s new fields: %s", day, ", ".join(sorted(changes.keys())))
            missing_custom = set(custom).intersection(changes)
            if missing_custom:
                # Create only needed, nonexisting fields. The private custom-field
                # list is queried before writes; no global athlete config reset.
                created = intervals.provision_fields(apply=apply, needed=missing_custom)
                metrics["new_fields"].extend(created)
            if apply:
                intervals.write_wellness(day, changes)
                metrics["writes"] += 1
        if apply:
            store.mark_wellness(day)
    metrics["new_fields"] = sorted(set(metrics["new_fields"]))
    return metrics
