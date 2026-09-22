#!/usr/bin/env python3
"""When the probe pulls the WSL box's activity telemetry to the Mac's vault,
and when it leaves it alone.

This replaces Obsidian Sync for Dashboard/activity/ and calendar-today.md:
Sync keeps a version per write, and the timer that rewrites activity/<date>/
HH.md every 5 minutes manufactured enough version history to peg the 1 GB
remote vault quota twice (2026-09-03, then again 2026-09-17, undiscovered for
six days). The interesting cases here are the ones that would silently
reproduce that failure mode or a worse one: a pull that fires from the WSL
box onto itself, a pull that blocks the menu bar's 30s probe watchdog, and a
retry floor that doesn't hold.

Run: pytest tests/test_activity_pull.py
"""

import importlib.util
import os
import time
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

NOW = datetime(2026, 9, 17, 13, 0, 0, tzinfo=wp.LOCAL)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A pull that records its rsync launches instead of running one, on a
    machine the code believes is the Mac."""
    launched = []
    monkeypatch.setattr(wp, "ACTIVITY_PULL_STAMP", str(tmp_path / "stamp"))
    monkeypatch.setattr(wp, "ACTIVITY_PULL_LOG", str(tmp_path / "pull.log"))
    monkeypatch.setattr(wp.wc, "this_platform", lambda: "darwin")
    monkeypatch.setattr(wp.wc, "dashboard_dir", lambda: str(tmp_path / "vault"))
    monkeypatch.setattr(wp.subprocess, "Popen",
                        lambda *a, **k: launched.append((a, k)) or None)

    class Rig:
        launches = launched
        stamp = tmp_path / "stamp"
    return Rig


def age_stamp(rig, when):
    """The floor is measured from the stamp's mtime, which the filesystem
    sets from the real clock -- so a test driving a fake `now` has to say
    when the attempt happened rather than letting the write decide."""
    os.utime(rig.stamp, (when.timestamp(), when.timestamp()))


def test_a_first_pull_launches_all_three_rsyncs(rig):
    assert wp.pull_mac_activity_if_stale(NOW) is True
    assert len(rig.launches) == 3


def test_a_recent_attempt_stops_a_second_one(rig):
    """Without the floor, a failing or slow pull would respawn every minute,
    all day, over a Tailscale link that isn't always up."""
    assert wp.pull_mac_activity_if_stale(NOW) is True
    age_stamp(rig, NOW)
    assert wp.pull_mac_activity_if_stale(NOW + timedelta(minutes=5)) is False
    assert len(rig.launches) == 3


def test_the_floor_lifts_once_it_has_passed(rig):
    wp.pull_mac_activity_if_stale(NOW)
    age_stamp(rig, NOW)
    assert wp.pull_mac_activity_if_stale(NOW + timedelta(minutes=11)) is True
    assert len(rig.launches) == 6


def test_the_attempt_is_stamped_before_it_is_launched(rig):
    """A pull that hangs or dies writes nothing, so the retry floor has to be
    measured from the attempt, not from a result it failed to produce --
    otherwise a hard failure retries forever."""
    def die(*a, **k):
        raise OSError("no")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(wp.subprocess, "Popen", die)
        assert wp.pull_mac_activity_if_stale(NOW) is False
    assert rig.stamp.exists()
    age_stamp(rig, NOW)
    assert wp.pull_mac_activity_if_stale(NOW + timedelta(minutes=5)) is False


def test_the_wsl_box_never_pulls_from_itself(rig, monkeypatch):
    """The WSL box IS the source the telemetry is written on. Pulling there
    would rsync the tree onto itself, and with --delete on one side of a
    self-copy that can wipe out files the exporter is still writing."""
    monkeypatch.setattr(wp.wc, "this_platform", lambda: "wsl")
    assert wp.pull_mac_activity_if_stale(NOW) is False
    assert rig.launches == [] and not rig.stamp.exists()


def test_plain_linux_also_never_pulls(rig, monkeypatch):
    monkeypatch.setattr(wp.wc, "this_platform", lambda: "linux")
    assert wp.pull_mac_activity_if_stale(NOW) is False
    assert rig.launches == []


def test_the_pull_outlives_the_probe_run_that_started_it(rig):
    """A one-minute child of a process that exits in seconds would otherwise
    be killed halfway through an rsync and leave a truncated mirror."""
    wp.pull_mac_activity_if_stale(NOW)
    assert rig.launches[0][1]["start_new_session"] is True
    assert rig.launches[1][1]["start_new_session"] is True
    assert rig.launches[2][1]["start_new_session"] is True


def test_activity_and_usage_are_mirrored_destructively_calendar_is_not(rig):
    """--delete belongs on activity/ and usage/ (exclusively WSL-authored,
    safe to prune) and must never reach the single calendar file, which is a
    plain copy sitting next to files the Mac's own calendar-refresh.sh can
    write."""
    wp.pull_mac_activity_if_stale(NOW)
    activity_argv, usage_argv, calendar_argv = (
        rig.launches[0][0][0], rig.launches[1][0][0], rig.launches[2][0][0])
    assert "--delete" in activity_argv
    assert "--delete" in usage_argv
    assert "--delete" not in calendar_argv
    assert any(a.rstrip("/").endswith("activity") for a in activity_argv)
    assert any(a.rstrip("/").endswith("usage") for a in usage_argv)
    assert any(a.endswith("calendar-today.md") for a in calendar_argv)


def test_worktime_snapshots_are_never_pulled(rig):
    """Dashboard/worktime/*.json is written BY the Mac locally -- pulling it
    from the WSL box would clobber the Mac's own local state with nothing."""
    wp.pull_mac_activity_if_stale(NOW)
    for argv, _ in rig.launches:
        assert not any("worktime" in a for a in argv)


def test_the_remote_path_carries_the_real_spaces(rig):
    """Pins the exact remote spec, including the spaces in the real Windows
    path -- the classic rsync gotcha is that LOCAL argv quoting (which this
    process never even does, since these are Popen list args, not a shell
    string) says nothing about whether the REMOTE shell re-splits the path.
    That mechanism is verified separately against a real sshd (see the task
    report); this only pins the string this code actually sends."""
    wp.pull_mac_activity_if_stale(NOW)
    activity_argv = rig.launches[0][0][0]
    remote_arg = next(a for a in activity_argv if a.startswith("esme@"))
    assert remote_arg == (
        "esme@100.103.237.24:/mnt/c/Users/Esme Louise Robinson/Documents/"
        "obsidian-vault/Dashboard/activity/")


def test_the_usage_rsync_carries_the_real_remote_path(rig):
    """Pins that Dashboard/usage/ is actually pulled, with --delete, to its
    own rsync call -- not folded into the activity/ mirror or silently
    dropped."""
    wp.pull_mac_activity_if_stale(NOW)
    usage_argv = rig.launches[1][0][0]
    remote_arg = next(a for a in usage_argv if a.startswith("esme@"))
    assert remote_arg == (
        "esme@100.103.237.24:/mnt/c/Users/Esme Louise Robinson/Documents/"
        "obsidian-vault/Dashboard/usage/")
    local_arg = usage_argv[-1]
    assert local_arg.rstrip("/").endswith(os.path.join("vault", "usage"))


def test_a_call_does_not_block_the_probe_run(tmp_path, monkeypatch):
    """The bar's 30s probe watchdog kills the whole run if status() blocks.
    This spawns a REAL slow process in place of rsync -- not a mock recording
    args -- so a regression that adds a .wait()/.communicate() call (or swaps
    Popen for subprocess.run) makes this test actually take 3+ seconds and
    miss its deadline, rather than just failing an assertion about kwargs."""
    monkeypatch.setattr(wp, "ACTIVITY_PULL_STAMP", str(tmp_path / "stamp"))
    monkeypatch.setattr(wp, "ACTIVITY_PULL_LOG", str(tmp_path / "pull.log"))
    monkeypatch.setattr(wp.wc, "this_platform", lambda: "darwin")
    monkeypatch.setattr(wp.wc, "dashboard_dir", lambda: str(tmp_path / "vault"))

    real_popen = wp.subprocess.Popen

    def slow_instead_of_rsync(argv, **kwargs):
        return real_popen(["sleep", "3"], **kwargs)

    monkeypatch.setattr(wp.subprocess, "Popen", slow_instead_of_rsync)

    started = time.monotonic()
    assert wp.pull_mac_activity_if_stale(NOW) is True
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"pull_mac_activity_if_stale blocked for {elapsed}s"


def test_status_calls_the_pull_right_next_to_the_calendar_refresh(monkeypatch):
    """status() is polled every minute by the menu bar -- the pull has to be
    reachable from there, not just from check(), which nothing schedules any
    more (see refresh_calendar_if_stale's call site)."""
    calls = []
    monkeypatch.setattr(wp, "refresh_calendar_if_stale",
                        lambda now: calls.append("calendar"))
    monkeypatch.setattr(wp, "pull_mac_activity_if_stale",
                        lambda now: calls.append("activity"))
    # live_activity and friends are real, heavy calls -- status() only needs
    # to get far enough to prove both refreshers were reached in order, so
    # stub the rest of its body out after the two calls under test by making
    # live_activity raise once it's called; the two refreshers run before it.
    monkeypatch.setattr(wp, "live_activity",
                        lambda day: (_ for _ in ()).throw(RuntimeError("stop")))
    with pytest.raises(RuntimeError, match="stop"):
        wp.status()
    assert calls == ["calendar", "activity"]
