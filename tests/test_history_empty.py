#!/usr/bin/env python3
"""An empty Chrome History is no browsing; a wrong one is still an error.

When this machine's disk hit 100% full, Chrome could not open its History and
recreated it: a 32KB file with zero tables where 61MB of visits had been.
Every query then raised `no such table: visits`, and because the probe raises
rather than guessing, that killed every poll -- the dot went red and stayed
red for a browser that was merely empty.

Empty is a real state and belongs with the absent History the caller already
handles. What must NOT happen is that becoming a blanket tolerance for sqlite
errors: a `no such table` from a database that has tables means the schema
moved, and the malformed-image case is the exact bug the journal copy exists
to prevent. Both still have to raise. That line is what this file pins.

Run: pytest tests/test_history_empty.py
"""

import importlib.util
import os
import sqlite3
import unittest

MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "worktime_common.py",
)
spec = importlib.util.spec_from_file_location("worktime_common", MODULE_PATH)
wc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wc)

VISITS = "SELECT id FROM visits"


class EmptyHistory(unittest.TestCase):
    def db(self, tmp_path, build=None):
        path = str(tmp_path / "History")
        con = sqlite3.connect(path)
        if build:
            build(con)
        con.commit()
        con.close()
        return path

    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)

    def test_a_database_with_no_tables_reads_as_no_browsing(self):
        """Exactly the file Chrome left behind when the disk filled."""
        path = self.db(self.path)
        self.assertEqual(wc.read_history_queries(path, [(VISITS, [])]), [[]])

    def test_every_query_gets_an_empty_result_not_just_the_first(self):
        """Callers ask for several answers about one moment in one copy."""
        path = self.db(self.path)
        got = wc.read_history_queries(
            path, [(VISITS, []), ("SELECT url FROM urls", [])])
        self.assertEqual(got, [[], []])

    def test_an_empty_database_is_not_confused_with_a_populated_one(self):
        def build(con):
            con.execute("CREATE TABLE visits(id INTEGER PRIMARY KEY)")
            con.executemany("INSERT INTO visits(id) VALUES(?)", [(1,), (2,)])
        path = self.db(self.path, build)
        self.assertEqual(wc.read_history_queries(path, [(VISITS, [])]),
                         [[(1,), (2,)]])


class WrongIsStillWrong(unittest.TestCase):
    """The empty case must not become a blanket catch."""

    def setUp(self):
        import tempfile, pathlib
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name)

    def test_a_missing_table_in_a_populated_database_still_raises(self):
        """That means the schema is not what this code expects -- a bug."""
        path = str(self.path / "History")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE something_else(id INTEGER PRIMARY KEY)")
        con.commit()
        con.close()
        with self.assertRaises(sqlite3.OperationalError):
            wc.read_history_queries(path, [(VISITS, [])])

    def test_a_corrupt_database_still_raises(self):
        """The malformed image is what the journal copy exists to prevent."""
        path = str(self.path / "History")
        with open(path, "wb") as fh:
            fh.write(b"SQLite format 3\x00" + os.urandom(4000))
        with self.assertRaises(sqlite3.DatabaseError):
            wc.read_history_queries(path, [(VISITS, [])])


if __name__ == "__main__":
    unittest.main()
