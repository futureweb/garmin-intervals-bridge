"""Command line interface. NO REMOTE WRITES unless --apply is present."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import Settings
from .fit import compare_fit, sha256
from .garmin import GarminSource
from .intervals import IntervalsClient
from .store import Store, single_instance
from .sync import sync_activities, sync_wellness


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
    setup = sub.add_parser("setup-fields", help="Preview/create missing private numeric custom fields")
    setup.add_argument("--apply", action="store_true", help="Actually create missing custom fields")
    sync = sub.add_parser("sync", help="Synchronize (local archive only / dry-run by default)")
    sync.add_argument("--apply", action="store_true", help="Permit Intervals writes")
    sync.add_argument("--scope", choices=["all", "activities", "wellness"], default="all")
    sync.add_argument("--allow-activity-upload", action="store_true",
                      help="Required in addition to --apply; first disable official Garmin activity import")
    sync.add_argument("--force-wellness", action="store_true", help="Ignore cached wellness refresh window")
    sync.add_argument("--activity-days", type=int, help="Override activity lookback (1-30 days)")
    sync.add_argument("--wellness-days", type=int, help="Override wellness lookback (1-30 days)")
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
                    pending = store.pending_activities()
                    print(json.dumps({"pending_garmin_activity_ids": pending}, indent=2))
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
                if args.cmd == "sync":
                    for name in ("activity_days", "wellness_days"):
                        v = getattr(args, name)
                        if v is not None and not 1 <= v <= 30:
                            raise ValueError(f"--{name.replace('_','-')} must be 1..30")
                    intervals = IntervalsClient(settings.intervals_api_key, settings.intervals_athlete_id)
                    results: dict = {"apply": args.apply, "scope": args.scope}
                    if args.scope in ("all", "activities"):
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
