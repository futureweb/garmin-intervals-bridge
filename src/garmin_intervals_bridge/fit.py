"""Keep FIT files as bytes; never reconstruct device streams from JSON."""
from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from collections import Counter
from pathlib import Path


class InvalidFIT(ValueError):
    pass


def validate_fit(data: bytes) -> None:
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


def extract_original_fit(download: bytes) -> bytes:
    """The ORIGINAL Garmin Connect export is normally a ZIP containing a FIT."""
    if zipfile.is_zipfile(io.BytesIO(download)):
        with zipfile.ZipFile(io.BytesIO(download)) as z:
            entries = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith(".fit")]
            if len(entries) != 1:
                raise InvalidFIT(f"Expected exactly one FIT in archive, found {len(entries)}")
            # No extraction to disk: prevents zip-slip, oversized ZIP bombs, etc.
            entry = entries[0]
            if entry.file_size > 200 * 1024 * 1024:
                raise InvalidFIT("Unexpectedly large FIT (>200MB)")
            with z.open(entry) as f:
                data = f.read(200 * 1024 * 1024 + 1)
    else:
        data = download
    validate_fit(data)
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fit_inventory(path: Path) -> dict:
    """FIT message/field inventory using the bundled fitparse dependency."""
    try:
        from fitparse import FitFile
    except ImportError as exc:
        raise RuntimeError('Dependency fitparse is missing; reinstall the bridge with pip') from exc
    content = path.read_bytes()
    validate_fit(content)
    counts: Counter[str] = Counter()
    fields: dict[str, set[str]] = {}
    for msg in FitFile(io.BytesIO(content)).get_messages():
        kind = msg.name
        counts[kind] += 1
        fields.setdefault(kind, set()).update(str(f.name) for f in msg)
    return {"bytes": len(content), "sha256": sha256(content),
            "messages": dict(counts), "fields": {k: sorted(v) for k, v in sorted(fields.items())}}


def compare_fit(first: Path, second: Path) -> dict:
    a, b = fit_inventory(first), fit_inventory(second)
    kinds = sorted(set(a["messages"]) | set(b["messages"]))
    return {
        "file_a": {"name": str(first), "bytes": a["bytes"], "sha256": a["sha256"]},
        "file_b": {"name": str(second), "bytes": b["bytes"], "sha256": b["sha256"]},
        "same_bytes": a["sha256"] == b["sha256"],
        "message_count_differences": {
            k: {"a": a["messages"].get(k, 0), "b": b["messages"].get(k, 0)}
            for k in kinds if a["messages"].get(k) != b["messages"].get(k)
        },
        "only_in_a_fields": {k: sorted(set(a["fields"].get(k, [])) - set(b["fields"].get(k, [])))
                             for k in kinds if set(a["fields"].get(k, [])) - set(b["fields"].get(k, []))},
        "only_in_b_fields": {k: sorted(set(b["fields"].get(k, [])) - set(a["fields"].get(k, [])))
                             for k in kinds if set(b["fields"].get(k, [])) - set(a["fields"].get(k, []))},
    }
