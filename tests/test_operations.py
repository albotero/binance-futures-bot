import json
import os
import sqlite3
import stat
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from futures_bot import operations
from futures_bot.operations import (
    OperationsError,
    backup_database,
    main,
    restore_database,
    verify_database,
)


def make_database(path, *, wal=False, revision="test_head", value="original"):
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.executescript(
        """
        CREATE TABLE alembic_version (version_num TEXT NOT NULL PRIMARY KEY);
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE fills (
            id INTEGER PRIMARY KEY, account_id INTEGER REFERENCES accounts(id), value TEXT
        );
        CREATE INDEX fills_account ON fills(account_id);
        CREATE VIEW fill_values AS SELECT value FROM fills;
        CREATE TRIGGER rename_account AFTER UPDATE OF name ON accounts
            BEGIN UPDATE fills SET value = NEW.name WHERE account_id = NEW.id; END;
        """
    )
    connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
    connection.execute("INSERT INTO accounts VALUES (1, 'paper')")
    connection.execute("INSERT INTO fills VALUES (1, 1, ?)", (value,))
    connection.execute("PRAGMA user_version=17")
    connection.commit()
    return connection


def restore(target, backup, **kwargs):
    return restore_database(target, backup, service_stopped=True, confirm_restore=True, **kwargs)


def test_online_wal_snapshot_includes_all_tables_schema_and_committed_rows(tmp_path):
    source = tmp_path / "platform.sqlite3"
    output = tmp_path / "snapshot.sqlite3"
    with closing(make_database(source, wal=True)) as live:
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        live.execute("INSERT INTO fills VALUES (2, 1, 'committed WAL row')")
        live.commit()
        result = backup_database(source, output, expected_revisions=["test_head"])
        assert result.schema_revisions == ("test_head",)
        assert result.table_count == 3
        assert live.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        with closing(sqlite3.connect(output)) as snapshot:
            assert snapshot.execute("SELECT value FROM fills ORDER BY id").fetchall() == [
                ("original",),
                ("committed WAL row",),
            ]
            assert snapshot.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            assert snapshot.execute("PRAGMA user_version").fetchone() == (17,)
            assert (
                snapshot.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
                == live.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            )
        assert not any(item.exists() for item in operations._sidecars(output))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.pending"))


def test_backup_does_not_copy_uncommitted_rows(tmp_path):
    source, output = tmp_path / "db", tmp_path / "backup"
    with closing(make_database(source, wal=True)) as writer:
        writer.execute("INSERT INTO fills VALUES (2, 1, 'not committed')")
        backup_database(source, output)
    with closing(sqlite3.connect(output)) as snapshot:
        assert snapshot.execute("SELECT count(*) FROM fills").fetchone() == (1,)


def test_backup_never_overwrites_output(tmp_path):
    source, output = tmp_path / "db", tmp_path / "backup"
    make_database(source).close()
    output.write_bytes(b"keep this")
    with pytest.raises(FileExistsError):
        backup_database(source, output)
    assert output.read_bytes() == b"keep this"


def test_publish_race_does_not_overwrite_output(tmp_path, monkeypatch):
    source, output = tmp_path / "db", tmp_path / "backup"
    make_database(source).close()
    actual_copy = operations._copy_snapshot

    def race(*args):
        result = actual_copy(*args)
        output.write_bytes(b"concurrent winner")
        return result

    monkeypatch.setattr(operations, "_copy_snapshot", race)
    with pytest.raises(FileExistsError):
        backup_database(source, output)
    assert output.read_bytes() == b"concurrent winner"
    assert not list(tmp_path.glob(".*.pending"))


@pytest.mark.parametrize("operation", ["verify", "backup", "restore"])
def test_corruption_is_rejected_and_original_files_are_preserved(tmp_path, operation):
    broken, target = tmp_path / "broken", tmp_path / "target"
    broken.write_bytes(b"this is not a SQLite database")
    make_database(target).close()
    before = target.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        if operation == "verify":
            verify_database(broken)
        elif operation == "backup":
            backup_database(broken, tmp_path / "output")
        else:
            restore(target, broken)
    assert target.read_bytes() == before
    assert broken.read_bytes() == b"this is not a SQLite database"
    assert not (tmp_path / "output").exists()
    assert not list(tmp_path.glob(".*.pending"))


def test_foreign_key_violation_is_rejected(tmp_path):
    source, output = tmp_path / "db", tmp_path / "backup"
    with closing(make_database(source)) as connection:
        connection.execute("INSERT INTO fills VALUES (2, 99, 'orphan')")
        connection.commit()
    for action in (lambda: verify_database(source), lambda: backup_database(source, output)):
        with pytest.raises(OperationsError, match="foreign_key_check"):
            action()
    assert not output.exists()


def test_foreign_key_failure_during_restore_preserves_both_inputs(tmp_path):
    source, target = tmp_path / "broken_backup", tmp_path / "target"
    with closing(make_database(source)) as connection:
        connection.execute("INSERT INTO fills VALUES (2, 99, 'orphan')")
        connection.commit()
    make_database(target, value="keep").close()
    source_before, target_before = source.read_bytes(), target.read_bytes()
    with pytest.raises(OperationsError, match="foreign_key_check"):
        restore(target, source)
    assert source.read_bytes() == source_before
    assert target.read_bytes() == target_before


def test_truncated_sqlite_database_is_rejected(tmp_path):
    source, output = tmp_path / "db", tmp_path / "backup"
    make_database(source).close()
    source.write_bytes(source.read_bytes()[:200])
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, output)
    assert not output.exists()


@pytest.mark.parametrize("revision", ["", "invalid revision", None])
def test_missing_or_invalid_schema_metadata_is_rejected(tmp_path, revision):
    source = tmp_path / "db"
    with closing(make_database(source)) as connection:
        connection.execute("DELETE FROM alembic_version")
        if revision is not None:
            connection.execute("INSERT INTO alembic_version VALUES (?)", (revision,))
        connection.commit()
    with pytest.raises(OperationsError, match="revision metadata"):
        verify_database(source)


def test_unversioned_database_is_rejected(tmp_path):
    source = tmp_path / "db"
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE arbitrary (id INTEGER)")
    with pytest.raises(OperationsError, match="no Alembic"):
        verify_database(source)


def test_expected_schema_revision_is_enforced_for_backup_and_restore(tmp_path):
    source, target = tmp_path / "db", tmp_path / "target"
    make_database(source).close()
    make_database(target, value="keep").close()
    before = target.read_bytes()
    with pytest.raises(OperationsError, match="expected revisions"):
        backup_database(source, tmp_path / "output", expected_revisions=["wrong_head"])
    with pytest.raises(OperationsError, match="expected revisions"):
        restore(target, source, expected_revisions=["wrong_head"])
    assert target.read_bytes() == before
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("stopped,confirmed", [(False, False), (True, False), (False, True)])
def test_restore_requires_both_explicit_confirmations(tmp_path, stopped, confirmed):
    source, target = tmp_path / "db", tmp_path / "target"
    make_database(source).close()
    with pytest.raises(OperationsError, match="requires --service-stopped"):
        restore_database(target, source, service_stopped=stopped, confirm_restore=confirmed)
    assert not target.exists()


@pytest.mark.parametrize("existing", [True, False])
def test_restore_publishes_valid_owner_only_standalone_database(tmp_path, existing):
    source, target = tmp_path / "db", tmp_path / "target"
    make_database(source, value="restore this").close()
    original_backup = source.read_bytes()
    if existing:
        make_database(target, value="replace this").close()
        target.chmod(0o644)
    assert restore(target, source).schema_revisions == ("test_head",)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT value FROM fills").fetchone() == ("restore this",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert source.read_bytes() == original_backup
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_restore_refuses_destination_sidecars_without_deleting_them(tmp_path, suffix):
    source, target = tmp_path / "db", tmp_path / "target"
    make_database(source).close()
    make_database(target).close()
    before = target.read_bytes()
    sidecar = target.with_name(target.name + suffix)
    sidecar.write_bytes(b"must not discard")
    with pytest.raises(OperationsError, match="sidecars"):
        restore(target, source)
    assert target.read_bytes() == before
    assert sidecar.read_bytes() == b"must not discard"


def test_restore_rejects_active_wal_destination(tmp_path):
    source, target = tmp_path / "backup", tmp_path / "live"
    make_database(source).close()
    with closing(make_database(target, wal=True)) as live:
        with pytest.raises(OperationsError, match="sidecars"):
            restore(target, source)
        assert live.execute("SELECT value FROM fills").fetchone() == ("original",)


def test_staging_failure_preserves_destination(tmp_path, monkeypatch):
    source, target = tmp_path / "db", tmp_path / "target"
    make_database(source).close()
    make_database(target, value="keep").close()
    before = target.read_bytes()

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(operations, "_copy_snapshot", fail)
    with pytest.raises(OSError, match="disk full"):
        restore(target, source)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".*.pending"))


def test_restore_refuses_destination_changed_while_staging(tmp_path, monkeypatch):
    source, target = tmp_path / "db", tmp_path / "target"
    make_database(source).close()
    make_database(target).close()
    actual_copy = operations._copy_snapshot

    def race(*args):
        result = actual_copy(*args)
        with closing(sqlite3.connect(target)) as concurrent:
            concurrent.execute("UPDATE fills SET value='concurrent write'")
            concurrent.commit()
        return result

    monkeypatch.setattr(operations, "_copy_snapshot", race)
    with pytest.raises(OperationsError, match="destination changed"):
        restore(target, source)
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("SELECT value FROM fills").fetchone() == ("concurrent write",)


@pytest.mark.parametrize("mode", [0o777, 0o770])
def test_output_directory_must_not_be_group_or_world_writable(tmp_path, mode):
    source, output_dir = tmp_path / "db", tmp_path / "outputs"
    make_database(source).close()
    output_dir.mkdir()
    output_dir.chmod(mode)
    with pytest.raises(OperationsError, match="destination directory"):
        backup_database(source, output_dir / "backup")
    with pytest.raises(OperationsError, match="destination directory"):
        restore(output_dir / "restored", source)


@pytest.mark.parametrize("same_kind", ["same_path", "hardlink", "sidecar"])
def test_source_destination_aliases_are_rejected(tmp_path, same_kind):
    source = tmp_path / "db"
    make_database(source).close()
    if same_kind == "same_path":
        target = source
    elif same_kind == "hardlink":
        target = tmp_path / "alias"
        os.link(source, target)
    else:
        target = tmp_path / "db-wal"
    with pytest.raises(OperationsError, match="distinct"):
        backup_database(source, target)
    with pytest.raises(OperationsError, match="distinct"):
        restore(target, source)


def test_symlinks_directories_missing_parents_and_urls_are_rejected(tmp_path):
    source = tmp_path / "db"
    make_database(source).close()
    link = tmp_path / "link"
    link.symlink_to(source)
    for path in (link, tmp_path):
        with pytest.raises(OperationsError, match="regular files"):
            verify_database(path)
    with pytest.raises(OperationsError, match="regular files"):
        backup_database(source, link)
    with pytest.raises(FileNotFoundError):
        backup_database(source, tmp_path / "missing" / "output")
    for path in ("", "sqlite:///db", "postgresql://user:secret@host/db", "file:db"):
        with pytest.raises(OperationsError, match="filesystem path"):
            verify_database(path)


def test_cli_backup_verify_restore_and_redacted_failures(tmp_path, capsys):
    source, backup, target = tmp_path / "db", tmp_path / "backup", tmp_path / "restored"
    make_database(source).close()
    assert main(["backup", "--database", str(source), "--output", str(backup)]) == 0
    assert json.loads(capsys.readouterr().out)["schema_revisions"] == ["test_head"]
    assert main(["verify", "--database", str(backup), "--expected-revision", "test_head"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "restore",
                "--database",
                str(target),
                "--backup",
                str(backup),
                "--service-stopped",
                "--confirm-restore",
            ]
        )
        == 0
    )
    capsys.readouterr()
    with pytest.raises(SystemExit) as failure:
        main(["verify", "--database", "postgresql://user:secret@host/db"])
    assert failure.value.code == 1
    assert "secret" not in capsys.readouterr().err


def test_cli_redacts_filesystem_and_sqlite_exception_details(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("sensitive-input-or-connection-details")

    monkeypatch.setattr(operations, "verify_database", fail)
    with pytest.raises(SystemExit) as failure:
        main(["verify", "--database", "database"])
    assert failure.value.code == 1
    error = capsys.readouterr().err
    assert "OperationalError" in error
    assert "sensitive-input" not in error


@pytest.mark.parametrize(
    "dependency,accepted",
    [
        ("fastapi==0.143.2", True),
        ("fastapi>=0.143.2", False),
        ("package @ https://user:password@example.invalid/package.whl", False),
    ],
)
def test_deployment_dependency_validator(tmp_path, dependency, accepted):
    import sys

    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(f"[project]\ndependencies = [{json.dumps(dependency)}]\n")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "deploy/validate_dependencies.py"), str(manifest)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == accepted
    assert "password" not in result.stderr


def test_deployment_runtime_template_uses_actual_application_settings():
    root = Path(__file__).resolve().parents[1]
    template = (root / "deploy/runtime.env.in").read_text()
    rendered = template.replace("@LAN_NAME@", "binancebot.albotero.com").replace(
        "@APP_ROOT@", "/opt/futures-platform/releases/approved"
    )
    assert "BOT_WEB_" not in rendered
    assert "BOT_DASHBOARD_ENABLED=true" in rendered
    assert "BOT_PUBLIC_ORIGIN=https://binancebot.albotero.com" in rendered
    assert "BOT_SECURE_COOKIES=true" in rendered
    assert (
        "BOT_FRONTEND_DIRECTORY=/opt/futures-platform/releases/approved/frontend/dist" in rendered
    )
    assert "BOT_SESSION_LIFETIME_SECONDS=28800" in rendered
