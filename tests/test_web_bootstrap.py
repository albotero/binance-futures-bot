import os
import sys

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from futures_bot.persistence.database import make_session_factory
from futures_bot.persistence.models import Base
from futures_bot.web.auth import verify_password
from futures_bot.web.bootstrap import main, read_password_file
from futures_bot.web.models import WebOwner

PASSWORD = "test-only owner passphrase 123!"


def test_password_file_requires_private_regular_owner_file(tmp_path):
    source = tmp_path / "password"
    source.write_text(PASSWORD + "\n")
    source.chmod(0o600)
    assert read_password_file(source) == PASSWORD
    source.chmod(0o644)
    with pytest.raises(ValueError):
        read_password_file(source)
    source.chmod(0o600)
    symlink = tmp_path / "link"
    symlink.symlink_to(source)
    with pytest.raises(OSError):
        read_password_file(symlink)
    hardlink = tmp_path / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(ValueError):
        read_password_file(source)
    hardlink.unlink()
    source.write_text(PASSWORD + "\nsecond-line")
    with pytest.raises(ValueError):
        read_password_file(source)
    source.write_bytes(b"x" * 4097)
    with pytest.raises(ValueError):
        read_password_file(source)
    with pytest.raises(ValueError):
        read_password_file(tmp_path)


def test_bootstrap_password_file_without_prompt_or_secret_output(tmp_path, monkeypatch, capsys):
    source = tmp_path / "password"
    source.write_text(PASSWORD + "\n")
    source.chmod(0o600)
    database = tmp_path / "bootstrap.sqlite3"
    url = f"sqlite:///{database}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        sys,
        "argv",
        ["bootstrap", "--database-url", url, "--username", "owner", "--password-file", str(source)],
    )

    def no_prompt(*args):
        pytest.fail("noninteractive password file unexpectedly prompted")

    monkeypatch.setattr("futures_bot.web.bootstrap.getpass.getpass", no_prompt)
    main()
    with make_session_factory(engine)() as session:
        assert verify_password(PASSWORD, session.get(WebOwner, 1).password_hash)
    assert capsys.readouterr() == ("", "")
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert PASSWORD not in capsys.readouterr().err
    engine.dispose()


def test_auth_migration_metadata_and_downgrade_roundtrip(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setenv("BOT_DATABASE_URL", url)
    configuration = Config("alembic.ini")
    command.upgrade(configuration, "20261016_0005")
    command.upgrade(configuration, "20261016_0006")
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        assert {"web_owners", "web_sessions", "web_login_throttles"} <= set(
            inspector.get_table_names()
        )
        assert inspector.get_foreign_keys("web_sessions")[0]["referred_table"] == "web_owners"
        assert inspector.get_unique_constraints("web_owners")[0]["column_names"] == ["username"]
        assert inspector.get_indexes("web_sessions")[0]["name"] == "ix_web_sessions_expires_at"
        assert inspector.get_indexes("web_login_throttles")[0]["name"] == (
            "ix_web_login_throttles_reset_at"
        )
        command.check(configuration)
        command.downgrade(configuration, "20261016_0005")
        assert "web_owners" not in inspect(engine).get_table_names()
        command.upgrade(configuration, "20261016_0006")
        command.check(configuration)
    finally:
        engine.dispose()
