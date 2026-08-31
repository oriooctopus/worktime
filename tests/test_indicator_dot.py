"""Tests for skills/indicator-dot/indicator-dot.py."""
import importlib.util, os, sys
from datetime import datetime

import pytest

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(ROOT, "skills", "indicator-dot", "indicator-dot.py")
spec = importlib.util.spec_from_file_location("indicator_dot", MODULE_PATH)
ind = importlib.util.module_from_spec(spec)
sys.modules["indicator_dot"] = ind
spec.loader.exec_module(ind)

TZ = ZoneInfo("America/New_York")
DOC = """---
generated: 2026-08-31T16:42:38-04:00
---
| Start | End | Event | Calendar |
|-------|-----|-------|----------|
| 10:00 | 10:30 | (busy) | work |
| 14:03 | 14:30 | Working (browsing) | work |
| 16:30 | 17:30 | (busy) | work |
| 18:00 | 19:00 | Soccer | personal |
"""

# A row with an empty Calendar column -- old exporters that predated the
# Calendar column produce these. The probe counts them as work; so must we.
DOC_WITH_EMPTY_TAG = """---
generated: 2026-08-31T16:42:38-04:00
---
| Start | End | Event | Calendar |
|-------|-----|-------|----------|
| 10:00 | 11:00 | Legacy meeting |  |
"""


def at(h, m):
    return datetime(2026, 8, 31, h, m, tzinfo=TZ)


# ---------------------------------------------------------------------------
# Calendar-based path (report()) — covers --at and calendar-only fallback
# ---------------------------------------------------------------------------

def test_parses_rows_and_skips_the_header():
    rows, gen = ind.parse(DOC)
    assert len(rows) == 4
    assert gen == "2026-08-31T16:42:38-04:00"


def test_green_while_a_work_row_covers_now():
    code, lines = ind.report(DOC, at(16, 48), age_hours=0.1)
    assert code == 0 and lines[0].startswith("GREEN")
    assert "42 min left" in lines[0]


def test_amber_when_nothing_covers_now():
    code, lines = ind.report(DOC, at(13, 0), age_hours=0.1)
    assert code == 1 and lines[0].startswith("AMBER")


def test_personal_rows_never_turn_it_green():
    """A personal appointment is not working time."""
    code, lines = ind.report(DOC, at(18, 30), age_hours=0.1)
    assert code == 1 and lines[0].startswith("AMBER")


def test_empty_tag_rows_count_as_work():
    """Rows with no Calendar tag come from old exporters; the probe counts them."""
    code, lines = ind.report(DOC_WITH_EMPTY_TAG, at(10, 30), age_hours=0.1)
    assert code == 0 and lines[0].startswith("GREEN")


def test_rubrik_tag_rows_count_as_work():
    """'rubrik' is a legacy tag the probe accepts in WORK_TAGS."""
    doc = DOC.replace("| (busy) | work |", "| (busy) | rubrik |", 1)
    code, _ = ind.report(doc, at(10, 15), age_hours=0.1)
    assert code == 0


def test_amber_names_the_previous_and_next_stretch():
    _, lines = ind.report(DOC, at(15, 0), age_hours=0.1)
    body = " ".join(lines)
    assert "ended 14:30" in body and "starts 16:30" in body


def test_boundary_end_is_exclusive():
    """At exactly the end minute the meeting is over, not still running."""
    code, _ = ind.report(DOC, at(17, 30), age_hours=0.1)
    assert code == 1


def test_boundary_start_is_inclusive():
    code, _ = ind.report(DOC, at(16, 30), age_hours=0.1)
    assert code == 0


def test_overlapping_rows_report_the_longest_first():
    doc = DOC + "| 16:40 | 16:45 | Working | work |\n"
    _, lines = ind.report(doc, at(16, 42), age_hours=0.1)
    assert "(16:30-17:30)" in lines[0]
    assert any("also covering now" in l for l in lines)


def test_stale_data_is_flagged_and_downgraded():
    code, lines = ind.report(DOC, at(16, 48), age_hours=9.0)
    assert code == 2 and any("STALE" in l for l in lines)


def test_no_rows_is_unknown():
    code, lines = ind.report("---\ngenerated: x\n---\n", at(16, 48))
    assert code == 2 and "UNKNOWN" in lines[0]


# ---------------------------------------------------------------------------
# Probe path (report_probe()) — authoritative current-time verdict
# ---------------------------------------------------------------------------

def test_report_probe_working_is_green():
    status = {"state": "working", "why": "2m since last activity",
              "worked_minutes": 183, "mode": "focused", "focus_pct": 78,
              "periods": [{"start": 540, "end": 600, "len": 60,
                           "n_prompts": 12, "n_slack": 0, "what": "auth fix",
                           "current": True}]}
    code, lines = ind.report_probe(status)
    assert code == 0
    assert lines[0].startswith("GREEN")
    assert "2m since last activity" in lines[0]
    assert any("3h 3m" in l for l in lines)


def test_report_probe_marked_is_blue():
    status = {"state": "marked", "why": "reading a PR",
              "worked_minutes": 90, "mode": "focused", "focus_pct": None,
              "periods": []}
    code, lines = ind.report_probe(status)
    assert code == 0
    assert lines[0].startswith("BLUE")


def test_report_probe_idle_is_amber():
    status = {"state": "idle", "why": "quiet 23m",
              "worked_minutes": 240, "mode": "focused", "focus_pct": 80,
              "quiet_since": "14:05", "periods": []}
    code, lines = ind.report_probe(status)
    assert code == 1
    assert lines[0].startswith("AMBER")
    assert any("14:05" in l for l in lines)


def test_report_probe_unknown_state_is_unusable():
    status = {"state": "rebooting", "why": "???"}
    code, lines = ind.report_probe(status)
    assert code == 2
    assert "UNKNOWN" in lines[0]


def test_report_probe_periods_are_listed():
    status = {"state": "working", "why": "1m since last activity",
              "worked_minutes": 60, "mode": "focused", "focus_pct": 90,
              "periods": [
                  {"start": 600, "end": 660, "len": 60,
                   "n_prompts": 5, "n_slack": 0, "what": "fixing bug",
                   "current": True},
                  {"start": 540, "end": 598, "len": 58,
                   "n_prompts": 8, "n_slack": 2, "what": "code review",
                   "current": False},
              ]}
    code, lines = ind.report_probe(status)
    assert code == 0
    period_lines = [l for l in lines if "10:" in l]
    assert len(period_lines) >= 1
    assert any("fixing bug" in l for l in lines)


# ---------------------------------------------------------------------------
# Terminal colouring
# ---------------------------------------------------------------------------

def test_colourise_marks_green_and_dims_context():
    out = ind.colourise(["GREEN - working: x", "  data 1 min old"])
    assert out[0].startswith(ind.COLOURS["GREEN"]) and out[0].endswith(ind.RESET)
    assert out[1].startswith(ind.DIM)


def test_colourise_uses_a_different_colour_for_amber():
    assert ind.colourise(["AMBER - nope"])[0].startswith(ind.COLOURS["AMBER"])


def test_colourise_uses_blue_for_marked():
    assert ind.colourise(["BLUE - reading a PR"])[0].startswith(ind.COLOURS["BLUE"])


def test_colourise_flags_stale_lines_even_when_indented():
    out = ind.colourise(["GREEN - working: x", "  STALE: data 9.0h old"])
    assert ind.COLOURS["STALE"] in out[1]


def test_report_itself_stays_plain_so_output_can_be_parsed():
    _, lines = ind.report(DOC, at(16, 48), age_hours=0.1)
    assert all("\033" not in l for l in lines)


def test_report_probe_stays_plain():
    status = {"state": "working", "why": "active", "worked_minutes": 60,
              "mode": "focused", "focus_pct": None, "periods": []}
    _, lines = ind.report_probe(status)
    assert all("\033" not in l for l in lines)
