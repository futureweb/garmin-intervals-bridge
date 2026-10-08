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


# ---------------------------------------------------------------------------
# Writing the gap back: field mappings come from the athlete's own custom items
# ---------------------------------------------------------------------------
#
# Intervals fills custom activity fields and custom streams from the FIT file
# itself, and every definition says where its value comes from:
#   ACTIVITY_FIELD  content.fit_session_field = "total_training_effect" | "178" | "140.9"
#   ACTIVITY_STREAM content.fit_record_field  = "step_length"
#   ACTIVITY_STREAM content.script            = "... for (let m of icu.fit.record) { let f = m.f_138 ..."
# plus an optional one-off conversion on first import:
#   content.script = "activity.isNew ? activity.RecoveryTime / 60 : activity.RecoveryTime"
# The bridge reuses those definitions verbatim, so it writes exactly the
# fields the athlete has configured, with the same source and the same units.
# Nothing Garmin-specific is hard-coded here.

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScalarMapping:
    code: str
    source: str                      # "session:<name>" | "session:<num>" | "mesg:<num>.<field>"
    convert: tuple = ()              # sequence of ("*"|"/", factor) applied to the raw value
    select_values: tuple | None = None   # for "select" fields: the only values the field accepts
    note: str = ""


@dataclass(frozen=True)
class StreamMapping:
    code: str
    record_field: str | int          # profile name or numeric field id in `record`


@dataclass
class FieldMappings:
    scalars: list[ScalarMapping] = field(default_factory=list)
    streams: list[StreamMapping] = field(default_factory=list)
    unsupported: dict[str, str] = field(default_factory=dict)   # code -> why


_CONVERSION = re.compile(
    r"^\s*activity\.isNew\s*\?\s*activity\.(?P<code>\w+)(?P<ops>(\s*[*/]\s*[0-9.]+)*)\s*:\s*activity\.(?P=code)\s*$")
_RECORD_SCRIPT = re.compile(r"icu\.fit\.record[\s\S]*?m\.f_(\d+)")


def _parse_conversion(script: str | None, code: str) -> tuple | None:
    """Return the first-import conversion as ("*"/"/", factor) steps, () for none, None if unknown."""
    if not script or not script.strip():
        return ()
    match = _CONVERSION.match(script)
    if match is None or match.group("code") != code:
        return None
    steps = []
    for op, factor in re.findall(r"([*/])\s*([0-9.]+)", match.group("ops")):
        steps.append((op, float(factor)))
    return tuple(steps)


def load_field_mappings(custom_items: list[dict]) -> FieldMappings:
    """Derive what to write, and from where, out of the athlete's custom item definitions."""
    out = FieldMappings()
    for item in custom_items:
        if not isinstance(item, dict) or not isinstance(item.get("content"), dict):
            continue
        content = item["content"]
        code = content.get("code")
        if not isinstance(code, str) or not code:
            continue
        kind = item.get("type")
        if kind == "ACTIVITY_FIELD":
            src = content.get("fit_session_field")
            if not src:
                continue  # computed from other fields; Intervals evaluates that itself
            convert = _parse_conversion(content.get("script"), code)
            if convert is None:
                out.unsupported[code] = "script with unknown semantics"
                continue
            src = str(src).strip()
            if re.fullmatch(r"\d+\.\d+", src):
                source = f"mesg:{src}"
            elif src.isdecimal():
                source = f"session:{src}"
            else:
                source = f"session:{src}"
            select_values = None
            if content.get("type") == "select" and isinstance(content.get("options"), list):
                select_values = tuple(float(o["value"]) for o in content["options"]
                                      if isinstance(o, dict) and isinstance(o.get("value"), (int, float)))
            out.scalars.append(ScalarMapping(code, source, convert, select_values))
        elif kind == "ACTIVITY_STREAM":
            src = content.get("fit_record_field")
            if src:
                src = str(src).strip()
                out.streams.append(StreamMapping(code, int(src) if src.isdecimal() else src))
                continue
            script = content.get("script") or ""
            m = _RECORD_SCRIPT.search(script)
            if m and "_num" not in script:
                out.streams.append(StreamMapping(code, int(m.group(1))))
            elif script.strip():
                out.unsupported[code] = "stream script not limited to a single record field"
    return out


def _lookup(messages: dict[str, list[dict]], source: str) -> Any:
    """Resolve a mapping source against decoded FIT messages; last non-null wins."""
    kind, _, spec = source.partition(":")
    if kind == "mesg":
        mesg, _, fld = spec.partition(".")
        items, key = messages.get(mesg, []), int(fld)
    else:
        items = messages.get("session", [])
        key = int(spec) if spec.isdecimal() else spec
    value = None
    for message in items:
        v = message.get(key)
        if v is not None:
            value = v
    return value


def _convert(value: Any, steps: tuple) -> Any:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    for op, factor in steps:
        result = result * factor if op == "*" else result / factor
    return round(result, 4)


def _same_value(existing: Any, value: Any) -> bool:
    if not isinstance(existing, (int, float)) or isinstance(existing, bool):
        return False
    return abs(float(existing) - float(value)) <= 1e-3 * max(1.0, abs(float(value)))


def plan_scalars(messages: dict[str, list[dict]], activity: dict, mappings: FieldMappings,
                 partner_messages: dict[str, list[dict]] | None = None) -> dict:
    """Fields to PUT on the activity: mapped, present in the original, and either empty in
    Intervals or demonstrably filled from a filtered file.

    The second case matters: when Intervals evaluates a field whose FIT source
    Garmin stripped, it may store 0 rather than null. A value that exists in
    Intervals while its source is absent from the partner copy cannot have come
    from real data, so it is replaced. Without a partner copy, existing values
    are left alone.
    """
    writes: dict[str, Any] = {}
    kept: dict[str, Any] = {}
    replaced: dict[str, dict] = {}
    rejected: dict[str, Any] = {}
    absent: list[str] = []
    for m in mappings.scalars:
        raw = _lookup(messages, m.source)
        if raw is None:
            absent.append(m.code)
            continue
        value = _convert(raw, m.convert)
        if value is None:
            continue
        if m.select_values is not None and value not in m.select_values:
            rejected[m.code] = value             # not one of the field's options
            continue
        existing = activity.get(m.code)
        if _same_value(existing, value):
            kept[m.code] = existing          # already there: a second run must write nothing
            continue
        if existing is not None:
            source_in_partner = partner_messages is not None and _lookup(partner_messages, m.source) is not None
            if partner_messages is None or source_in_partner:
                kept[m.code] = existing          # may be real data: never overwrite
                continue
            replaced[m.code] = {"old": existing, "new": value}
        writes[m.code] = value
    return {"writes": writes, "kept_existing": kept, "replaced_filtered": replaced,
            "rejected_for_select": rejected, "absent_in_original": absent}


def align_stream(records: list[dict], time_stream: list, record_field: str | int) -> tuple[list, dict]:
    """Values of one record field aligned to the Intervals `time` stream (seconds from start).

    Alignment is by timestamp, not by index, so a record Garmin dropped or
    duplicated cannot shift the whole series.
    """
    stamped = [(r.get("timestamp"), r.get(record_field)) for r in records
               if isinstance(r.get("timestamp"), (int, float))]
    if not stamped or not time_stream:
        return [], {"matched": 0, "points": len(time_stream), "non_null": 0}
    t0 = stamped[0][0]
    by_offset = {int(ts - t0): val for ts, val in stamped}
    data = [by_offset.get(int(t)) if isinstance(t, (int, float)) else None for t in time_stream]
    matched = sum(1 for t in time_stream if isinstance(t, (int, float)) and int(t) in by_offset)
    non_null = sum(1 for v in data if v is not None)
    return data, {"matched": matched, "points": len(time_stream), "non_null": non_null}


def plan_streams(messages: dict[str, list[dict]], activity: dict, time_stream: list,
                 mappings: FieldMappings, *, min_alignment: float = 0.95) -> dict:
    """Custom streams to PUT: mapped, carried by the original, not yet on the activity."""
    existing = set(activity.get("stream_types") or [])
    records = messages.get("record", [])
    writes: list[dict] = []
    skipped: dict[str, str] = {}
    for m in mappings.streams:
        if m.code in existing:
            skipped[m.code] = "already on activity"
            continue
        if not any(r.get(m.record_field) is not None for r in records):
            skipped[m.code] = "not in original"
            continue
        data, stats = align_stream(records, time_stream, m.record_field)
        if stats["points"] == 0 or stats["matched"] / stats["points"] < min_alignment:
            skipped[m.code] = f"alignment too poor ({stats['matched']}/{stats['points']} points)"
            continue
        writes.append({"type": m.code, "custom": True, "data": data, "_stats": stats})
    return {"writes": writes, "skipped": skipped}
