"""Keep FIT files as bytes; never reconstruct device streams from JSON.

Decoding uses Garmin's official FIT SDK. Two properties of that decoder matter
for this project and are relied upon here: messages whose number is not in the
public profile are kept under their numeric key, and fields a profile message
does not know are kept under their numeric field ID. The data Garmin strips
from partner downloads (stamina, recovery time, sweat loss, training status)
lives exactly in those unknown messages and fields.
"""
from __future__ import annotations

import hashlib
import io
import struct
import zipfile
from pathlib import Path
from typing import Any

from garmin_fit_sdk import Decoder, Stream


class InvalidFIT(ValueError):
    pass


# Size guard for anything we are asked to decode. Device activity files are a
# few hundred kB; the guard only blocks absurd inputs such as ZIP bombs.
MAX_FIT_BYTES = 32 * 1024 * 1024


def validate_fit(data: bytes) -> None:
    """Structural check followed by the SDK's CRC check of header and data."""
    if len(data) < 14:
        raise InvalidFIT("File is shorter than FIT header/trailer")
    size = data[0]
    if size not in (12, 14):
        raise InvalidFIT(f"Unexpected FIT header size {size}")
    if data[8:12] != b".FIT":
        raise InvalidFIT("FIT magic header missing")
    declared = struct.unpack_from("<I", data, 4)[0]
    expected = size + declared + 2  # FIT CRC after data
    if len(data) != expected:
        raise InvalidFIT(f"FIT length mismatch: expected {expected}, got {len(data)}")
    decoder = Decoder(Stream.from_byte_array(bytearray(data)))
    if not decoder.is_fit():
        raise InvalidFIT("Not a FIT file according to the SDK")
    if not decoder.check_integrity():
        raise InvalidFIT("FIT CRC check failed")


def extract_original_fit(download: bytes) -> bytes:
    """The ORIGINAL Garmin Connect export is normally a ZIP containing a FIT."""
    if zipfile.is_zipfile(io.BytesIO(download)):
        with zipfile.ZipFile(io.BytesIO(download)) as z:
            entries = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".fit")]
            if len(entries) != 1:
                raise InvalidFIT(f"Expected exactly one FIT in archive, found {len(entries)}")
            # No extraction to disk: prevents zip-slip, oversized ZIP bombs, etc.
            entry = entries[0]
            if entry.file_size > MAX_FIT_BYTES:
                raise InvalidFIT("Unexpectedly large FIT (>200MB)")
            with z.open(entry) as f:
                data = f.read(MAX_FIT_BYTES + 1)
    else:
        data = download
    validate_fit(data)
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def decode_fit(data: bytes) -> tuple[dict[str, list[dict]], list]:
    """Decode to {message_name: [messages]} plus the SDK's non-fatal error list.

    Timestamps stay numeric (FIT epoch seconds) so results are JSON-safe.
    Heart-rate merging is off: the record stream must stay what the device wrote.
    """
    validate_fit(data)
    decoder = Decoder(Stream.from_byte_array(bytearray(data)))
    messages, errors = decoder.read(convert_datetimes_to_dates=False, merge_heart_rates=False)
    normalised: dict[str, list[dict]] = {}
    for key, items in messages.items():
        # Profile messages arrive as "<name>_mesgs"; unknown ones as their number.
        name = key[:-6] if key.endswith("_mesgs") else key
        normalised[name] = items
    return normalised, errors


def _sample(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return list(value[:5])
    if isinstance(value, bytes):
        return value[:16].hex()
    return value


def fit_inventory(path: Path) -> dict:
    """Message/field inventory, separating profile-known from Garmin-internal data.

    `unknown_fields` and `unknown_messages` carry a sample value each, which is
    what you need to put a name to a numeric ID when comparing a device
    original with the file a partner received.
    """
    content = path.read_bytes()
    messages, errors = decode_fit(content)
    counts: dict[str, int] = {}
    fields: dict[str, list[str]] = {}
    unknown_messages: dict[str, int] = {}
    unknown_fields: dict[str, dict[str, Any]] = {}
    developer_fields: dict[str, list[str]] = {}
    for name, items in sorted(messages.items()):
        counts[name] = len(items)
        if name.isdecimal():
            unknown_messages[name] = len(items)
        seen: set[str] = set()
        dev: set[str] = set()
        samples: dict[str, Any] = {}
        for message in items:
            for key, value in message.items():
                if key == "developer_fields" and isinstance(value, dict):
                    dev.update(str(k) for k in value)
                    continue
                label = str(key)
                seen.add(label)
                if isinstance(key, int) and value is not None and label not in samples:
                    samples[label] = _sample(value)
        fields[name] = sorted(seen)
        if samples:
            unknown_fields[name] = dict(sorted(samples.items(), key=lambda kv: int(kv[0])))
        if dev:
            developer_fields[name] = sorted(dev)
    return {
        "bytes": len(content),
        "sha256": sha256(content),
        "messages": counts,
        "fields": fields,
        "unknown_messages": unknown_messages,
        "unknown_fields": unknown_fields,
        "developer_fields": developer_fields,
        "decode_errors": [str(e) for e in errors],
    }


def compare_fit(first: Path, second: Path) -> dict:
    """What does file A carry that file B lacks, and vice versa?

    Used to prove that a Garmin Connect original holds data the partner API
    copy does not. Unknown numeric IDs are compared like named fields.
    """
    a, b = fit_inventory(first), fit_inventory(second)
    kinds = sorted(set(a["messages"]) | set(b["messages"]))

    def only(x: dict, y: dict) -> dict[str, list[str]]:
        return {k: sorted(set(x["fields"].get(k, [])) - set(y["fields"].get(k, [])))
                for k in kinds if set(x["fields"].get(k, [])) - set(y["fields"].get(k, []))}

    return {
        "file_a": {"name": str(first), "bytes": a["bytes"], "sha256": a["sha256"]},
        "file_b": {"name": str(second), "bytes": b["bytes"], "sha256": b["sha256"]},
        "same_bytes": a["sha256"] == b["sha256"],
        "message_count_differences": {
            k: {"a": a["messages"].get(k, 0), "b": b["messages"].get(k, 0)}
            for k in kinds if a["messages"].get(k) != b["messages"].get(k)
        },
        "only_in_a_fields": only(a, b),
        "only_in_b_fields": only(b, a),
        "only_in_a_developer_fields": {
            k: sorted(set(a["developer_fields"].get(k, [])) - set(b["developer_fields"].get(k, [])))
            for k in kinds if set(a["developer_fields"].get(k, [])) - set(b["developer_fields"].get(k, []))
        },
        "unknown_field_samples_a": a["unknown_fields"],
        "unknown_field_samples_b": b["unknown_fields"],
    }
