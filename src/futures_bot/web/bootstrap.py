"""Interactive, one-time owner creation; run after database migrations."""

import argparse
import getpass
import os
import stat
from pathlib import Path

from futures_bot.persistence.database import make_engine, make_session_factory
from futures_bot.web.auth import bootstrap_owner


def read_password_file(path: Path) -> str:
    """Read only an owner-private regular file, without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
        ):
            raise ValueError("password file must be a singly linked, owner-private regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            content = source.read(4097)
        if len(content) > 4096:
            raise ValueError("password file exceeds the supported size")
        password = content.decode("utf-8")
        if password.endswith("\n"):
            password = password[:-1]
            if password.endswith("\r"):
                password = password[:-1]
        if "\n" in password or "\r" in password:
            raise ValueError("password file must contain a single password line")
        return password
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--password-file",
        type=Path,
        help="owner-private regular UTF-8 file; optional final newline is removed",
    )
    args = parser.parse_args()
    if args.password_file is not None:
        try:
            password = read_password_file(args.password_file)
        except (OSError, UnicodeError, ValueError):
            parser.exit(1, "Password file could not be securely read.\n")
    else:
        password = getpass.getpass("New owner password (minimum 14 characters): ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            parser.exit(1, "Passwords do not match.\n")
    engine = make_engine(args.database_url)
    try:
        bootstrap_owner(make_session_factory(engine), args.username, password)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
