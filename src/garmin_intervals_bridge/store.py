"""Restart-safe local state and private raw archive, never added to Git."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, base: Path):
        self.base = base
        self.base.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(base / "bridge.db"))
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS activity (
            garmin_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            sha256 TEXT, intervals_id TEXT, updated REAL NOT NULL)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS wellness (
            day TEXT PRIMARY KEY, fetched REAL NOT NULL)""")
        self.db.commit()

    def activity_status(self, garmin_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM activity WHERE garmin_id=?", (garmin_id,)).fetchone()
        return row[0] if row else None

    def record_activity(self, garmin_id: str, status: str, sha: str | None = None,
                        intervals_id: str | None = None) -> None:
        assert status in ("downloaded", "pending", "uploaded", "remote_exists")
        self.db.execute("""INSERT INTO activity (garmin_id,status,sha256,intervals_id,updated)
            VALUES (?,?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET
            status=excluded.status,sha256=excluded.sha256,
            intervals_id=excluded.intervals_id,updated=excluded.updated""",
                        (garmin_id, status, sha, intervals_id, time.time()))
        self.db.commit()

    def wellness_recent(self, day: date, hours: int) -> bool:
        row = self.db.execute("SELECT fetched FROM wellness WHERE day=?", (day.isoformat(),)).fetchone()
        return bool(row and time.time() - row[0] < hours * 3600)

    def mark_wellness(self, day: date) -> None:
        self.db.execute("INSERT INTO wellness(day,fetched) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET fetched=excluded.fetched",
                        (day.isoformat(), time.time()))
        self.db.commit()

    def pending_activities(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT garmin_id FROM activity WHERE status='pending'")]

    @staticmethod
    def atomic_save(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".partial-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def fit_path(self, garmin_id: str) -> Path:
        # Never put unsanitized Garmin-originated identifiers into paths.
        if not garmin_id.isdecimal():
            raise ValueError("Garmin activity ID must be numeric")
        return self.base / "fits" / f"{garmin_id}.fit"

    def save_snapshot(self, day: date, snapshot: dict) -> Path:
        path = self.base / "raw" / f"{day.isoformat()}.json"
        self.atomic_save(path, (json.dumps(snapshot, indent=2, ensure_ascii=False,
                                           default=str, allow_nan=False) + "\n").encode())
        return path

    def close(self) -> None:
        self.db.close()


@contextmanager
def single_instance(data_dir: Path):
    """Avoid overlapping cron runs. Linux Docker is the supported scheduler."""
    import fcntl
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = data_dir / ".bridge.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another bridge instance is running") from exc
        yield
        fcntl.flock(lock, fcntl.LOCK_UN)
