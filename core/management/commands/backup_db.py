"""``manage.py backup_db [--output-dir DIR] [--keep N] [--with-media]`` (API_CONTRACT_V3 §16.2)."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import backup


class Command(BaseCommand):
    help = "Bazaning izchil zaxira nusxasini oladi (SQLite backup API yoki pg_dump)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--output-dir", type=Path, default=None, help="Standart: BASE_DIR/backups")
        parser.add_argument("--keep", type=int, default=14, help="Saqlanadigan nusxalar soni (standart 14)")
        parser.add_argument("--with-media", action="store_true", help="MEDIA_ROOT ni ham zip qiladi")
        parser.add_argument("--database-path", type=Path, default=None, help="Boshqa SQLite fayl (testlar uchun)")

    def handle(self, *args, **options) -> None:
        output_dir: Path = options["output_dir"] or backup.default_output_dir()
        keep: int = options["keep"]
        if keep < 1:
            raise CommandError("--keep kamida 1 bo‘lishi kerak.")
        try:
            if options["database_path"] is not None:
                info = backup.DbInfo("sqlite", path=options["database_path"])
            else:
                info = backup.db_info()
            if info.engine == "sqlite":
                target = backup.backup_sqlite(info.path, output_dir)
            else:
                target = backup.backup_postgres(info.config, output_dir)
            self.stdout.write(f"Zaxira nusxa: {target} ({backup.human_size(target)})")
            if options["with_media"]:
                media = backup.backup_media(output_dir)
                if media is not None:
                    self.stdout.write(f"Fayllar arxivi: {media} ({backup.human_size(media)})")
                else:
                    self.stdout.write("Fayllar S3 da saqlanadi — media arxivi olinmadi.")
            removed = backup.rotate(output_dir, keep)
            if removed:
                self.stdout.write(f"Eski nusxalar o‘chirildi: {len(removed)}")
        except backup.BackupError as exc:
            raise CommandError(str(exc)) from None
