"""Forum #198: after an update the page said "Not running · last check never" while the background
bridge waited for a Garmin sign-in, a sign-in on the page (its own window) did not wake it for 15
minutes, and the library's warnings about refused sign-in ways looked like the sign-in had failed."""
import logging
import os
import threading
import time
from contextlib import ExitStack

import pytest
from test_webui import (  # noqa: F401  (fixtures)
    call,
    env,  # noqa: F401
    page,
    signed_in,
)

from garmin_intervals_bridge import cli, config, garmin
from garmin_intervals_bridge.garmin import GarminBlocked, GarminLoginNeeded
from garmin_intervals_bridge.store import InstanceBusy, instance_running, single_instance

# ---- the page sees the background bridge by its lock ----

def test_instance_running_sees_who_holds_the_run_lock(tmp_path):
    assert not instance_running(tmp_path)                 # never started: no lock file at all
    with single_instance(tmp_path, ("run",)):
        assert instance_running(tmp_path)
    assert not instance_running(tmp_path)                 # stopped: the file stays, nobody holds it
    with single_instance(tmp_path, ("run",)):             # the look gave the lock back
        pass


def test_a_starting_bridge_waits_out_a_look_at_its_lock(tmp_path):
    taken, release = threading.Event(), threading.Event()

    def look():
        with single_instance(tmp_path, ("run",)):         # what instance_running does, held longer
            taken.set()
            release.wait(5)

    t = threading.Thread(target=look)
    t.start()
    taken.wait(5)
    threading.Timer(0.3, release.set).start()
    started = time.monotonic()
    with ExitStack() as locks:
        cli._take_part(tmp_path, "run", locks, logging.getLogger("test"))
    assert 0.2 <= time.monotonic() - started < cli.LOCK_GRACE_SECONDS
    t.join(5)


def test_other_parts_still_refuse_at_once(tmp_path):
    with single_instance(tmp_path, ("activities",)):
        started = time.monotonic()
        with pytest.raises(InstanceBusy), ExitStack() as locks:
            cli._take_part(tmp_path, "activities", locks, logging.getLogger("test"))
        assert time.monotonic() - started < 0.5


def test_the_page_shows_a_bridge_that_has_not_finished_a_check(page):  # noqa: F811
    settings = config.Settings.from_env()
    cookie = signed_in(page)
    assert "Not running" in call(page, "GET", "/", cookie=cookie)[2]
    with single_instance(settings.data_dir, ("run",)):    # the background bridge, still in its first check
        text = call(page, "GET", "/", cookie=cookie)[2]
        assert "Waiting for the Garmin sign-in" in text and "Not running" not in text      # no session yet
        settings.token_dir.mkdir(parents=True, exist_ok=True)
        (settings.token_dir / "garmin_tokens.json").write_text("{}")
        text = call(page, "GET", "/", cookie=cookie)[2]
        assert "Running</b> &middot; first check in progress" in text and "Not running" not in text
    assert "Not running" in call(page, "GET", "/", cookie=cookie)[2]


# ---- a sign-in in another window wakes the waiting bridge ----

def test_waiting_for_a_sign_in_goes_on_when_another_process_saves_one(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "SIGN_IN_LOOK_SECONDS", 0.05)
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    old = tokens / "garmin_tokens.json"
    old.write_text("{}")
    os.utime(old, (time.time() - 3600, time.time() - 3600))      # the expired session it failed with
    threading.Timer(0.3, lambda: old.write_text('{"new": 1}')).start()   # the page in its own window
    started = time.monotonic()
    assert cli.wait_after_block(GarminLoginNeeded("sign in"), seconds=30, token_dir=tokens) is True
    assert time.monotonic() - started < 5


def test_waiting_for_a_sign_in_without_one_takes_the_whole_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "SIGN_IN_LOOK_SECONDS", 0.05)
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "garmin_tokens.json").write_text("{}")
    os.utime(tokens / "garmin_tokens.json", (time.time() - 3600, time.time() - 3600))
    started = time.monotonic()
    assert cli.wait_after_block(GarminLoginNeeded("sign in"), seconds=0.4, token_dir=tokens) is False
    assert time.monotonic() - started >= 0.35


def test_a_rate_limit_still_waits_the_whole_pause(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    (tmp_path / "garmin_tokens.json").write_text("{}")             # a fresh session is no reason here
    assert cli.wait_after_block(GarminBlocked("429"), seconds=900, token_dir=tmp_path) is False
    assert slept == [900]


# ---- refused sign-in ways: one quiet line when the sign-in works ----

LIBRARY = logging.getLogger("garminconnect.client")


def test_refused_ways_are_one_quiet_line_when_the_sign_in_works(caplog):
    caplog.set_level(logging.INFO)
    with garmin._sign_in_warnings():
        for _ in range(2):
            LIBRARY.warning("%s returned 429: %s", "mobile+cffi", "Mobile login returned 429")
            LIBRARY.warning("%s returned 429: %s", "mobile+requests", "Mobile login returned 429")
        LIBRARY.warning("Token rejected by API tier: %s", "x")                 # not a sign-in way: shows
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["Token rejected by API tier: x"]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == ["Signed in to Garmin; first tried mobile+cffi (HTTP 429), mobile+requests (HTTP 429)"
                     " - Garmin refuses those at times, nothing to do"]
    assert not LIBRARY.filters                                                   # filter removed again


def test_refused_ways_all_show_when_the_sign_in_fails(caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(RuntimeError), garmin._sign_in_warnings():
        LIBRARY.warning("%s returned 429: %s", "mobile+cffi", "Mobile login returned 429")
        LIBRARY.warning("%s failed: %s", "widget+cffi", "boom")
        raise RuntimeError("All login strategies exhausted")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["mobile+cffi returned 429: Mobile login returned 429", "widget+cffi failed: boom"]
    assert not [r for r in caplog.records if r.levelno == logging.INFO]
    assert not LIBRARY.filters


def test_the_page_sign_in_holds_the_warnings_back(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    class FakeLibrary:
        def __init__(self, **kwargs):
            self.password = kwargs.get("password")

        def login(self):
            LIBRARY.warning("%s returned 429: %s", "mobile+cffi", "Mobile login returned 429")
            return "needs_mfa", None

    import garminconnect
    monkeypatch.setattr(garminconnect, "Garmin", FakeLibrary)
    source = garmin.GarminSource(tmp_path / "tokens")
    assert source.start_login("a@b.c", "pw") == "mfa"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("first tried mobile+cffi (HTTP 429)" in r.getMessage() for r in caplog.records)
