"""``manage.py restore_db --file PATH --yes`` (SQLite only, API_CONTRACT_V3 §16.2).

Makes a safety copy of the current database first (``nnp-before-restore-*.sqlite3``), then replaces the
database content via the SQLite backup API. Agents never run this against the real database.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from core import backup


class Command(BaseCommand):
    help = "SQLite bazani zaxira nusxadan tiklaydi (avval joriy bazaning nusxasini oladi)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--file", type=Path, required=True, help="Tiklanadigan .sqlite3 nusxa")
        parser.add_argument("--yes", action="store_true", help="Tasdiqlash (busiz ishlamaydi)")
        parser.add_argument("--output-dir", type=Path, default=None, help="Xavfsizlik nusxasi papkasi")
        parser.add_argument("--database-path", type=Path, default=None, help="Boshqa SQLite fayl (testlar uchun)")

    def handle(self, *args, **options) -> None:
        source: Path = options["file"]
        if options["database_path"] is not None:
            info = backup.DbInfo("sqlite", path=options["database_path"])
        else:
            try:
                info = backup.db_info()
            except backup.BackupError as exc:
                raise CommandError(str(exc)) from None
        if info.engine != "sqlite":
            raise CommandError(f"PostgreSQL uchun qo‘lda bajaring: pg_restore --clean -d <baza> {source}")
        if not options["yes"]:
            raise CommandError("Tiklash joriy bazani almashtiradi. Davom etish uchun --yes qo‘shing.")
        if not source.exists():
            raise CommandError(f"Fayl topilmadi: {source}")
        if not backup.integrity_ok(source) or not backup.has_migrations_table(source):
            raise CommandError("Fayl yaroqli NNP SQLite zaxira nusxasi emas.")
        if info.path.exists() and source.resolve() == info.path.resolve():
            raise CommandError("Manba va joriy baza bir xil fayl.")
        output_dir: Path = options["output_dir"] or backup.default_output_dir()
        try:
            safety = None
            if info.path.exists():
                safety = backup.backup_sqlite(info.path, output_dir, prefix=backup.RESTORE_PREFIX)
                self.stdout.write(f"Joriy baza nusxasi: {safety}")
            if options["database_path"] is None:
                connections.close_all()
            backup.sqlite_copy(source, info.path)
        except backup.BackupError as exc:
            raise CommandError(str(exc)) from None
        if not backup.integrity_ok(info.path):
            raise CommandError(f"Tiklangan baza tekshiruvdan o‘tmadi. Nusxa: {safety}")
        self.stdout.write("Baza tiklandi. Serverni qayta ishga tushiring.")
