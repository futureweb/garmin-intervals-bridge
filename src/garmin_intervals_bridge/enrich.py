"""Enrich an officially synced Intervals activity with what Garmin stripped.

The official Garmin -> Intervals sync keeps running. For each Garmin activity
the bridge fetches the device original, finds the Intervals activity the
official sync created, and compares both files. What only the original has
is the gap. Everything here is read-only; writing the gap back is a
separate, explicitly enabled step.
"""
from __future__ import annotations

from typing import Any

from .fit import decode_fit, sha256
from .times import external_id_names, parse_utc

# Matching tolerances for *writing into* an activity. Tighter than the
# upload blocker in sync.py: a wrong match would put one ride's data on
# another ride.
START_TOLERANCE_SECONDS = 120
DURATION_TOLERANCE = 0.05          # relative
DURATION_TOLERANCE_SECONDS = 60    # absolute floor for short activities


def match_activity(garmin: dict, remote: list[dict]) -> dict | None:
    """Find the Intervals activity created by the official sync for this Garmin activity.

    Accepts a remote activity when its external_id names the Garmin ID, or
    when it comes from Garmin Connect and both start time and duration agree.
    Returns None when nothing matches or when the inputs do not allow a safe
    decision; never guesses.
    """
    gid = str(garmin.get("activityId", ""))
    start = parse_utc(garmin.get("startTimeGMT"), garmin_gmt=True)
    duration = garmin.get("duration")
    if not gid.isdecimal() or start is None:
        return None
    for item in remote:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if external_id_names(item.get("external_id"), gid):
            return item
        if str(item.get("source") or "").upper() != "GARMIN_CONNECT":
            continue
        remote_start = parse_utc(item.get("start_date"))
        if remote_start is None:
            continue
        if abs((remote_start - start).total_seconds()) > START_TOLERANCE_SECONDS:
            continue
        remote_duration = item.get("elapsed_time") or item.get("moving_time")
        if isinstance(duration, (int, float)) and isinstance(remote_duration, (int, float)):
            allowed = max(DURATION_TOLERANCE_SECONDS, DURATION_TOLERANCE * float(duration))
            if abs(float(remote_duration) - float(duration)) > allowed:
                continue
        return item
    return None


def _field_sets(messages: dict[str, list[dict]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, items in messages.items():
        keys: set[str] = set()
        for message in items:
            keys.update(str(k) for k in message if k != "developer_fields")
        out[name] = keys
    return out


def _developer_sets(messages: dict[str, list[dict]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for name, items in messages.items():
        keys: set[str] = set()
        for message in items:
            dev = message.get("developer_fields")
            if isinstance(dev, dict):
                keys.update(str(k) for k in dev)
        if keys:
            out[name] = keys
    return out


def _first_values(items: list[dict], fields: set[str]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for message in items:
        for key, value in message.items():
            label = str(key)
            if label in fields and label not in values and value is not None:
                values[label] = value if not isinstance(value, (list, tuple)) else list(value[:5])
    return values


def _non_null_counts(items: list[dict], fields: set[str]) -> dict[str, int]:
    counts = {f: 0 for f in fields}
    for message in items:
        for key, value in message.items():
            label = str(key)
            if label in counts and value is not None:
                counts[label] += 1
    return counts


def gap_report(original: bytes, partner: bytes | None) -> dict:
    """What does the device original carry that the partner copy lacks?

    With no partner file the report lists the original's inventory and marks
    the comparison as unavailable rather than pretending everything is a gap.
    """
    orig_messages, orig_errors = decode_fit(original)
    orig_fields = _field_sets(orig_messages)
    orig_dev = _developer_sets(orig_messages)
    report: dict[str, Any] = {
        "original": {"bytes": len(original), "sha256": sha256(original),
                     "messages": {k: len(v) for k, v in sorted(orig_messages.items())},
                     "decode_errors": [str(e) for e in orig_errors]},
        "partner": None,
        "comparable": partner is not None,
    }
    if partner is None:
        report["record_fields"] = sorted(orig_fields.get("record", set()))
        report["session_fields"] = sorted(orig_fields.get("session", set()))
        return report

    part_messages, part_errors = decode_fit(partner)
    part_fields = _field_sets(part_messages)
    part_dev = _developer_sets(part_messages)
    report["partner"] = {"bytes": len(partner), "sha256": sha256(partner),
                         "messages": {k: len(v) for k, v in sorted(part_messages.items())},
                         "decode_errors": [str(e) for e in part_errors]}
    report["same_bytes"] = report["original"]["sha256"] == report["partner"]["sha256"]

    # Whole message types that only the original has (workout steps, user
    # profile, sensor settings, Garmin-internal numeric messages ...).
    report["messages_only_in_original"] = {
        name: len(orig_messages[name]) for name in sorted(orig_messages) if name not in part_messages}
    report["message_count_differences"] = {
        name: {"original": len(orig_messages.get(name, [])), "partner": len(part_messages.get(name, []))}
        for name in sorted(set(orig_messages) | set(part_messages))
        if name in part_messages and len(orig_messages.get(name, [])) != len(part_messages[name])}

    # Field-level gaps inside shared message types. Record gaps are stream
    # candidates, session gaps are scalar candidates; both keep sample values
    # so unknown numeric IDs can be named against a known device.
    field_gaps: dict[str, dict[str, Any]] = {}
    for name in sorted(orig_fields):
        if name not in part_fields:
            continue
        missing = orig_fields[name] - part_fields[name]
        if not missing:
            continue
        field_gaps[name] = {
            "fields": sorted(missing, key=lambda f: (f.isdecimal(), int(f) if f.isdecimal() else 0, f)),
            "samples": _first_values(orig_messages[name], missing),
        }
        if name == "record":
            field_gaps[name]["non_null_counts"] = _non_null_counts(orig_messages[name], missing)
    report["field_gaps"] = field_gaps
    report["stream_candidates"] = field_gaps.get("record", {}).get("fields", [])
    report["scalar_candidates"] = {
        f: field_gaps["session"]["samples"].get(f) for f in field_gaps.get("session", {}).get("fields", [])}
    report["developer_fields_only_in_original"] = {
        name: sorted(orig_dev[name] - part_dev.get(name, set()))
        for name in sorted(orig_dev) if orig_dev[name] - part_dev.get(name, set())}
    report["unknown_ids_needing_names"] = {
        name: [f for f in gap["fields"] if f.isdecimal()]
        for name, gap in field_gaps.items() if any(f.isdecimal() for f in gap["fields"])}
    return report
