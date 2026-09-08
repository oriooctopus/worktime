"""Tests for read_history_queries()'s snapshot of Chrome's History. Run with:
    python3 -m pytest test_history_snapshot.py -v

Chrome keeps History open and writing, so the probe reads a copy rather than
the live file. What makes that copy trustworthy is the rollback journal beside
it: these fixtures build a tiny database in the same `journal_mode=delete`
Chrome uses, park it mid-transaction, and check that the copy reads back the
committed state rather than the half-applied one. Nothing here touches the real
Chrome profile on this box.
"""
import importlib.util
import os
import shutil
import sqlite3

import pytest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "worktime_common.py",
)
spec = importlib.util.spec_from_file_location("worktime_common", MODULE_PATH)
wc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc)

ROWS = 4000
COMMITTED = "x" * 400
UNCOMMITTED = "y" * 400


@pytest.fixture
def mid_transaction(tmp_path):
    """A History-shaped database frozen partway through an uncommitted write.

    `cache_size=1` is what makes this reproduce: sqlite only spills dirty pages
    into the database file once its page cache is full, so with a normal cache
    the whole update would sit in memory and a copy of the file would happen to
    be clean. Chrome, writing far more than a test does, spills constantly --
    the tiny cache reproduces in 4000 rows what a real browsing session reaches
    on its own.

    The returned connection is deliberately left inside `BEGIN IMMEDIATE`: the
    hot journal only exists while the transaction is open, and it is the thing
    under test. Closing it would roll the write back and erase the fixture.
    """
    db = str(tmp_path / "History")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=delete")
    con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany(
        "INSERT INTO visits(v) VALUES(?)", [(COMMITTED,) for _ in range(ROWS)]
    )
    con.commit()
    con.close()

    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("PRAGMA cache_size=1")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("UPDATE visits SET v=?", (UNCOMMITTED,))
    assert os.path.exists(db + "-journal"), "fixture never produced a hot journal"
    yield db
    writer.close()


def committed_rows(path):
    return wc.read_history_queries(
        path, [("SELECT count(*) FROM visits WHERE v LIKE 'x%'", [])]
    )[0][0][0]


def test_reads_committed_state_not_the_open_transaction(mid_transaction):
    """The snapshot shows the write as not-yet-happened, never half-happened."""
    assert committed_rows(mid_transaction) == ROWS


def test_journal_is_copied_alongside_the_database(mid_transaction, monkeypatch):
    """The rollback journal is what the guarantee above is actually made of."""
    seen = []
    real = shutil.copy2
    monkeypatch.setattr(
        shutil, "copy2",
        lambda s, d, *a, **k: (seen.append(os.path.basename(s)), real(s, d, *a, **k))[1],
    )
    committed_rows(mid_transaction)
    assert seen == ["History", "History-journal"]


def test_database_alone_would_have_read_the_uncommitted_write(mid_transaction, tmp_path):
    """Why the journal is not optional: without it the copy is silently wrong.

    This is the pre-fix behaviour, asserted directly so the regression cannot
    return unnoticed. Note that `integrity_check` passes on that copy -- the
    half-applied state is a structurally valid database holding data no reader
    ever committed, which is why the bug spent so long looking like an
    occasional `database disk image is malformed` rather than the everyday
    wrong answer it mostly was.
    """
    lone = str(tmp_path / "copy" / "History")
    os.makedirs(os.path.dirname(lone))
    shutil.copy2(mid_transaction, lone)
    con = sqlite3.connect(lone)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    leaked = con.execute("SELECT count(*) FROM visits WHERE v LIKE 'y%'").fetchone()[0]
    con.close()
    assert leaked > 0, "fixture did not spill uncommitted pages into the file"


def test_wal_sidecars_are_copied_when_present(tmp_path, monkeypatch):
    """Chrome has not always used `delete`; a WAL profile must snapshot too."""
    db = str(tmp_path / "History")
    con = sqlite3.connect(db)
    con.execute("PRAGMA journal_mode=wal")
    con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO visits(v) VALUES('x')")
    con.commit()

    seen = []
    real = shutil.copy2
    monkeypatch.setattr(
        shutil, "copy2",
        lambda s, d, *a, **k: (seen.append(os.path.basename(s)), real(s, d, *a, **k))[1],
    )
    assert committed_rows(db) == 1
    con.close()
    assert seen[0] == "History"
    assert "History-wal" in seen
