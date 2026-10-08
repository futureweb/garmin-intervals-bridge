import time
from types import SimpleNamespace

from garmin_intervals_bridge.health import probe
from garmin_intervals_bridge.store import Store


class Garmin:
    def __init__(self, ok=True):
        self.ok = ok

    def login(self, interactive=False):
        if not self.ok:
            raise RuntimeError("Garmin login needed")


class Intervals:
    def __init__(self, ok=True):
        self.ok = ok

    def activities(self, start, end, fields=None, limit=None):
        if not self.ok:
            raise ConnectionError("down")
        return []


def test_healthy_probe_records_success_and_exits_zero(tmp_path):
    st = Store(tmp_path)
    r = probe(SimpleNamespace(stale_hours=24), Garmin(), Intervals(), st)
    assert r["exit"] == 0 and r["services"]["garmin"]["ok"] and r["services"]["intervals"]["ok"]
    assert st.meta_updated("garmin_last_ok") is not None
    st.close()


def test_failure_inside_the_window_is_a_warning_not_an_alert(tmp_path):
    st = Store(tmp_path)
    st.set_meta("garmin_last_ok")                       # succeeded just now
    r = probe(SimpleNamespace(stale_hours=24), Garmin(ok=False), Intervals(), st, now=time.time() + 3 * 3600)
    assert r["exit"] == 0 and r["services"]["garmin"]["ok"] is False
    assert "stale" not in r["services"]["garmin"] and r["services"]["garmin"]["last_ok_age_hours"] == 3.0
    st.close()


def test_failure_beyond_the_window_or_never_ok_exits_two(tmp_path):
    st = Store(tmp_path)
    r = probe(SimpleNamespace(stale_hours=24), Garmin(ok=False), Intervals(), st)
    assert r["exit"] == 2 and r["services"]["garmin"]["stale"] is True          # never succeeded
    st.set_meta("garmin_last_ok")
    r = probe(SimpleNamespace(stale_hours=24), Garmin(ok=False), Intervals(), st, now=time.time() + 30 * 3600)
    assert r["exit"] == 2 and r["services"]["garmin"]["last_ok_age_hours"] == 30.0
    st.close()
