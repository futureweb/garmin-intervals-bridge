"""Swap mode (BRIDGE_SWAP=true, off by default): Garmin's filtered copy for the original.

The official import stays on and is the trigger: with the next poll after a Garmin activity
arriving in Intervals, the bridge puts the device's original file in its place (Garmin Connect
is only ever read, never changed). Intervals then holds the complete file – it computes every
custom field and stream itself, and "Reprocess file" keeps them. Nothing to poll at Garmin: one
original download per new activity, as in enrich mode.

Two ways (BRIDGE_SWAP_METHOD):
  - replace (default): Intervals' POST /activity/{id}/file puts the original into the same
    activity – its id, name, notes, RPE, gear, tags, comments, the paired workout, its
    intervals (edited ones too) and the values typed into custom fields stay; nothing is
    deleted. It stays Garmin's import (source GARMIN_CONNECT) with the complete file.
  - upload: the original goes up as a new activity of your own (source UPLOAD), and the
    filtered copy is deleted. The one place the bridge deletes anything, so it is careful:
  - only a copy of the official Garmin import (source GARMIN_CONNECT, its Garmin id),
    within BRIDGE_SWAP_HOURS (default 48) of its import – `swap <id>` by hand has no limit
  - never with comments on it (asked again right before every delete, retries included),
    never when its intervals were edited – by hand, or (automatic swaps) after the bridge
    wrote streams into it, which Intervals counts as an edit too
  - not when the original is the very file Intervals has (Garmin filtered nothing)
  - upload first, delete last: a failure leaves a duplicate to see, never a loss; an
    upload with an unknown outcome stays `pending` (health reports it), is never sent again
    and deletes nothing; an original uploaded before is taken up again, not uploaded twice
  - name, description, the paired workout, type, RPE, feel, tags, gear, commute/race
    flags and ignore settings go over to the new activity, and so do the values of custom
    activity fields without a script (nothing in the file computes them: the logged intake
    the bridge writes from Garmin's summary, what you typed in yourself) – and the copy is
    deleted only once they read back from the new activity
  - a delete tried again later only while Intervals still lists both activities
  - one swap at a time (a lock shared by the watch, the full runs and `swap` by hand)
  - the original stays in the local archive either way

Your own uploads (source UPLOAD – a Strava archive, files you uploaded by hand) take the original
too, in place only (replace): `swap <id>` and `backfill --swap`, never the automatic swap. No
Garmin id names them, so they are paired by start and duration, and only one to one: one upload,
one Garmin activity, no import of Garmin's own beside them. And the file Intervals holds must be
the same recording – its FIT names the same device and creation time. A file that cannot tell
(a GPX, say) only by hand, after the dry run. Other platforms' imports (Strava's) Intervals does
not let anyone change.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .enrich import agrees, garmin_id_from
from .fit import InvalidFIT, NotFIT, decode_fit, extract_original_fit, sha256
from .intervals import refused
from .store import single_instance
from .times import parse_utc

log = logging.getLogger(__name__)

# What the athlete may have set on the copy already; carried when it differs on the new one.
CARRY = ("name", "description", "type", "sub_type", "commute", "race", "trainer", "icu_rpe", "feel", "tags",
         "gear", "icu_ignore_hr", "icu_ignore_power", "icu_ignore_time", "ignore_velocity", "ignore_pace")
DELETE_KEY = "swap:delete:"            # + the copy's Intervals id; value: the new id
PAIR_KEY = "swap:pair:"                # + the new id; value: the workout to pair it with again


def _hours_since(value: Any, now: datetime) -> float | None:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 3600


def typed_fields(intervals: Any) -> set[str]:
    """Custom activity fields Intervals cannot compute from the file: no script, no FIT field (on
    Garmin's copy those hold what the filtered file gave – the original's own value must win)."""
    out = set()
    for item in intervals.custom_items():
        content = item.get("content") or {}
        if (item.get("type") == "ACTIVITY_FIELD" and content.get("code")
                and not str(content.get("script") or "").strip() and not content.get("fit_session_field")):
            out.add(str(content["code"]))
    return out


def _value(value: Any) -> bool:
    """Set on the copy: a 0 is a value (a typed field), False is not (the new file decides)."""
    return value is not None and value is not False and value != "" and value != [] and str(value) != "NaN"


def _same(have: Any, want: Any) -> bool:
    if isinstance(want, dict):                          # gear: the same item
        return isinstance(have, dict) and have.get("id") == want.get("id")
    if isinstance(want, list) and isinstance(have, list):
        return sorted(map(str, have)) == sorted(map(str, want))
    return have == want


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _listed(intervals: Any, around: Any) -> set[str] | None:
    """The ids Intervals lists around that day; None when it cannot be told. A deleted activity
    still answers by its id – only the list shows it is gone."""
    day = _day(around)
    if day is None:
        return None
    try:
        return {str(a.get("id")) for a in intervals.activities(day - timedelta(days=1), day + timedelta(days=1),
                                                               fields=["id"])}
    except Exception:
        return None


def why_not(copy: dict, gid: str, intervals: Any, store: Any, *, window_hours: float | None,
            now: datetime | None = None) -> str | None:
    """The reason to keep this copy; None when it may be swapped."""
    if str(copy.get("source") or "").upper() != "GARMIN_CONNECT":
        return f"not Garmin's import (source {copy.get('source') or 'unknown'})"
    if garmin_id_from(copy.get("external_id")) != gid:
        return "its Garmin id does not match"
    if window_hours is not None:
        age = _hours_since(copy.get("created") or copy.get("start_date"), now or datetime.now(timezone.utc))
        if age is None or age > window_hours:
            return f"imported more than {window_hours:g} h ago (swap it by hand: `swap <id> --apply`)"
    previous = store.enrichment(gid) or {}
    if copy.get("icu_intervals_edited") and not previous.get("streams"):
        return "its intervals were edited by hand"
    if copy.get("icu_intervals_edited") and window_hours is not None:
        # the bridge's stream writes count as an edit too – a later one by hand would look the same
        return ("its intervals were edited (by the bridge's streams or by hand): swap it by hand if they are "
                "the bridge's")
    if intervals.activity_messages(str(copy["id"])):
        return "it has comments"
    return None


def replace_activity(gid: str, iid: str, garmin: Any, intervals: Any, store: Any, *, apply: bool) -> dict:
    """The original into Garmin's copy itself (POST /activity/{id}/file). Outcomes: kept (with
    the reason), same (nothing filtered, or Intervals found the file unchanged), planned, replaced."""
    path, partner = store.fit_path(gid), store.partner_path(gid)
    previous = store.enrichment(gid)
    if previous and previous["intervals_id"] != iid and partner.is_file():
        partner.unlink()                       # another Intervals activity's file (re-imported): stale
    if path.is_file() and partner.is_file() and sha256(path.read_bytes()) == sha256(partner.read_bytes()):
        # the copy stays Garmin's import after a replace: every later scan ends here, without a request
        return {"outcome": "same", "intervals_id": iid, "reason": "Intervals has the original already"}
    copy = intervals.activity(iid)
    if str(copy.get("source") or "").upper() != "GARMIN_CONNECT":
        return {"outcome": "kept", "intervals_id": iid, "reason": f"not Garmin's import (source "
                f"{copy.get('source') or 'unknown'})"}
    if garmin_id_from(copy.get("external_id")) != gid:
        return {"outcome": "kept", "intervals_id": iid, "reason": "its Garmin id does not match"}
    if not path.is_file():
        store.atomic_save(path, garmin.original_fit(gid))
    if not partner.is_file():
        store.atomic_save(partner, extract_original_fit(intervals.activity_file(iid)))
    original = path.read_bytes()
    if sha256(original) == sha256(partner.read_bytes()):
        return {"outcome": "same", "intervals_id": iid, "reason": "Intervals has the original already"}
    plan = {"outcome": "planned", "intervals_id": iid, "garmin_id": gid, "method": "replace"}
    if not apply:
        return plan
    with single_instance(store.base, ("swap",)):
        # typed values stay; intervals only when someone edited them – keeping unedited ones would mark
        # them edited (Intervals' flag), and the complete file finds them anew
        done = intervals.replace_file(iid, path, keep_intervals=bool(copy.get("icu_intervals_edited")))
        store.atomic_save(partner, original)                   # what Intervals holds now: nothing left to add
        store.drop_enrichment(gid)                             # what the bridge wrote is the file's own now
    if done is None:
        return {**plan, "outcome": "same", "reason": "Intervals found the file unchanged"}
    log.info("Replaced %s: Garmin's copy %s now holds the original (same activity)", gid, iid)
    return {**plan, "outcome": "replaced"}


def is_own_upload(item: dict) -> bool:
    return str(item.get("source") or "").upper() == "UPLOAD"


def _names(item: dict) -> str | None:
    """The Garmin id an upload's external id names for sure (`garmin:<id>`, Garmin's export
    `<id>_ACTIVITY.fit`); a bare number may be any platform's id."""
    external = str(item.get("external_id") or "").strip()
    return None if external.isdecimal() else garmin_id_from(external)


def own_upload_pair(activity: dict, remote: list[dict], listed: list[dict]) -> tuple[dict | None, str | None]:
    """Your own upload of this Garmin recording among `remote` (Intervals around its day), checked
    against `listed` (Garmin around its day): (the upload, None), or (None, why not – None when
    there is no upload like it at all). Never guesses."""
    gid = str(activity.get("activityId"))
    mine = [item for item in remote if is_own_upload(item) and item.get("id") and _names(item) in (None, gid)]
    uploads = [item for item in mine if _names(item) == gid] or [item for item in mine if agrees(activity, item)]
    if not uploads:
        return None, None
    if any(str(item.get("source") or "").upper() == "GARMIN_CONNECT"
           and (garmin_id_from(item.get("external_id")) == gid or agrees(activity, item)) for item in remote):
        return None, "Garmin's own import of it is in Intervals too: swap that one"
    if len(uploads) > 1:
        return None, f"{len(uploads)} of your uploads start and last like it"
    others = [a for a in listed if str(a.get("activityId")) != gid and a.get("manualActivity") is not True]
    if _names(uploads[0]) is None and any(agrees(other, uploads[0]) for other in others):
        return None, "another Garmin activity starts and lasts like it too (two recordings of one session?)"
    return uploads[0], None


FILE_ID = ("manufacturer", "product", "serial_number", "time_created")


def same_recording(original: bytes, held: bytes) -> bool | None:
    """Do both FITs come from one recording – the same device and creation time in their file id?
    None when they cannot tell (no creation time in one of them)."""
    ids = []
    for data in (original, held):
        found = (decode_fit(data, checked=True)[0].get("file_id") or [{}])[0]
        ids.append({k: found.get(k) for k in FILE_ID if found.get(k) is not None})
    if not all("time_created" in i for i in ids):
        return None
    return all(ids[0][k] == ids[1][k] for k in FILE_ID if k in ids[0] and k in ids[1])


def replace_own_upload(gid: str, iid: str, garmin: Any, intervals: Any, store: Any, *, apply: bool,
                       by_hand: bool = False) -> dict:
    """The original into an upload of your own (POST /activity/{id}/file): the same activity,
    nothing deleted. Only into the same recording; a file that cannot tell (no FIT) only `by_hand`.
    Outcomes: kept (with the reason), same, planned, replaced. Leaves the bridge's records of
    Garmin's copies alone (the partner file, the enrichment)."""
    copy = intervals.activity(iid)
    if not is_own_upload(copy):
        return {"outcome": "kept", "intervals_id": iid,
                "reason": f"not your own upload (source {copy.get('source') or 'unknown'})"}
    path = store.fit_path(gid)
    if not path.is_file():
        store.atomic_save(path, garmin.original_fit(gid))
    original = path.read_bytes()
    try:
        held = extract_original_fit(intervals.activity_file(iid))
    except (InvalidFIT, NotFIT):
        held = None                                    # a GPX or TCX, or a broken FIT
    if held is not None and sha256(held) == sha256(original):
        return {"outcome": "same", "intervals_id": iid, "reason": "Intervals has the original already"}
    same = same_recording(original, held) if held is not None else None
    if same is False:
        return {"outcome": "kept", "intervals_id": iid,
                "reason": "its file is another recording (another device or creation time in the FIT)"}
    if same is None and not by_hand:
        return {"outcome": "kept", "intervals_id": iid, "reason": "its file cannot tell whether it is this "
                f"recording (no FIT): check `swap {iid}` and swap it by hand"}
    plan = {"outcome": "planned", "intervals_id": iid, "garmin_id": gid, "method": "replace", "own_upload": True,
            "checked": "the same recording (the FIT's device and creation time)" if same else
                       "start and duration only: make sure it is this recording"}
    if not apply:
        return plan
    with single_instance(store.base, ("swap",)):
        done = intervals.replace_file(iid, path, keep_intervals=bool(copy.get("icu_intervals_edited")))
    if done is None:
        return {**plan, "outcome": "same", "reason": "Intervals found the file unchanged"}
    log.info("Replaced %s: your upload %s now holds the original (same activity)", gid, iid)
    return {**plan, "outcome": "replaced"}


def swap_own_upload_by_hand(iid: str, item: dict, garmin: Any, intervals: Any, store: Any, *, apply: bool) -> dict:
    """`swap <id>` on an upload of your own: its Garmin recording found by start and duration
    (or the Garmin id it names), then the original into it."""
    gid = _names(item)
    start = parse_utc(item.get("start_date"))
    if start is None:
        return {"outcome": "kept", "intervals_id": iid, "reason": "it has no start time"}
    day = start.date()
    listed = [a for a in garmin.activities(day - timedelta(days=1), day + timedelta(days=1))
              if a.get("manualActivity") is not True]
    if gid is None:
        alike = [a for a in listed if agrees(a, item)]
        if len(alike) != 1:
            return {"outcome": "kept", "intervals_id": iid, "reason": "no Garmin activity starts and lasts like it"
                    if not alike else f"{len(alike)} Garmin activities start and last like it (two recordings?)"}
        activity = alike[0]
    else:
        activity = next((a for a in listed if str(a.get("activityId")) == gid), {"activityId": gid})
    remote = intervals.activities(day - timedelta(days=1), day + timedelta(days=1),
                                  fields=["id", "external_id", "source", "start_date", "moving_time", "elapsed_time"])
    upload, reason = own_upload_pair(activity, remote, listed)
    if upload is None or str(upload.get("id")) != iid:
        return {"outcome": "kept", "intervals_id": iid, "garmin_id": str(activity["activityId"]),
                "reason": reason or "Intervals pairs another upload with that Garmin activity"}
    return replace_own_upload(str(activity["activityId"]), iid, garmin, intervals, store, apply=apply, by_hand=True)


def swap_activity(gid: str, iid: str, garmin: Any, intervals: Any, store: Any, *, apply: bool,
                  window_hours: float | None = 48, now: datetime | None = None) -> dict:
    """Outcomes: kept (with the reason), same (nothing filtered), planned, swapped,
    review (the upload made several activities: nothing deleted), delete_failed (both stay;
    tried again on the next run)."""
    copy = intervals.activity(iid)
    status, earlier = store.activity_row(gid)
    resume = status == "uploaded" and bool(earlier) and earlier != iid      # the original went in before
    reason = why_not(copy, gid, intervals, store, window_hours=None if resume else window_hours, now=now)
    if reason:
        return {"outcome": "kept", "intervals_id": iid, "reason": reason}
    path = store.fit_path(gid)
    if not path.is_file():
        store.atomic_save(path, garmin.original_fit(gid))
    partner = store.partner_path(gid)
    if not partner.is_file():
        store.atomic_save(partner, extract_original_fit(intervals.activity_file(iid)))
    digest = sha256(path.read_bytes())
    if digest == sha256(partner.read_bytes()):
        return {"outcome": "same", "intervals_id": iid, "reason": "Garmin sent the original itself"}
    carry = {k: copy[k] for k in (*CARRY, *sorted(typed_fields(intervals))) if _value(copy.get(k))}
    plan = {"outcome": "planned", "intervals_id": iid, "garmin_id": gid, "carry": sorted(carry),
            "paired_event_id": copy.get("paired_event_id")}
    if copy.get("icu_intervals_edited"):
        plan["warning"] = "its intervals were edited; the new activity gets the ones Intervals finds in the file"
    if not apply:
        return plan
    with single_instance(store.base, ("swap",)):
        return _swap(gid, iid, copy, carry, digest, plan, resume=resume, earlier=earlier, path=path,
                     intervals=intervals, store=store)


def _swap(gid: str, iid: str, copy: dict, carry: dict, digest: str, plan: dict, *, resume: bool,
          earlier: str | None, path: Any, intervals: Any, store: Any) -> dict:
    """Upload (or take up the original uploaded before), carry the values over, check them, delete."""
    status, _ = store.activity_row(gid)
    if status == "pending":
        return {**plan, "outcome": "kept", "reason": "an earlier upload of the original has an unknown outcome: "
                f"look for it in Intervals, then `reset-pending --activity-id {gid}`"}
    if resume:
        listed = _listed(intervals, copy.get("start_date_local") or copy.get("start_date"))
        if listed is None or earlier not in listed:
            return {**plan, "outcome": "kept", "reason": f"the original uploaded before ({earlier}) is not in "
                    "Intervals any more" if listed is not None else "Intervals' list could not be read"}
        new_id = str(earlier)
    else:
        store.record_activity(gid, "pending", digest)      # an unknown outcome is never replayed
        try:
            result = intervals.upload_fit(gid, path, name=copy.get("name"), description=copy.get("description"),
                                          paired_event_id=copy.get("paired_event_id"))
        except Exception as exc:
            if refused(exc):
                store.record_activity(gid, "downloaded", digest)    # refused: nothing went in
            raise
        if not result.get("created"):
            store.record_activity(gid, "remote_exists", digest, iid)
            return {**plan, "outcome": "kept", "reason": "Intervals already has this file"}
        items = result.get("items") or []
        new_id = str(items[0]["id"])
        store.record_activity(gid, "uploaded", digest, new_id)
        store.mark_intervals_seen(new_id, f"garmin:{gid}", "UPLOAD")
        if len(items) != 1:
            log.warning("Swap %s: the original became %d activities; %s kept, check by hand", gid, len(items), iid)
            return {**plan, "outcome": "review", "new_ids": [str(i["id"]) for i in items]}
    fresh = intervals.activity(new_id)
    changes = {k: v for k, v in carry.items() if not _same(fresh.get(k), v)}
    if changes:
        payload = {k: ({"id": v.get("id")} if isinstance(v, dict) else v) for k, v in changes.items()}
        try:
            intervals.update_activity(new_id, payload)
            fresh = intervals.activity(new_id)
            missing = sorted(k for k in changes if not _same(fresh.get(k), carry[k]))
        except Exception as exc:
            missing = sorted(changes)
            log.warning("Swap %s: carrying values to %s failed (%s)", gid, new_id, type(exc).__name__)
        if missing:
            # the copy holds what the athlete set: it stays until they are on the original
            log.warning("Swap %s: %s not on the original %s yet; Garmin's copy %s stays (tried again by the "
                        "next full run)", gid, missing, new_id, iid)
            return {**plan, "outcome": "review", "new_id": new_id,
                    "reason": f"{', '.join(missing)} not carried over; both stay"}
    plan.update(outcome="swapped", new_id=new_id, carried=sorted(changes))
    if intervals.activity_messages(iid):
        log.warning("Swap %s: Garmin's copy %s got comments meanwhile; both stay", gid, iid)
        return {**plan, "outcome": "review", "reason": "Garmin's copy got comments meanwhile; both stay"}
    if not _delete(intervals, store, iid, new_id):
        plan["outcome"] = "delete_failed"
        return plan
    store.drop_enrichment(gid)                         # what the bridge wrote went with the copy
    if copy.get("paired_event_id") and not intervals.activity(new_id).get("paired_event_id"):
        _pair(intervals, store, new_id, copy["paired_event_id"])
    log.info("Swapped %s: Garmin's copy %s -> the original %s (carried %s)", gid, iid, new_id, sorted(changes))
    return plan


def _pair(intervals: Any, store: Any, new_id: str, event_id: Any) -> bool:
    try:
        intervals.update_activity(new_id, {"paired_event_id": event_id})
    except Exception as exc:
        log.warning("Swap: the workout %s not paired with %s yet (%s); tried again next run", event_id, new_id,
                    type(exc).__name__)
        store.set_meta(PAIR_KEY + new_id, str(event_id))
        return False
    if store.get_meta(PAIR_KEY + new_id):
        store.set_meta(PAIR_KEY + new_id, "")
    return True


def _delete(intervals: Any, store: Any, iid: str, new_id: str) -> bool:
    error = None
    try:
        intervals.delete_activity(iid)
    except Exception as exc:
        if getattr(getattr(exc, "response", None), "status_code", None) != 404:   # 404: gone already
            error = type(exc).__name__
    if error is not None:
        log.warning("Swap: Garmin's copy %s not deleted (%s); both stay, tried again next run", iid, error)
        store.set_meta(DELETE_KEY + iid, new_id)
        return False
    if store.get_meta(DELETE_KEY + iid):
        store.set_meta(DELETE_KEY + iid, "")
    return True


def retry_deletes(intervals: Any, store: Any) -> int:
    """Copies whose delete failed: again – only while Intervals still lists both the copy and its
    original, and the copy has no comments. Workouts not paired again: again."""
    done = 0
    for key, new_id in store.meta_items(DELETE_KEY + "*"):
        iid = key[len(DELETE_KEY):]
        if not new_id:
            continue
        try:
            copy = intervals.activity(iid)
            listed = _listed(intervals, copy.get("start_date_local") or copy.get("start_date"))
            comments = intervals.activity_messages(iid) if listed and iid in listed else []
        except Exception as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                store.set_meta(key, "")                    # the copy is gone already
            continue                                       # otherwise: next run
        if listed is None:
            continue
        if iid not in listed:
            store.set_meta(key, "")                        # deleted meanwhile
        elif new_id not in listed or comments:
            log.warning("Swap: Garmin's copy %s stays – %s", iid, "it has comments now" if comments else
                        f"its original {new_id} is not in Intervals any more")
            store.set_meta(key, "")
        else:
            done += _delete(intervals, store, iid, new_id)
    for key, event_id in store.meta_items(PAIR_KEY + "*"):
        if event_id:
            _pair(intervals, store, key[len(PAIR_KEY):], int(event_id) if event_id.isdecimal() else event_id)
    return done
