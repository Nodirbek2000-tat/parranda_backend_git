"""Online SQLite backup / restore helpers (API_CONTRACT_V3 §16.2), used by ``backup_db`` and ``restore_db``.

``sqlite3.Connection.backup()`` copies a consistent snapshot while the server keeps running (a raw file copy
can catch a half-written page). Nothing here prints settings or secrets.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.utils import timezone

PREFIX = "nnp-"
RESTORE_PREFIX = "nnp-before-restore-"
_OWN_FILE = re.compile(r"^nnp-(media-)?\d{8}-\d{6}(-\d+)?\.(sqlite3|zip|dump)$")


class BackupError(Exception):
    """Human readable (Uzbek) failure; the command prints it and exits 1."""


@dataclass
class DbInfo:
    engine: str  # "sqlite" | "postgresql"
    path: Path | None = None  # SQLite file
    config: dict | None = None  # PostgreSQL settings


def db_info(alias: str = "default") -> DbInfo:
    config = settings.DATABASES[alias]
    engine = config["ENGINE"]
    if engine.endswith("sqlite3"):
        return DbInfo("sqlite", path=Path(str(config["NAME"])))
    if "postgresql" in engine:
        return DbInfo("postgresql", config=config)
    raise BackupError(f"Qo‘llab-quvvatlanmaydigan baza: {engine}")


def default_output_dir() -> Path:
    return Path(settings.BASE_DIR) / "backups"


def stamp() -> str:
    return timezone.localtime().strftime("%Y%m%d-%H%M%S")


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise BackupError("Fayl nomini tanlab bo‘lmadi.")


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=30)


def integrity_ok(path: Path) -> bool:
    try:
        con = _connect_ro(path)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("PRAGMA integrity_check").fetchone()
        return bool(row) and row[0] == "ok"
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def has_migrations_table(path: Path) -> bool:
    try:
        con = _connect_ro(path)
    except sqlite3.Error:
        return False
    try:
        row = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='django_migrations'").fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def sqlite_copy(source: Path, target: Path) -> None:
    """Consistent online copy ``source`` → ``target`` with the SQLite backup API."""
    if not source.exists():
        raise BackupError(f"Baza fayli topilmadi: {source}")
    src = _connect_ro(source)
    dst = sqlite3.connect(str(target), timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def backup_sqlite(source: Path, output_dir: Path, *, prefix: str = PREFIX) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = _unique(output_dir / f"{prefix}{stamp()}.sqlite3")
    sqlite_copy(source, target)
    if not integrity_ok(target):
        target.unlink(missing_ok=True)
        raise BackupError("Zaxira nusxa butunlik tekshiruvidan o‘tmadi (integrity_check), fayl o‘chirildi.")
    return target


def backup_media(output_dir: Path) -> Path | None:
    """Zip of ``MEDIA_ROOT`` (local storage only; ``None`` for S3)."""
    if getattr(settings, "FILE_STORAGE", "local") != "local":
        return None
    root = Path(settings.MEDIA_ROOT)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = _unique(output_dir / f"{PREFIX}media-{stamp()}.zip")
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if root.exists():
            for file in sorted(root.rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(root).as_posix())
    return target


def rotate(output_dir: Path, keep: int) -> list[Path]:
    """Keep the newest ``keep`` database copies and media archives created by ``backup_db`` (others untouched)."""
    removed: list[Path] = []
    groups: dict[str, list[Path]] = {}
    for file in output_dir.glob(f"{PREFIX}*"):
        match = _OWN_FILE.match(file.name)
        if match is None:
            continue
        groups.setdefault(f"{match.group(1) or ''}{match.group(3)}", []).append(file)
    for files in groups.values():
        for old in sorted(files, key=lambda f: (f.stat().st_mtime_ns, f.name), reverse=True)[keep:]:
            old.unlink(missing_ok=True)
            removed.append(old)
    return removed


def backup_postgres(config: dict, output_dir: Path) -> Path:
    exe = shutil.which("pg_dump")
    if exe is None:
        raise BackupError("pg_dump topilmadi. Qo‘lda bajaring: pg_dump -Fc -f nnp.dump <baza>")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = _unique(output_dir / f"{PREFIX}{stamp()}.dump")
    env = dict(os.environ)
    if config.get("PASSWORD"):
        env["PGPASSWORD"] = config["PASSWORD"]  # never on the command line or in the output
    args = [exe, "-Fc", "-f", str(target), "-d", config["NAME"]]
    if config.get("HOST"):
        args += ["-h", config["HOST"]]
    if config.get("PORT"):
        args += ["-p", str(config["PORT"])]
    if config.get("USER"):
        args += ["-U", config["USER"]]
    result = subprocess.run(args, env=env, capture_output=True, check=False)  # noqa: S603
    if result.returncode != 0:
        target.unlink(missing_ok=True)
        raise BackupError("pg_dump xato bilan tugadi.")
    return target


def human_size(path: Path) -> str:
    size = float(path.stat().st_size)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
