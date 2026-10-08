import pytest

from garmin_intervals_bridge.store import single_instance


def test_scopes_lock_independently(tmp_path):
    with single_instance(tmp_path, ("wellness",)):
        with single_instance(tmp_path, ("activities",)):        # a watcher next to a backfill
            pass
        with pytest.raises(RuntimeError, match="wellness"):
            with single_instance(tmp_path, ("wellness",)):
                pass
        with pytest.raises(RuntimeError, match="wellness"):
            with single_instance(tmp_path, ("activities", "wellness")):   # sync --scope all waits
                pass
    with single_instance(tmp_path, ("activities", "wellness")):        # released cleanly
        pass
