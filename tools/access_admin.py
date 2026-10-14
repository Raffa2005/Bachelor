#!/usr/bin/env python3
"""Local operator commands for invitation access."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

try:
    from .access_control import (
        AccessStore,
        AccessStoreError,
        DEFAULT_INVITATION_USES,
        InvitationNotFoundError,
        MODE_INVITE_ONLY,
        MODE_OPEN,
        default_access_database_path,
    )
except ImportError:
    from access_control import (
        AccessStore,
        AccessStoreError,
        DEFAULT_INVITATION_USES,
        InvitationNotFoundError,
        MODE_INVITE_ONLY,
        MODE_OPEN,
        default_access_database_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create, revoke, and purge invitation links or switch the site "
            "between open and invitation-only access."
        )
    )
    parser.add_argument(
        "--database",
        help=(
            "access database (default: DM_ACCESS_DATABASE_PATH, otherwise "
            "access.sqlite3 beside DM_DATABASE_PATH)"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show the current access mode and origin")
    commands.add_parser("open", help="allow everyone to use the site")
    commands.add_parser(
        "invite-only", help="require a valid invitation browser session"
    )

    create = commands.add_parser(
        "create", help="create an invitation and print its link once"
    )
    create.add_argument(
        "label",
        nargs="?",
        default="",
        help="private operator label, such as the recipient's name",
    )
    create.add_argument(
        "--uses",
        type=int,
        default=DEFAULT_INVITATION_USES,
        help=f"browser redemptions (default: {DEFAULT_INVITATION_USES})",
    )
    create.add_argument(
        "--origin",
        help="public site origin; saves it for future invitation links",
    )

    commands.add_parser("list", help="list invitations and their use counts")
    revoke = commands.add_parser(
        "revoke",
        help="revoke an invitation and every browser session it issued",
    )
    revoke.add_argument("invitation_id")
    commands.add_parser(
        "purge-revoked",
        help="permanently delete all revoked invitations and their records",
    )

    origin = commands.add_parser(
        "origin", help="show or save the public origin used in invitation links"
    )
    origin.add_argument("value", nargs="?")
    return parser


def _normalize_origin(raw: str) -> str:
    value = str(raw).strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "origin must contain only http(s), hostname, and optional port"
        )
    return value


def _tailscale_origin() -> str | None:
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        payload = json.loads(result.stdout)
        dns_name = str(payload.get("Self", {}).get("DNSName", "")).rstrip(".")
        if dns_name:
            return _normalize_origin("https://" + dns_name)
    except (FileNotFoundError, OSError, ValueError, subprocess.SubprocessError):
        pass
    return None


def _configured_environment_origin() -> str | None:
    for value in (os.environ.get("DM_PUBLIC_ORIGIN") or "").split(","):
        if value.strip():
            return _normalize_origin(value)
    return None


def _invitation_origin(
    store: AccessStore, explicit: str | None
) -> tuple[str, str]:
    if explicit:
        origin = _normalize_origin(explicit)
        store.set_public_origin(origin)
        return origin, "explicit"
    environment_origin = _configured_environment_origin()
    if environment_origin:
        store.set_public_origin(environment_origin)
        return environment_origin, "environment"
    saved_origin = store.get_public_origin()
    if saved_origin:
        return _normalize_origin(saved_origin), "saved"
    tailscale_origin = _tailscale_origin()
    if tailscale_origin:
        store.set_public_origin(tailscale_origin)
        return tailscale_origin, "Tailscale"
    port = os.environ.get("DM_PORT", "8082")
    return _normalize_origin(f"http://127.0.0.1:{port}"), "local fallback"


def _format_time(timestamp: int | None) -> str:
    if timestamp is None:
        return "—"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _run(args: argparse.Namespace) -> int:
    path = Path(args.database or default_access_database_path())
    store = AccessStore(path)
    if args.command == "status":
        print(
            "Mode: "
            + (
                "everyone can use"
                if store.get_mode() == MODE_OPEN
                else "only invitees can use"
            )
        )
        print(f"Database: {path.resolve()}")
        print(f"Invitation origin: {store.get_public_origin() or 'auto-detect'}")
        return 0

    if args.command == "open":
        store.set_mode(MODE_OPEN)
        print("Access is now open: everyone can use the site.")
        return 0

    if args.command == "invite-only":
        store.set_mode(MODE_INVITE_ONLY)
        print("Access is now invitation-only.")
        return 0

    if args.command == "origin":
        if args.value is None:
            saved = store.get_public_origin()
            if saved:
                print(saved)
            else:
                detected, source = _invitation_origin(store, None)
                print(f"{detected} ({source})")
            return 0
        saved = _normalize_origin(args.value)
        store.set_public_origin(saved)
        print(f"Invitation origin saved: {saved}")
        return 0

    if args.command == "create":
        origin, source = _invitation_origin(store, args.origin)
        invitation = store.create_invitation(
            args.label,
            max_redemptions=args.uses,
        )
        print(f"Invitation: {invitation.id}")
        if invitation.label:
            print(f"Label: {invitation.label}")
        print(f"Uses: {invitation.max_redemptions} browsers")
        print("Expires: never (until revoked)")
        print(f"Origin: {origin} ({source})")
        print()
        print(f"{origin}/invite#{invitation.token}")
        print()
        print(
            "This is the only time the secret link is shown. "
            f"Revoke it with: ./access.sh revoke {invitation.id}"
        )
        return 0

    if args.command == "list":
        invitations = store.list_invitations()
        if not invitations:
            print("No invitations.")
            return 0
        print("ID                USES  STATE     CREATED             LABEL")
        for invitation in invitations:
            uses = (
                f"{invitation['redemption_count']}/"
                f"{invitation['max_redemptions']}"
            )
            state = "revoked" if invitation["revoked_at"] else "active"
            print(
                f"{invitation['id']}  {uses:>5}  {state:<8}  "
                f"{_format_time(invitation['created_at']):<19}  "
                f"{invitation['label'] or '—'}"
            )
        return 0

    if args.command == "revoke":
        result = store.revoke_invitation(args.invitation_id)
        qualifier = "was already revoked" if result["already_revoked"] else "revoked"
        print(
            f"Invitation {result['id']} {qualifier}; "
            f"{result['sessions_revoked']} browser session(s) invalidated."
        )
        return 0

    if args.command == "purge-revoked":
        result = store.purge_revoked_invitations()
        print(
            f"Deleted {result['invitations_deleted']} revoked invitation(s), "
            f"{result['sessions_deleted']} session(s), and "
            f"{result['transfers_deleted']} transfer(s)."
        )
        return 0

    raise AssertionError("argparse accepted an unknown command")


def main(argv=None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except InvitationNotFoundError as error:
        print(f"access command failed: {error}", file=sys.stderr)
        return 2
    except (AccessStoreError, OSError, ValueError, sqlite3.Error) as error:
        print(f"access command failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
