#!/usr/bin/env python3
"""Operator-safe maintenance commands for the public game database."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Sequence

if __package__:
    from .game_store import GameStore, GameStoreError
else:
    from game_store import GameStore, GameStoreError


DEFAULT_DATABASE_PATH = "/var/lib/double-move/games.sqlite3"


def _database_path(argument: str | None) -> Path:
    raw_path = argument or os.environ.get(
        "DM_DATABASE_PATH", DEFAULT_DATABASE_PATH
    )
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise GameStoreError(
            f"database does not exist or is not a regular file: {path}"
        )
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check, back up, or clean the double-move public game database."
        )
    )
    parser.add_argument(
        "--database",
        metavar="PATH",
        help=(
            "database file (default: DM_DATABASE_PATH, then "
            f"{DEFAULT_DATABASE_PATH})"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "integrity",
        help="run SQLite's full integrity check without changing the database",
    )

    backup = commands.add_parser(
        "backup",
        help="create and verify a consistent backup at a new path",
    )
    backup.add_argument(
        "destination",
        metavar="PATH",
        help="new backup path; an existing file is never overwritten",
    )

    cleanup = commands.add_parser(
        "cleanup",
        help="apply the GameStore's configured expiry and retention policy",
    )
    cleanup.add_argument(
        "--confirm",
        action="store_true",
        help="required because cleanup can abandon or purge expired records",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        store = GameStore(_database_path(args.database))
        if args.command == "integrity":
            result = {"integrity_check": store.integrity_check()}
            exit_code = 0 if result["integrity_check"] == ["ok"] else 1
        elif args.command == "backup":
            result = store.backup(Path(args.destination).expanduser())
            exit_code = 0
        elif args.command == "cleanup":
            if not args.confirm:
                parser.error("cleanup requires --confirm")
            result = store.cleanup()
            exit_code = 0
        else:  # pragma: no cover - argparse enforces the command choices.
            parser.error(f"unknown command: {args.command}")
        print(json.dumps(result, sort_keys=True))
        return exit_code
    except (GameStoreError, OSError) as error:
        print(f"game database maintenance failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
