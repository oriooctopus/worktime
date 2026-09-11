"""Tests for the one-time old-day-file -> chunked-layout migration
(bin/activity-migrate-chunks.py). See that file's module docstring for the
migration's own safety story (dry-run default, subset-check when a live
export already wrote the new layout, resumability after an interrupted run).

Run with: python3 -m pytest tests/test_activity_migrate_chunks.py -v
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "activity_migrate_chunks", os.path.join(ROOT, "bin", "activity-migrate-chunks.py"))
mig = importlib.util.module_from_spec(spec)
sys.modules["activity_migrate_chunks"] = mig
spec.loader.exec_module(mig)


OLD_FILE = """---
date: 2026-08-25
generated_at: 2026-08-26T23:56:31-04:00
counts: {{whatsapp_sent: 1}}
---

| time | source | direction | who | detail |
|---|---|---|---|---|
{rows}
{periods_section}
"""


def write_old_file(dir_path, day, rows, periods=()):
    rows_block = "\n".join(
        f"| {t} | {src} | {dirn} | {who} | {detail} |" for t, src, dirn, who, detail in rows
    )
    periods_section = ""
    if periods:
        periods_rows = "\n".join(f"| {s} | {e} | {summary} |" for s, e, summary in periods)
        periods_section = "\n## Periods\n| start | end | summary |\n|---|---|---|\n" + periods_rows + "\n"
    with open(os.path.join(dir_path, f"{day}.md"), "w") as f:
        f.write(OLD_FILE.format(rows=rows_block, periods_section=periods_section))


@pytest.fixture
def activity_dir(tmp_path):
    d = tmp_path / "activity"
    d.mkdir()
    return str(d)


def test_dry_run_makes_no_filesystem_changes(activity_dir):
    write_old_file(activity_dir, "2026-08-25",
                    [("09:00", "whatsapp", "received", "Alice", "hi")])
    before = os.listdir(activity_dir)
    rc = mig.main([activity_dir])
    assert rc == 0
    assert os.listdir(activity_dir) == before  # untouched -- dry-run default


def test_migration_row_for_row_equality(activity_dir):
    rows = [
        ("09:00", "whatsapp", "received", "Alice", "hi there"),
        ("09:05", "whatsapp", "sent", "Alice", "reply with \\| pipe"),
        ("14:30", "chrome", "visit", "Windows", "Page — https://x.com"),
    ]
    periods = [("09:00", "09:05", "chat with Alice"), ("14:30", "14:30", "browsed")]
    write_old_file(activity_dir, "2026-08-25", rows, periods)

    rc = mig.main([activity_dir, "--apply"])
    assert rc == 0
    assert not os.path.exists(os.path.join(activity_dir, "2026-08-25.md"))

    day_dir = os.path.join(activity_dir, "2026-08-25")
    got_rows = mig.events_rows_from_hour_chunks(day_dir)
    assert got_rows == rows  # row-for-row, same order
    got_periods = mig.periods_rows_from_file(os.path.join(day_dir, "periods.md"))
    assert got_periods == periods


def test_migration_is_idempotent(activity_dir):
    write_old_file(activity_dir, "2026-08-25",
                    [("09:00", "whatsapp", "received", "Alice", "hi")],
                    [("09:00", "09:00", "said hi")])
    assert mig.main([activity_dir, "--apply"]) == 0
    day_dir = os.path.join(activity_dir, "2026-08-25")
    snap1 = {
        fn: open(os.path.join(day_dir, fn), "rb").read()
        for fn in os.listdir(day_dir)
    }
    # Re-run against an already-migrated day (no old file left) -- must be a
    # true no-op, not an error and not a rewrite.
    assert mig.main([activity_dir, "--apply"]) == 0
    snap2 = {
        fn: open(os.path.join(day_dir, fn), "rb").read()
        for fn in os.listdir(day_dir)
    }
    assert snap1 == snap2


def test_interrupted_run_is_safe_to_resume(activity_dir):
    """Simulates being killed after chunks were written but before the old
    file was deleted (e.g. process killed between the two): re-running with
    the old file still present must recognize the live dir already covers
    every old row and just delete the stale duplicate, not fail or
    double-write."""
    rows = [("09:00", "whatsapp", "received", "Alice", "hi")]
    write_old_file(activity_dir, "2026-08-25", rows, [("09:00", "09:00", "said hi")])
    assert mig.main([activity_dir, "--apply"]) == 0
    assert not os.path.exists(os.path.join(activity_dir, "2026-08-25.md"))

    # Recreate the old file as if the delete step never ran.
    write_old_file(activity_dir, "2026-08-25", rows, [("09:00", "09:00", "said hi")])
    assert mig.main([activity_dir, "--apply"]) == 0
    assert not os.path.exists(os.path.join(activity_dir, "2026-08-25.md"))


def test_live_directory_with_extra_rows_is_not_clobbered(activity_dir):
    """A day whose new-format directory already exists (the real exporter
    kept running) must never be overwritten with the stale old file's
    content -- only a subset check against what's already there, and only a
    delete of the redundant old file, never a write into the live dir."""
    write_old_file(activity_dir, "2026-08-25",
                    [("09:00", "whatsapp", "received", "Alice", "hi")])
    # Live directory already has this row PLUS a newer one the stale old
    # file never saw.
    from datetime import date
    import importlib.util as ilu
    ae_spec = ilu.spec_from_file_location("ae", os.path.join(ROOT, "bin", "activity-export.py"))
    ae = ilu.module_from_spec(ae_spec)
    ae_spec.loader.exec_module(ae)
    day = date(2026, 8, 25)
    ae.write_day_chunks(activity_dir, day, [
        ae.Event(1, day, "09:00", "whatsapp", "received", "Alice", "hi"),
        ae.Event(2, day, "10:00", "whatsapp", "received", "Alice", "later message"),
    ])

    rc = mig.main([activity_dir, "--apply"])
    assert rc == 0
    assert not os.path.exists(os.path.join(activity_dir, "2026-08-25.md"))
    # The live dir's extra row must survive untouched.
    rows = mig.events_rows_from_hour_chunks(os.path.join(activity_dir, "2026-08-25"))
    assert ("10:00", "whatsapp", "received", "Alice", "later message") in rows


def test_a_write_fault_that_drops_a_row_is_caught_before_deleting_the_old_file(activity_dir, monkeypatch):
    """The migration's own safety net (module docstring: 'RE-READ from disk
    and checked row-for-row ... only on a clean match is the old file
    deleted') must actually stop the delete when the write comes out wrong
    -- not just when everything goes right (G4: this verification gate was
    removed by a mutation and nothing caught it). Faked here by monkeypatching
    ae.write_day_chunks to silently drop a row, simulating a writer bug/
    partial write that the naive 'we just wrote it, it must be fine' code
    path would miss."""
    rows = [
        ("09:00", "whatsapp", "received", "Alice", "row one"),
        ("09:05", "whatsapp", "received", "Alice", "row two"),
    ]
    write_old_file(activity_dir, "2026-08-25", rows)

    real_write_day_chunks = mig.ae.write_day_chunks

    def faulty_write_day_chunks(vault_dir, day, events):
        # Drop the last event before handing off to the real writer -- the
        # written chunk ends up with one fewer row than old_rows expects.
        real_write_day_chunks(vault_dir, day, events[:-1])

    monkeypatch.setattr(mig.ae, "write_day_chunks", faulty_write_day_chunks)

    rc = mig.main([activity_dir, "--apply"])
    assert rc == 1  # reported as a failure, not silently swallowed
    # The old file must SURVIVE a mismatched write -- deleting it here would
    # mean the dropped row ("row two") is now gone from both the old file
    # and the new chunk layout, with nothing left to recover it from.
    assert os.path.exists(os.path.join(activity_dir, "2026-08-25.md"))
    day_dir = os.path.join(activity_dir, "2026-08-25")
    written_rows = mig.events_rows_from_hour_chunks(day_dir)
    assert ("09:05", "whatsapp", "received", "Alice", "row two") not in written_rows


def test_live_directory_missing_an_old_row_is_left_alone(activity_dir):
    """If the live directory somehow does NOT already contain a row the old
    file has, the migration must refuse to delete the old file -- losing a
    row silently is worse than a leftover stale file."""
    write_old_file(activity_dir, "2026-08-25",
                    [("09:00", "whatsapp", "received", "Alice", "a row nobody else has")])
    os.makedirs(os.path.join(activity_dir, "2026-08-25"))  # empty live dir

    rc = mig.main([activity_dir, "--apply"])
    assert rc == 1  # reported as a problem, not silently swallowed
    assert os.path.exists(os.path.join(activity_dir, "2026-08-25.md"))  # left in place
