"""``backup_db`` / ``restore_db`` on temporary SQLite files only (API_CONTRACT_V3 §16.2)."""

from __future__ import annotations

import io
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from core import backup


def make_db(path: Path, value: str) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE django_migrations (id INTEGER PRIMARY KEY, app TEXT, name TEXT, applied TEXT)")
    con.execute("CREATE TABLE demo (value TEXT)")
    con.execute("INSERT INTO demo VALUES (?)", (value,))
    con.commit()
    con.close()


def read_value(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT value FROM demo").fetchone()[0]
    finally:
        con.close()


class BackupRestoreTests(SimpleTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nnp-backup-test-"))
        self.db = self.tmp / "live.sqlite3"
        self.out = self.tmp / "backups"
        make_db(self.db, "original")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_backup(self, *extra: str) -> str:
        out = io.StringIO()
        call_command("backup_db", "--database-path", str(self.db), "--output-dir", str(self.out), *extra, stdout=out)
        return out.getvalue()

    def test_backup_creates_valid_copy(self):
        output = self.run_backup()
        copies = list(self.out.glob("nnp-*.sqlite3"))
        self.assertEqual(len(copies), 1)
        self.assertRegex(copies[0].name, r"^nnp-\d{8}-\d{6}\.sqlite3$")
        self.assertTrue(backup.integrity_ok(copies[0]))
        self.assertEqual(read_value(copies[0]), "original")
        self.assertIn("Zaxira nusxa:", output)

    def test_keep_rotation_only_touches_own_files(self):
        foreign = self.out / "nnp-notes.sqlite3"
        self.out.mkdir()
        foreign.write_bytes(b"x")
        for _ in range(3):
            self.run_backup("--keep", "2")
            time.sleep(0.02)
        own = [f for f in self.out.glob("nnp-*.sqlite3") if f != foreign]
        self.assertEqual(len(own), 2)
        self.assertTrue(foreign.exists())

    def test_with_media_writes_zip(self):
        media = self.tmp / "media"
        (media / "uploads").mkdir(parents=True)
        (media / "uploads" / "a.jpg").write_bytes(b"\xff\xd8\xff")
        with self.settings(MEDIA_ROOT=media, FILE_STORAGE="local"):
            self.run_backup("--with-media")
        self.assertEqual(len(list(self.out.glob("nnp-media-*.zip"))), 1)

    def test_restore_requires_yes(self):
        self.run_backup()
        copy = next(self.out.glob("nnp-*.sqlite3"))
        with self.assertRaisesMessage(CommandError, "--yes"):
            call_command("restore_db", "--file", str(copy), "--database-path", str(self.db), stdout=io.StringIO())

    def test_restore_round_trip_with_safety_copy(self):
        self.run_backup()
        copy = next(self.out.glob("nnp-*.sqlite3"))
        con = sqlite3.connect(self.db)
        con.execute("UPDATE demo SET value = 'changed'")
        con.commit()
        con.close()
        out = io.StringIO()
        call_command("restore_db", "--file", str(copy), "--database-path", str(self.db), "--output-dir",
                     str(self.out), "--yes", stdout=out)
        self.assertEqual(read_value(self.db), "original")
        safety = list(self.out.glob("nnp-before-restore-*.sqlite3"))
        self.assertEqual(len(safety), 1)
        self.assertEqual(read_value(safety[0]), "changed")
        self.assertIn("Serverni qayta ishga tushiring.", out.getvalue())

    def test_restore_rejects_invalid_file(self):
        bad = self.tmp / "bad.sqlite3"
        bad.write_bytes(b"not a database" * 100)
        with self.assertRaisesMessage(CommandError, "yaroqli"):
            call_command("restore_db", "--file", str(bad), "--database-path", str(self.db), "--yes",
                         stdout=io.StringIO())
        no_migrations = self.tmp / "plain.sqlite3"
        sqlite3.connect(no_migrations).close()
        with self.assertRaises(CommandError):
            call_command("restore_db", "--file", str(no_migrations), "--database-path", str(self.db), "--yes",
                         stdout=io.StringIO())
        self.assertEqual(read_value(self.db), "original")

