"""Refuse unpinned or URL-based runtime dependencies before installing a release."""

import re
import sys
import tomllib
from pathlib import Path


def main() -> None:
    with Path(sys.argv[1]).open("rb") as source:
        metadata = tomllib.load(source)
    dependencies = metadata["project"]["dependencies"]
    if not dependencies or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.!+-]*", item)
        for item in dependencies
    ):
        raise SystemExit("Runtime dependencies must be exact public package pins, not URLs.")


if __name__ == "__main__":
    main()
