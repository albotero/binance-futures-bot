"""Offline restore and online SQLite backup tools; never starts trading or services."""

import argparse
import json
import os
import re
import sqlite3
import stat
import uuid
from collections.abc import Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path


class OperationsError(ValueError):
    """A failed safety precondition or database verification."""


@dataclass(frozen=True)
class Verification:
    schema_revisions: tuple[str, ...]
    table_count: int


def _path(value: str | Path, *, existing: bool) -> Path:
    raw = str(value)
    if not raw.strip() or "\x00" in raw or "://" in raw or raw.startswith("file:"):
        raise OperationsError("use a filesystem path, not a database URL or URI")
    path = Path(raw).expanduser()
    # Resolve parents, but never follow a final-component symlink.
    parent = path.parent.resolve(strict=True)
    path = parent / path.name
    try:
        info = path.lstat()
    except FileNotFoundError:
        if existing:
            raise OperationsError("input database does not exist") from None
    else:
        if not stat.S_ISREG(info.st_mode):
            raise OperationsError("database paths must be regular files, not symlinks")
    return path


def _sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(f"{path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))


def _no_sidecars(path: Path) -> None:
    if any(item.exists() or item.is_symlink() for item in _sidecars(path)):
        raise OperationsError(
            "destination has SQLite sidecars; stop all database users and let SQLite "
            "checkpoint/close cleanly before retrying; never manually delete a WAL"
        )


def _secure_parent(path: Path) -> None:
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise OperationsError(
            "destination directory must be owned by the invoking user and not group/world-writable"
        )


def _identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    # stat_result tuple equality omits the nanosecond timestamp fields.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _distinct(source: Path, destination: Path) -> None:
    if (
        source == destination
        or source in _sidecars(destination)
        or destination in _sidecars(source)
        or (destination.exists() and source.samefile(destination))
    ):
        raise OperationsError("source and destination must be distinct, unrelated database files")


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=15)


def _verify(
    connection: sqlite3.Connection, expected_revisions: Sequence[str] | None = None
) -> Verification:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise OperationsError("SQLite integrity_check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise OperationsError("SQLite foreign_key_check failed")
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
    ).fetchone():
        raise OperationsError("database has no Alembic schema revision metadata")
    revisions = tuple(
        row[0] for row in connection.execute("SELECT version_num FROM alembic_version")
    )
    if (
        not revisions
        or any(
            not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", revision)
            for revision in revisions
        )
        or len(set(revisions)) != len(revisions)
    ):
        raise OperationsError("invalid Alembic schema revision metadata")
    if expected_revisions is not None and set(revisions) != set(expected_revisions):
        raise OperationsError("database schema revisions do not match the expected revisions")
    tables = connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[
        0
    ]
    return Verification(tuple(sorted(revisions)), tables)


def verify_database(
    database: str | Path, *, expected_revisions: Sequence[str] | None = None
) -> Verification:
    """Check a consistent read snapshot without migrating or modifying its schema."""
    path = _path(database, existing=True)
    with closing(_connect(path)) as connection:
        connection.execute("BEGIN")
        return _verify(connection, expected_revisions)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _staging_file(destination: Path):
    path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.pending")
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        yield path
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        for item in (path, *_sidecars(path)):
            try:
                item.unlink()
            except FileNotFoundError:
                pass


def _copy_snapshot(
    source: Path, destination: Path, expected_revisions: Sequence[str] | None
) -> Verification:
    with closing(_connect(source)) as original, closing(sqlite3.connect(destination)) as copy:
        original.execute("BEGIN")
        verified = _verify(original, expected_revisions)
        original.backup(copy)
        # Produce a standalone backup, never one that depends on a sibling WAL.
        if copy.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise OperationsError("could not make a standalone SQLite snapshot")
        copied = _verify(copy, verified.schema_revisions)
        if copied != verified:
            raise OperationsError("snapshot metadata does not match the source")
    descriptor = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return copied


def backup_database(
    database: str | Path,
    output: str | Path,
    *,
    expected_revisions: Sequence[str] | None = None,
) -> Verification:
    """Publish a verified, owner-only snapshot atomically, without overwriting anything."""
    source = _path(database, existing=True)
    destination = _path(output, existing=False)
    _distinct(source, destination)
    _secure_parent(destination)
    if destination.exists():
        raise FileExistsError("backup destination already exists")
    _no_sidecars(destination)
    with _staging_file(destination) as staging:
        result = _copy_snapshot(source, staging, expected_revisions)
        _no_sidecars(destination)
        # A hard link is an atomic no-replace publication on the same filesystem.
        os.link(staging, destination, follow_symlinks=False)
        _sync_directory(destination.parent)
    _sync_directory(destination.parent)
    return result


def restore_database(
    database: str | Path,
    backup: str | Path,
    *,
    service_stopped: bool = False,
    confirm_restore: bool = False,
    expected_revisions: Sequence[str] | None = None,
) -> Verification:
    """Replace a stopped database only after validating a complete staged snapshot.

    Both confirmations are mandatory even for a new destination. They are operator
    assertions: this tool cannot prove that remote processes have closed SQLite.
    Existing destination WAL/SHM/journal files are refused rather than discarded.
    """
    if not service_stopped or not confirm_restore:
        raise OperationsError("restore requires --service-stopped and --confirm-restore")
    source = _path(backup, existing=True)
    destination = _path(database, existing=False)
    _distinct(source, destination)
    _secure_parent(destination)
    _no_sidecars(destination)
    original = _identity(destination)
    with _staging_file(destination) as staging:
        result = _copy_snapshot(source, staging, expected_revisions)
        checked = _path(destination, existing=False)
        current = _identity(checked)
        if current != original:
            raise OperationsError("destination changed while preparing the restore")
        _no_sidecars(destination)
        if original is None:
            os.link(staging, destination, follow_symlinks=False)
        else:
            os.replace(staging, destination)
        _sync_directory(destination.parent)
    _sync_directory(destination.parent)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "verify", "restore"):
        command = subcommands.add_parser(name)
        command.add_argument("--database", required=True, help="SQLite filesystem path, not URL")
        command.add_argument(
            "--expected-revision",
            action="append",
            help="exact expected Alembic head; repeat for branches",
        )
        if name == "backup":
            command.add_argument("--output", required=True)
        elif name == "restore":
            command.add_argument("--backup", required=True)
            command.add_argument("--service-stopped", action="store_true")
            command.add_argument("--confirm-restore", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            result = backup_database(
                args.database, args.output, expected_revisions=args.expected_revision
            )
        elif args.command == "verify":
            result = verify_database(args.database, expected_revisions=args.expected_revision)
        else:
            result = restore_database(
                args.database,
                args.backup,
                service_stopped=args.service_stopped,
                confirm_restore=args.confirm_restore,
                expected_revisions=args.expected_revision,
            )
    except OperationsError as exc:
        parser.exit(1, f"Operation refused: {exc}\n")
    except (OSError, sqlite3.Error) as exc:
        # Exception strings may contain user-supplied paths or connection details.
        parser.exit(1, f"Operation failed ({type(exc).__name__}); no success was reported.\n")
    print(
        json.dumps({"schema_revisions": result.schema_revisions, "table_count": result.table_count})
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
