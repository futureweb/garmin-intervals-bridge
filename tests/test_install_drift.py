"""Forum #205/#207: the page ran from another install than the background bridge (a second venv, another
Python with the bridge installed) – or was started before an update – and nobody could tell; its update hint
and its buttons reached the page's copy only. The bridge now says what it runs, the page compares."""
import json
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from test_easy_setup import Runner
from test_webui import call, csrf, env, page, signed_in  # noqa: F401  (fixtures)

from garmin_intervals_bridge import __version__, autostart, cli, config, webui
from garmin_intervals_bridge.store import BRIDGE_INSTALL, Store

PACKAGE = str(Path(config.__file__).resolve().parent)


def _bridge_says(info: dict) -> None:
    store = Store(config.Settings.from_env().data_dir)
    store.set_meta(BRIDGE_INSTALL, json.dumps(info))
    store.close()


@pytest.fixture
def running(monkeypatch):
    monkeypatch.setattr(webui, "instance_running", lambda data_dir: True)


# ---- what a process runs ----

def test_install_names_version_python_package_and_settings(env):  # noqa: F811
    info = config.install()
    assert info == {"version": __version__, "python": sys.executable, "package": PACKAGE,
                    "env_file": str(env.resolve())}


def test_the_background_bridge_says_what_it_runs_when_it_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "sync_enrich", lambda *a, **k: pytest.fail("no cycle in this test"))
    settings = SimpleNamespace(data_dir=tmp_path, token_dir=tmp_path / "tokens", intervals_api_key="k",
                               intervals_athlete_id="0", garmin_request_delay=0.5, timezone=ZoneInfo("Europe/Vienna"),
                               activity_days=3, wellness_days=3, wellness_refresh_hours=4, wellness_profile="all",
                               stale_hours=24)
    store = Store(tmp_path)
    cli.run_loop(settings, store, apply=False, poll_seconds=300, sync_minutes=30, iterations=0)
    said = json.loads(store.get_meta(BRIDGE_INSTALL))
    store.close()
    assert said["package"] == PACKAGE and said["python"] == sys.executable and said["version"] == __version__


# ---- the commands for the bridge's copy ----

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX paths and shell quoting")
def test_commands_use_the_installs_own_python_and_settings_file(_empty_home, monkeypatch):
    monkeypatch.setattr(webui.autostart, "platform", lambda: "macos")
    pip, bridge = webui.install_commands({"python": str(_empty_home / "GarminBridge" / "venv" / "bin" / "python"),
                                          "env_file": str(_empty_home / ".env")})
    assert pip == "~/GarminBridge/venv/bin/python -m pip"
    assert bridge == "~/GarminBridge/venv/bin/python -m garmin_intervals_bridge --env-file ~/.env"
    pip, bridge = webui.install_commands({"python": "/Users/ann/My Python/bin/python3", "env_file": ""})
    assert pip == "'/Users/ann/My Python/bin/python3' -m pip"
    assert bridge == "'/Users/ann/My Python/bin/python3' -m garmin_intervals_bridge"


def test_on_windows_the_command_shows_output_and_runs_in_powershell(monkeypatch):
    monkeypatch.setattr(webui.autostart, "platform", lambda: "windows")
    pip, bridge = webui.install_commands({"python": r"C:\Program Files\Python313\pythonw.exe",
                                          "env_file": r"C:\Users\ann\GarminBridge\.env"})
    assert pip == r'& "C:\Program Files\Python313\python.exe" -m pip'          # python, not pythonw: output
    assert bridge.endswith(r"-m garmin_intervals_bridge --env-file C:\Users\ann\GarminBridge\.env")


def test_the_update_hint_reaches_the_bridges_copy(page, _empty_home, monkeypatch):  # noqa: F811
    monkeypatch.setattr(webui.autostart, "platform", lambda: "macos")
    framework = "/Library/Frameworks/Python.framework/Versions/3.15/bin/python3"
    info = {"python": framework, "env_file": str(_empty_home / ".env")}
    pip, bridge = webui.install_commands(info)
    assert pip == f"{framework} -m pip" and bridge.startswith(f"{framework} -m garmin_intervals_bridge --env-file ")
    hint = page._how_to_update(info)
    for command in (f"{pip} install --upgrade garmin-intervals-bridge", f"{bridge} autostart", f"{bridge} ui"):
        assert f"<code>{webui.esc(command)}</code>" in hint


# ---- the page compares ----

def test_a_page_from_another_install_says_so_and_locks_its_buttons(page, env, running):  # noqa: F811
    info = {"version": __version__, "python": "/Library/Frameworks/Python.framework/Versions/3.15/bin/python3",
            "package": "/Library/Frameworks/Python.framework/Versions/3.15/lib/python3.15/site-packages/"
                       "garmin_intervals_bridge", "env_file": str(env.resolve())}
    _bridge_says(info)
    cookie = signed_in(page)
    text = call(page, "GET", "/", cookie=cookie)[2]
    assert "This page runs from another install than the background bridge" in text
    bridge = webui.install_commands(info)[1]
    assert bridge.startswith("/Library/Frameworks/Python.framework/Versions/3.15/bin/python3 -m garmin_intervals_bridge")
    assert webui.esc(f"{bridge} ui") in text                    # the bridge's Python and settings file
    token = csrf(call(page, "GET", "/settings", cookie=cookie)[2])
    status, _, text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Europe/Dublin"}, cookie=cookie)
    assert status == 200 and "Locked" in text and "Europe/Vienna" in env.read_text()       # nothing changed
    text = call(page, "POST", "/poll-now", {"csrf": token}, cookie=cookie)[2]
    assert "Locked" not in text                                       # "Check now" only wakes the bridge


def test_a_page_started_before_the_update_says_so_and_locks_its_buttons(page, env, running):  # noqa: F811
    _bridge_says({"version": "99.0.0", "python": sys.executable, "package": PACKAGE, "env_file": str(env.resolve())})
    cookie = signed_in(page)
    text = call(page, "GET", "/", cookie=cookie)[2]
    assert f"This page still runs {__version__}</b>, the background bridge 99.0.0" in text
    assert text.count("This page still runs") == 1                       # not twice with the installed check
    token = csrf(call(page, "GET", "/settings", cookie=cookie)[2])
    text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Europe/Dublin"}, cookie=cookie)[2]
    assert "Locked" in text and "Europe/Vienna" in env.read_text()


def test_a_bridge_not_restarted_after_the_update_is_named_and_the_buttons_stay(page, env, running):  # noqa: F811
    _bridge_says({"version": "0.1.0", "python": sys.executable, "package": PACKAGE, "env_file": str(env.resolve())})
    cookie = signed_in(page)
    text = call(page, "GET", "/", cookie=cookie)[2]
    assert "The background bridge still runs 0.1.0</b>" in text and " autostart</code> restarts the one autostart set up" in text
    token = csrf(call(page, "GET", "/settings", cookie=cookie)[2])
    text = call(page, "POST", "/settings", {"csrf": token, "timezone": "Europe/Dublin"}, cookie=cookie)[2]
    assert "Locked" not in text


def test_nothing_is_said_without_a_running_bridge_or_when_both_match(page, env, monkeypatch):  # noqa: F811
    other = {"version": "0.1.0", "python": "/elsewhere/python", "package": "/elsewhere/garmin_intervals_bridge",
             "env_file": str(env.resolve())}
    _bridge_says(other)
    monkeypatch.setattr(webui, "instance_running", lambda data_dir: False)       # what it said is old news
    cookie = signed_in(page)
    assert "another install" not in call(page, "GET", "/", cookie=cookie)[2]
    monkeypatch.setattr(webui, "instance_running", lambda data_dir: True)
    _bridge_says({**other, "version": __version__, "package": PACKAGE})
    text = call(page, "GET", "/", cookie=cookie)[2]
    assert "another install" not in text and "still runs" not in text


def test_a_page_that_is_part_of_the_bridge_never_compares(env, running):  # noqa: F811
    _bridge_says({"version": "0.1.0", "python": "/x", "package": "/elsewhere", "env_file": ""})
    ui = webui.UI(host="127.0.0.1", port=0, password="long enough", in_run=True)
    assert ui.drift(config.Settings.from_env()) is None


# ---- autostart says when it moves the bridge to another install ----

def test_registered_reads_python_and_settings_back(tmp_path, _empty_home, monkeypatch):
    env_file = tmp_path / ".env"
    assert autostart.registered(env_file, home=_empty_home, kind="macos") is None           # none yet
    autostart.install(env_file, runner=Runner(fail=(["launchctl", "bootout"],)), home=_empty_home, kind="macos")
    expected = {"python": sys.executable, "env_file": str(env_file.resolve())}
    assert autostart.registered(env_file, home=_empty_home, kind="macos") == expected
    if sys.platform != "win32":                       # a systemd unit on Windows: POSIX quoting, never used
        monkeypatch.setattr(autostart.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(autostart.shutil, "which", lambda name: "/usr/bin/systemctl")
        autostart.install(env_file, runner=Runner(), home=_empty_home, kind="linux")
        assert autostart.registered(env_file, home=_empty_home, kind="linux") == expected
    autostart.install(env_file, runner=Runner(), home=_empty_home, kind="windows")
    windowless = autostart._python(windowless=True)                # the task runs pythonw.exe: no window
    assert autostart.registered(env_file, home=_empty_home, kind="windows") == {**expected, "python": windowless}


def test_autostart_from_another_install_says_it_moves_the_bridge(tmp_path, _empty_home, monkeypatch, capsys):
    env_file = tmp_path / ".env"
    env_file.write_text("INTERVALS_API_KEY=k\n")
    monkeypatch.setattr(autostart, "platform", lambda: "macos")
    monkeypatch.setattr(autostart, "_run", Runner(fail=(["launchctl", "bootout"],)))
    agent = _empty_home / "Library" / "LaunchAgents" / f"{autostart.LABEL}.plist"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(plistlib.dumps({"Label": autostart.LABEL, "ProgramArguments": [
        "/old/venv/bin/python", "-m", "garmin_intervals_bridge", "--env-file", "/old/.env", "run", "--apply"]}))
    assert cli.main(["--env-file", str(env_file), "autostart"]) == 0
    out = capsys.readouterr().out
    assert (f"Note: it ran /old/venv/bin/python with the settings /old/.env; from now on {sys.executable} with "
            f"{env_file.resolve()}.") in out
    assert cli.main(["--env-file", str(env_file), "autostart"]) == 0
    assert "Note: it ran" not in capsys.readouterr().out                   # the same again: nothing to say
    cli.main(["--env-file", str(env_file), "autostart", "--status"])
    assert f"Starts: {sys.executable} with the settings {env_file.resolve()}" in capsys.readouterr().out
