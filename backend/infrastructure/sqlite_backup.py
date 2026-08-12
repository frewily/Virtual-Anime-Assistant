"""Create consistent, rotated backups of the assistant SQLite database."""

import argparse
import os
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from infrastructure.database_config import DatabaseSettings


def create_backup(
    source: Path,
    backup_dir: Path,
    *,
    now: str,
    keep: int = 7,
) -> Path:
    source = Path(source)
    backup_dir = Path(backup_dir)
    if keep <= 0:
        raise ValueError("keep must be positive")
    if not source.is_file():
        raise FileNotFoundError(source)

    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"assistant-{now}.db"
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".assistant-backup-",
            suffix=".db",
            dir=backup_dir,
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        with (
            sqlite3.connect(source) as source_connection,
            sqlite3.connect(temporary_path) as backup_connection,
        ):
            source_connection.backup(backup_connection)
            result = backup_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            if result != ("ok",):
                raise RuntimeError("backup integrity check failed")
        os.replace(temporary_path, destination)
        temporary_path = None

        backups = sorted(backup_dir.glob("assistant-*.db"))
        for stale_backup in backups[:-keep]:
            stale_backup.unlink()
        return destination
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _utc_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("/data/backups"))
    parser.add_argument("--keep", type=int, default=7)
    arguments = parser.parse_args()
    source = arguments.source or DatabaseSettings.from_env().database_path
    create_backup(
        source,
        arguments.output,
        now=_utc_timestamp(),
        keep=arguments.keep,
    )


if __name__ == "__main__":
    main()
