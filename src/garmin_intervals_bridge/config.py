"""Config from environment without loading secrets into logs."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _positive_int(name: str, default: int, max_value: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= max_value:
        raise ValueError(f"{name} must be between 1 and {max_value}")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    token_dir: Path
    intervals_api_key: str
    intervals_athlete_id: str
    timezone: ZoneInfo
    activity_days: int
    wellness_days: int
    wellness_refresh_hours: int
    garmin_request_delay: float
    wellness_profile: str = "recommended"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("BRIDGE_DATA_DIR", "./data")).expanduser().resolve()
        token_dir = Path(os.getenv("GARMIN_TOKEN_DIR", str(data_dir / "tokens"))).expanduser().resolve()
        tz = ZoneInfo(os.getenv("BRIDGE_TIMEZONE", "Europe/Vienna"))
        delay = float(os.getenv("BRIDGE_GARMIN_REQUEST_DELAY", "0.5"))
        if delay < 0.25:
            raise ValueError("BRIDGE_GARMIN_REQUEST_DELAY must be >= 0.25 seconds")
        profile = os.getenv("BRIDGE_WELLNESS_PROFILE", "recommended").strip().lower()
        if profile not in ("recommended", "all"):
            raise ValueError("BRIDGE_WELLNESS_PROFILE must be 'recommended' or 'all'")
        athlete_id = os.getenv("INTERVALS_ATHLETE_ID", "0").strip()
        if re.fullmatch(r"i?\d+", athlete_id) is None:
            raise ValueError("Invalid INTERVALS_ATHLETE_ID")
        return cls(
            data_dir=data_dir,
            token_dir=token_dir,
            intervals_api_key=os.getenv("INTERVALS_API_KEY", "").strip(),
            intervals_athlete_id=athlete_id,
            timezone=tz,
            activity_days=_positive_int("BRIDGE_ACTIVITY_LOOKBACK_DAYS", 4, 30),
            wellness_days=_positive_int("BRIDGE_WELLNESS_LOOKBACK_DAYS", 3, 30),
            wellness_refresh_hours=_positive_int("BRIDGE_WELLNESS_REFRESH_HOURS", 4, 72),
            garmin_request_delay=delay,
            wellness_profile=profile,
        )

    def ensure_data_dir(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.data_dir.chmod(0o700)
        except OSError:
            pass
