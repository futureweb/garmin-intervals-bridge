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


ACTIVITY_STATUSES = ("downloaded", "pending", "uploaded", "remote_exists", "failed", "skipped")

# States a later *failure* must never downgrade: an upload whose outcome is
# unknown stays pending (manual reconciliation), a completed upload stays done.
GUARDED_STATUSES = ("pending", "uploaded")

# Retry schedule for activities whose processing raised: quick retries for
# transient errors, then rare ones, so an activity that can never be processed
# (e.g. Garmin has no device file for it) costs one request a week instead of
# aborting every run. The last interval repeats.
RETRY_BACKOFF_SECONDS = (3600, 6 * 3600, 24 * 3600, 7 * 86400)


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
        # Additive schema migration for databases created by v0.1.x.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(activity)")}
        for name, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                          ("next_retry", "REAL"), ("error", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE activity ADD COLUMN {name} {ddl}")
        self.db.commit()

    def activity_status(self, garmin_id: str) -> str | None:
        row = self.db.execute("SELECT status FROM activity WHERE garmin_id=?", (garmin_id,)).fetchone()
        return row[0] if row else None

    def record_activity(self, garmin_id: str, status: str, sha: str | None = None,
                        intervals_id: str | None = None) -> None:
        """Record a non-failure outcome; this also clears any earlier failure bookkeeping."""
        assert status in ACTIVITY_STATUSES and status != "failed"
        self.db.execute("""INSERT INTO activity (garmin_id,status,sha256,intervals_id,updated)
            VALUES (?,?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET
            status=excluded.status,sha256=excluded.sha256,
            intervals_id=excluded.intervals_id,updated=excluded.updated,
            attempts=0,next_retry=NULL,error=NULL""",
                        (garmin_id, status, sha, intervals_id, time.time()))
        self.db.commit()

    def record_failure(self, garmin_id: str, error: str) -> int:
        """Mark one activity as failed with exponential backoff; returns the attempt count.

        Guarded states are left untouched so that an upload with an unknown
        outcome is never turned into a retryable failure.
        """
        row = self.db.execute("SELECT status, attempts FROM activity WHERE garmin_id=?",
                              (garmin_id,)).fetchone()
        if row and row[0] in GUARDED_STATUSES:
            return int(row[1])
        attempts = (int(row[1]) if row else 0) + 1
        delay = RETRY_BACKOFF_SECONDS[min(attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
        now = time.time()
        self.db.execute("""INSERT INTO activity (garmin_id,status,sha256,intervals_id,updated,attempts,next_retry,error)
            VALUES (?,'failed',NULL,NULL,?,?,?,?) ON CONFLICT(garmin_id) DO UPDATE SET
            status='failed',updated=excluded.updated,attempts=excluded.attempts,
            next_retry=excluded.next_retry,error=excluded.error""",
                        (garmin_id, now, attempts, now + delay, error[:200]))
        self.db.commit()
        return attempts

    def is_deferred(self, garmin_id: str, now: float | None = None) -> bool:
        """True while a failed activity is still inside its backoff window."""
        row = self.db.execute("SELECT status, next_retry FROM activity WHERE garmin_id=?",
                              (garmin_id,)).fetchone()
        if not row or row[0] != "failed" or row[1] is None:
            return False
        return (now if now is not None else time.time()) < row[1]

    def failed_activities(self) -> list[dict]:
        rows = self.db.execute("""SELECT garmin_id, attempts, next_retry, error FROM activity
                                  WHERE status='failed' ORDER BY next_retry""")
        return [{"garmin_id": r[0], "attempts": r[1], "next_retry": r[2], "error": r[3]} for r in rows]

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
