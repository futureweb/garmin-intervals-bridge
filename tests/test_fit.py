import io
import struct
import zipfile

import pytest

from garmin_intervals_bridge.fit import InvalidFIT, extract_original_fit, sha256, validate_fit


def minimal_fit():
    # Well-shaped synthetic FIT byte layout; no real activity records.
    return bytes([14, 0x10, 0, 0]) + struct.pack("<I", 0) + b".FIT" + b"\x00\x00" + b"\x00\x00"


def test_fit_extraction_preserves_bytes():
    data = minimal_fit()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Folder/TEST.FIT", data)
    assert extract_original_fit(buf.getvalue()) == data
    assert extract_original_fit(data) == data
    assert len(sha256(data)) == 64


def test_fit_rejects_invalid_header_and_mismatched_size():
    with pytest.raises(InvalidFIT):
        validate_fit(b"not fit")
    with pytest.raises(InvalidFIT, match="mismatch"):
        validate_fit(minimal_fit() + b"X")


def test_fit_rejects_multiple_entries():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("first.fit", minimal_fit())
        z.writestr("second.fit", minimal_fit())
    with pytest.raises(InvalidFIT, match="exactly one"):
        extract_original_fit(buf.getvalue())
