"""Forum #201: the autostarted bridge did not stay up and said nowhere why (launchd threw its stderr away,
the log file was empty), and the page printed a traceback when the browser left before the answer."""
import logging
import os
import plistlib
import sys
import time
from http.server import BaseHTTPRequestHandler
from types import SimpleNamespace

from test_easy_setup import Runner
from test_webui import call, env, page, signed_in  # noqa: F401  (fixtures)

from garmin_intervals_bridge import autostart, cli, webui

# ---- macOS: the launch agent keeps the bridge's errors ----

def test_the_launch_agent_writes_errors_to_a_file_next_to_the_settings(tmp_path, _empty_home):
    env_file = tmp_path / ".env"
    autostart.install(env_file, runner=Runner(fail=(["launchctl", "bootout"],)), home=_empty_home, kind="macos")
    agent = plistlib.loads((_empty_home / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist").read_bytes())
    errors = str(tmp_path.resolve() / autostart.ERROR_LOG)
    assert agent["StandardErrorPath"] == errors
    assert agent["EnvironmentVariables"] == {"BRIDGE_AUTOSTARTED": "1", "BRIDGE_AUTOSTART_ERRORS": errors}
    assert "StandardOutPath" not in agent                       # the regular log stays BRIDGE_LOG_FILE


def test_status_reads_state_and_last_exit_code_from_launchctl(tmp_path, _empty_home):
    printed = "\n".join([f"gui/501/{autostart.LABEL} = {{"] + [f"\tline {n}" for n in range(15)]
                        + ["\tstate = not running", "\tlast exit code = 1", "}"])
    state = autostart.status(tmp_path / ".env", runner=lambda cmd: SimpleNamespace(stdout=printed),
                             home=_empty_home, kind="macos")
    assert state["installed"] and state["state"] == "not running" and state["last_exit"] == "1"
    assert len(state["detail"].splitlines()) == 12                   # what `autostart --status` prints


def test_last_error_is_the_last_line_of_a_recent_error_file(tmp_path):
    env_file = tmp_path / ".env"
    assert autostart.last_error(env_file) is None                    # never written
    path = autostart.error_log(env_file)
    path.write_text("Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'y'\n\n")
    assert autostart.last_error(env_file) == "ModuleNotFoundError: No module named 'y'"
    old = time.time() - autostart.LAST_ERROR_MAX_AGE - 60
    os.utime(path, (old, old))
    assert autostart.last_error(env_file) is None                    # an old error says nothing about now


# ---- the autostarted bridge: only errors on stderr, the file kept small ----

def test_an_autostarted_bridge_logs_only_errors_to_its_error_file(tmp_path, monkeypatch):
    errors = tmp_path / autostart.ERROR_LOG
    errors.write_bytes(b"".join(b"INFO line %d\n" % n for n in range(200_000)))      # ~2.4 MB of old noise
    monkeypatch.setenv("BRIDGE_AUTOSTARTED", "1")
    monkeypatch.setenv("BRIDGE_AUTOSTART_ERRORS", str(errors))
    stream = logging.StreamHandler()
    logging.getLogger().addHandler(stream)
    try:
        cli._autostarted_stderr()
        assert stream.level == logging.ERROR
    finally:
        logging.getLogger().removeHandler(stream)
    assert "BRIDGE_AUTOSTARTED" not in os.environ and "BRIDGE_AUTOSTART_ERRORS" not in os.environ   # children log
    body = errors.read_bytes()
    assert body.startswith(b"(older lines removed)\nINFO line ") and body.endswith(b"INFO line 199999\n")
    assert len(body) <= cli.AUTOSTART_ERRORS_KEEP + 64
    if sys.platform != "win32":                                      # POSIX file modes
        assert oct(errors.stat().st_mode & 0o777) == "0o600"


def test_a_small_error_file_is_left_alone(tmp_path, monkeypatch):
    errors = tmp_path / autostart.ERROR_LOG
    errors.write_text("ERROR something\n")
    monkeypatch.setenv("BRIDGE_AUTOSTART_ERRORS", str(errors))
    cli._autostarted_stderr()
    assert errors.read_text() == "ERROR something\n"


# ---- the page says why the background bridge does not stay up ----

def test_the_page_names_exit_code_and_last_error(page, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(webui.autostart, "status", lambda env_file: {"installed": True, "kind": "macos", "detail": "",
                                                                      "state": "not running", "last_exit": "1"})
    autostart.error_log(env).write_text("ERROR Intervals.icu rejected the API key\n")
    text = call(page, "GET", "/", cookie=signed_in(page))[2]
    assert "Not running" in text and "it stopped with exit code 1" in text
    assert "its last error: <code>ERROR Intervals.icu rejected the API key</code>" in text


def test_the_page_adds_nothing_when_the_bridge_never_stopped(page, env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(webui.autostart, "status", lambda env_file: {"installed": True, "kind": "macos", "detail": "",
                                                                      "state": "running", "last_exit": "(never exited)"})
    text = call(page, "GET", "/", cookie=signed_in(page))[2]
    assert "exit code" not in text and "last error" not in text


# ---- a browser that left: no traceback in the window ----

def test_a_browser_that_left_is_no_traceback(capsys):
    server = webui._Server(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        for gone in (BrokenPipeError(32, "Broken pipe"), ConnectionResetError(54, "reset"), ConnectionAbortedError()):
            try:
                raise gone
            except OSError:
                server.handle_error(None, ("127.0.0.1", 49897))
        assert capsys.readouterr().err == ""
        try:
            raise ValueError("a real error")
        except ValueError:
            server.handle_error(None, ("127.0.0.1", 49897))
        assert "ValueError: a real error" in capsys.readouterr().err        # real errors still show
    finally:
        server.server_close()
