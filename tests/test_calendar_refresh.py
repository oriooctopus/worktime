#!/usr/bin/env python3
"""When the probe relaunches the calendar refresh, and when it leaves it alone.

The refresh exists because the vault dump silently stopped being written and
nothing noticed for three days -- so the interesting cases here are the ones
where noticing fails: a dump for yesterday that looks perfectly well-formed, a
refresh that dies without writing anything, and a spawn that would otherwise
repeat once a minute for as long as the failure lasts.

Run: pytest tests/test_calendar_refresh.py
"""

import importlib.util
import os
from datetime import datetime, timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

spec = importlib.util.spec_from_file_location(
    "wp", os.path.join(ROOT, "bin", "worktime-probe.py"))
wp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wp)

NOW = datetime(2026, 9, 9, 13, 0, 0, tzinfo=wp.LOCAL)


def dump(generated, day="2026-09-09"):
    return (f"---\ngenerated: {generated}\n---\n"
            f"# Calendar — Wednesday {day}\n\n"
            "| Start | End | Event | Calendar |\n"
            "|-------|-----|-------|----------|\n"
            "| 12:15 | 12:30 | (busy) | work |\n")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """A refresh command that records its launches instead of running one."""
    launched = []
    cmd = tmp_path / "calendar-refresh.sh"
    cmd.write_text("#!/bin/sh\ntrue\n")
    cmd.chmod(0o755)

    monkeypatch.setattr(wp, "CAL_FILE", str(tmp_path / "calendar-today.md"))
    monkeypatch.setattr(wp, "CAL_REFRESH_STAMP", str(tmp_path / "stamp"))
    monkeypatch.setattr(wp, "CAL_REFRESH_CMD", str(cmd))
    monkeypatch.setattr(wp.subprocess, "Popen",
                        lambda *a, **k: launched.append(a) or None)

    class Rig:
        launches = launched
        cal = tmp_path / "calendar-today.md"
        stamp = tmp_path / "stamp"
        command = cmd
    return Rig


def test_a_fresh_dump_launches_nothing(rig):
    rig.cal.write_text(dump("2026-09-09T12:30:00-04:00"))
    assert wp.refresh_calendar_if_stale(NOW) is False
    assert rig.launches == []


def test_a_dump_going_stale_is_refreshed(rig):
    """Refreshed at two hours, well before the six at which the probe would
    stop trusting it -- the point is to never reach that state."""
    rig.cal.write_text(dump("2026-09-09T10:30:00-04:00"))
    assert wp.refresh_calendar_if_stale(NOW) is True
    assert len(rig.launches) == 1


def test_a_missing_dump_is_refreshed(rig):
    assert wp.refresh_calendar_if_stale(NOW) is True
    assert len(rig.launches) == 1


def test_yesterdays_dump_is_refreshed_however_recently_it_was_written(rig):
    """The failure that hides: a well-formed file, written minutes ago, whose
    meetings are all for the wrong day."""
    rig.cal.write_text(dump("2026-09-09T12:55:00-04:00", day="2026-09-08"))
    assert wp.refresh_calendar_if_stale(NOW) is True


def test_an_unreadable_dump_is_refreshed(rig):
    rig.cal.write_text("half a fi")
    assert wp.refresh_calendar_if_stale(NOW) is True


def age_stamp(rig, when):
    """The floor is measured from the stamp's mtime, which the filesystem sets
    from the real clock -- so a test driving a fake `now` has to say when the
    attempt happened rather than letting the write decide."""
    os.utime(rig.stamp, (when.timestamp(), when.timestamp()))


def test_a_recent_attempt_stops_a_second_one(rig):
    """Without the floor a failing refresh respawns once a minute, all day."""
    rig.cal.write_text(dump("2026-09-09T09:00:00-04:00"))
    assert wp.refresh_calendar_if_stale(NOW) is True
    age_stamp(rig, NOW)
    assert wp.refresh_calendar_if_stale(NOW + timedelta(minutes=5)) is False
    assert len(rig.launches) == 1


def test_the_floor_lifts_once_it_has_passed(rig):
    rig.cal.write_text(dump("2026-09-09T09:00:00-04:00"))
    wp.refresh_calendar_if_stale(NOW)
    age_stamp(rig, NOW)
    assert wp.refresh_calendar_if_stale(NOW + timedelta(minutes=31)) is True
    assert len(rig.launches) == 2


def test_the_attempt_is_stamped_before_it_is_launched(rig):
    """A refresh that hangs or dies writes no dump, so the retry floor has to
    be measured from the attempt rather than from the file it failed to
    produce -- otherwise a hard failure retries forever."""
    def die(*a, **k):
        raise OSError("no")
    rig.cal.write_text(dump("2026-09-09T09:00:00-04:00"))
    with pytest.MonkeyPatch.context() as m:
        m.setattr(wp.subprocess, "Popen", die)
        assert wp.refresh_calendar_if_stale(NOW) is False
    assert rig.stamp.exists()
    age_stamp(rig, NOW)
    assert wp.refresh_calendar_if_stale(NOW + timedelta(minutes=5)) is False


def test_an_uninstalled_refresh_command_is_not_launched(rig, monkeypatch):
    """The probe runs on machines that never installed the refresh. Missing is
    not an error there, and must not cost a stamp that delays a real one."""
    monkeypatch.setattr(wp, "CAL_REFRESH_CMD", str(rig.command) + ".nope")
    assert wp.refresh_calendar_if_stale(NOW) is False
    assert rig.launches == [] and not rig.stamp.exists()


def test_the_refresh_outlives_the_probe_run_that_started_it(rig, monkeypatch):
    """A one-minute child of a process that exits in seconds gets killed
    halfway through and writes nothing."""
    seen = {}
    monkeypatch.setattr(wp.subprocess, "Popen",
                        lambda *a, **k: seen.update(k) or None)
    wp.refresh_calendar_if_stale(NOW)
    assert seen["start_new_session"] is True
