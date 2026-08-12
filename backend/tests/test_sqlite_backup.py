import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from infrastructure.sqlite_backup import create_backup


class SqliteBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "assistant.db"
        self.backups = self.root / "backups"
        with sqlite3.connect(self.source) as connection:
            connection.execute(
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, text TEXT)"
            )
            connection.executemany(
                "INSERT INTO messages(text) VALUES (?)",
                [("one",), ("two",), ("three",)],
            )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_backup_is_consistent_and_keeps_seven_files(self):
        for index in range(9):
            create_backup(
                self.source,
                self.backups,
                now=f"20260812T120{index:02d}Z",
                keep=7,
            )

        files = sorted(self.backups.glob("assistant-*.db"))
        self.assertEqual(len(files), 7)
        self.assertEqual(files[0].name, "assistant-20260812T12002Z.db")
        with sqlite3.connect(files[-1]) as connection:
            self.assertEqual(
                connection.execute("PRAGMA integrity_check").fetchone(),
                ("ok",),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM messages"
                ).fetchone(),
                (3,),
            )

    def test_invalid_keep_does_not_create_backup(self):
        with self.assertRaisesRegex(ValueError, "^keep must be positive$"):
            create_backup(
                self.source,
                self.backups,
                now="20260812T120000Z",
                keep=0,
            )

        self.assertFalse(self.backups.exists())

    def test_missing_source_does_not_leave_temporary_file(self):
        with self.assertRaises(FileNotFoundError):
            create_backup(
                self.root / "missing.db",
                self.backups,
                now="20260812T120000Z",
                keep=7,
            )

        self.assertEqual(list(self.backups.glob("*")), [])


if __name__ == "__main__":
    unittest.main()
