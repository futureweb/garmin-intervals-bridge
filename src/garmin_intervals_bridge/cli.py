"""Command line interface. NO REMOTE WRITES unless --apply is present."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .enrich import enrich_activity, gap_report, match_activity
from .fit import compare_fit, decode_fit, extract_original_fit, sha256
from .garmin import GarminSource
from .intervals import IntervalsClient
from .store import Store, single_instance
from .sync import sync_activities, sync_enrich, sync_wellness, watch_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="garmin-intervals-bridge", description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="Sign into Garmin once and save refreshable tokens (interactive)")
    probe = sub.add_parser("probe", help="Download and archive one untouched ORIGINAL FIT; no Intervals writes")
    probe.add_argument("--activity-id", help="Garmin Connect activity ID (default: latest)")
    cmp = sub.add_parser("compare-fit", help="Compare two local FITs by message/field inventories")
    cmp.add_argument("file_a", type=Path)
    cmp.add_argument("file_b", type=Path)
    gap = sub.add_parser("gap", help="Compare one Garmin original with the copy Intervals received; no writes")
    gap.add_argument("--activity-id", required=True, help="Garmin Connect activity ID")
    gap.add_argument("--intervals-id", help="Intervals activity ID (default: match automatically)")
    enrich = sub.add_parser("enrich", help="Fill the Intervals activity's custom fields/streams from the Garmin original (dry run unless --apply)")
    enrich.add_argument("--activity-id", required=True, help="Garmin Connect activity ID")
    enrich.add_argument("--intervals-id", help="Intervals activity ID (default: match automatically)")
    enrich.add_argument("--apply", action="store_true", help="Actually PUT fields and streams")
    enrich.add_argument("--refresh-own-streams", action="store_true",
                        help="Rewrite streams this bridge wrote before (e.g. after a mapping fix)")
    setup = sub.add_parser("setup-fields", help="Preview/create missing private numeric custom fields")
    setup.add_argument("--apply", action="store_true", help="Actually create missing custom fields")
    sync = sub.add_parser("sync", help="Synchronize (local archive only / dry-run by default)")
    sync.add_argument("--apply", action="store_true", help="Permit Intervals writes")
    sync.add_argument("--scope", choices=["all", "activities", "wellness"], default="all")
    sync.add_argument("--mode", choices=["enrich", "upload"], default="enrich",
                      help="enrich = official import stays on, add what Garmin stripped (default); "
                           "upload = post originals as new activities (only with the official import off)")
    sync.add_argument("--allow-activity-upload", action="store_true",
                      help="Required in addition to --apply; first disable official Garmin activity import")
    sync.add_argument("--force-wellness", action="store_true", help="Ignore cached wellness refresh window")
    sync.add_argument("--activity-days", type=int, help="Override activity lookback (1-30 days)")
    sync.add_argument("--wellness-days", type=int, help="Override wellness lookback (1-30 days)")
    watch = sub.add_parser("watch", help="One cheap poll of Intervals; enrich only what appeared since the last poll (dry run unless --apply)")
    watch.add_argument("--apply", action="store_true")
    backfill = sub.add_parser("backfill", help="Enrich a date range from the past, paced (dry run unless --apply)")
    backfill.add_argument("--scope", choices=["activities", "wellness"], required=True)
    backfill.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    backfill.add_argument("--to", dest="end", help="YYYY-MM-DD (default: today)")
    backfill.add_argument("--apply", action="store_true")
    backfill.add_argument("--pause", type=float, default=2.0, help="Seconds between days/activities (default 2)")
    backfill.add_argument("--force-wellness", action="store_true", help="Re-fetch days already fetched")
    pending = sub.add_parser("reset-pending", help="After manual duplicate check, clear a pending upload lock")
    pending.add_argument("--activity-id", required=True)
    pending.add_argument("--i-checked-intervals", action="store_true", required=True,
                         help="Confirm you checked Intervals for duplicates and verified it is absent")
    sub.add_parser("status", help="Show pending uploads requiring manual reconciliation")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    log = logging.getLogger("bridge")
    try:
        if args.cmd == "compare-fit":
            print(json.dumps(compare_fit(args.file_a, args.file_b), indent=2))
            return 0
        settings = Settings.from_env()
        settings.ensure_data_dir()
        if args.cmd == "login":
            garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
            garmin.login(interactive=True)
            print("Garmin authenticated; session tokens saved locally.")
            return 0
        with single_instance(settings.data_dir):
            store = Store(settings.data_dir)
            try:
                if args.cmd == "status":
                    print(json.dumps({"pending_garmin_activity_ids": store.pending_activities(),
                                      "failed_activities": store.failed_activities()}, indent=2))
                    return 0
                if args.cmd == "reset-pending":
                    if not args.activity_id.isdecimal() or store.activity_status(args.activity_id) != "pending":
                        raise ValueError("Activity must have numeric ID and pending status")
                    store.record_activity(args.activity_id, "downloaded")
                    print(f"Pending lock cleared locally for {args.activity_id}. Next sync will recheck remote.")
                    return 0
                if args.cmd == "setup-fields":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    fields = intervals.provision_fields(apply=args.apply)
                    print(json.dumps({"apply": args.apply, "missing_codes": fields}, indent=2))
                    return 0
                garmin = GarminSource(settings.token_dir, settings.garmin_request_delay)
                garmin.login(interactive=False)
                if args.cmd == "gap":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    gid = args.activity_id
                    path = store.fit_path(gid)
                    if not path.is_file():
                        store.atomic_save(path, garmin.original_fit(gid))
                    original = path.read_bytes()
                    if args.intervals_id:
                        remote_id = args.intervals_id
                        matched = {"id": remote_id, "how": "given"}
                    else:
                        activity = garmin.activity(gid)
                        day = datetime.fromisoformat(activity["startTimeGMT"]).date()
                        candidates = intervals.activities(day - timedelta(days=1), day + timedelta(days=1))
                        found = match_activity(activity, candidates)
                        if found is None:
                            print(json.dumps({"activity_id": gid, "matched": None,
                                              "candidates_seen": len(candidates),
                                              "report": gap_report(original, None)}, indent=2, default=str))
                            return 0
                        remote_id = str(found["id"])
                        matched = {"id": remote_id, "how": "matched", "source": found.get("source"),
                                   "external_id": found.get("external_id"), "start_date": found.get("start_date")}
                    partner = extract_original_fit(intervals.activity_file(remote_id))
                    store.atomic_save(store.partner_path(gid), partner)
                    print(json.dumps({"activity_id": gid, "matched": matched,
                                      "partner_file": str(store.partner_path(gid)),
                                      "report": gap_report(original, partner)}, indent=2, default=str))
                    return 0
                if args.cmd == "enrich":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    plan = enrich_activity(args.activity_id, garmin, intervals, store, apply=args.apply,
                                           intervals_id=args.intervals_id, refresh_own=args.refresh_own_streams)
                    print(json.dumps(plan, indent=2, default=str))
                    return 0
                if args.cmd == "probe":
                    if args.activity_id:
                        gid = args.activity_id
                    else:
                        today = datetime.now(settings.timezone).date()
                        recent = garmin.activities(today - timedelta(days=7), today)
                        if not recent:
                            raise ValueError("No recent Garmin activities; provide --activity-id")
                        gid = str(sorted(recent, key=lambda a: a.get("startTimeGMT", ""))[-1]["activityId"])
                    path = store.fit_path(gid)
                    fit = garmin.original_fit(gid)
                    store.atomic_save(path, fit)
                    print(json.dumps({"activity_id": gid, "file": str(path), "bytes": len(fit), "sha256": sha256(fit)}, indent=2))
                    return 0
                if args.cmd == "watch":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    print(json.dumps({"apply": args.apply, "watch": watch_once(settings, garmin, intervals, store, apply=args.apply)}, indent=2))
                    return 0
                if args.cmd == "backfill":
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    start = date.fromisoformat(args.start)
                    end = date.fromisoformat(args.end) if args.end else datetime.now(settings.timezone).date()
                    if start > end:
                        raise ValueError("--from must not be after --to")
                    if args.scope == "wellness":
                        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
                        log.info("Backfill wellness: %d days, ~22 Garmin requests each, %.1fs pause", len(days), args.pause)
                        out = sync_wellness(settings, garmin, intervals, store, apply=args.apply,
                                            force=args.force_wellness, days=days, pause_seconds=args.pause)
                    else:
                        log.info("Backfill activities %s..%s, one original download per new activity, %.1fs pause", start, end, args.pause)
                        out = sync_enrich(settings, garmin, intervals, store, apply=args.apply,
                                          date_range=(start, end), pause_seconds=args.pause)
                    print(json.dumps({"apply": args.apply, "scope": args.scope, "from": str(start), "to": str(end),
                                      "result": out}, indent=2))
                    return 0
                if args.cmd == "sync":
                    for name in ("activity_days", "wellness_days"):
                        v = getattr(args, name)
                        if v is not None and not 1 <= v <= 30:
                            raise ValueError(f"--{name.replace('_','-')} must be 1..30")
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    results: dict = {"apply": args.apply, "scope": args.scope}
                    results["mode"] = args.mode
                    if args.scope in ("all", "activities") and args.mode == "enrich":
                        results["activities"] = sync_enrich(settings, garmin, intervals, store,
                            apply=args.apply, activity_days=args.activity_days)
                    elif args.scope in ("all", "activities"):
                        if args.apply and not args.allow_activity_upload:
                            log.warning("Activity uploads disabled; add --allow-activity-upload ONLY AFTER disabling Garmin auto-activity sync")
                        results["activities"] = sync_activities(settings, garmin, intervals, store,
                            apply=args.apply, allow_upload=args.allow_activity_upload,
                            activity_days=args.activity_days)
                    if args.scope in ("all", "wellness"):
                        results["wellness"] = sync_wellness(settings, garmin, intervals, store,
                            apply=args.apply, wellness_days=args.wellness_days,
                            force=args.force_wellness)
                    print(json.dumps(results, indent=2))
                    return 0
            finally:
                store.close()
    except Exception as exc:
        # Do not echo possibly credential-bearing exception messages for requests failures.
        if exc.__class__.__module__.startswith("requests"):
            log.error("HTTP/API request failed: %s", type(exc).__name__)
        else:
            log.error("%s: %s", type(exc).__name__, str(exc))
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
