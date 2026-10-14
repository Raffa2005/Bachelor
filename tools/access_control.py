#!/usr/bin/env python3
"""Persistent invitation access for the hosted double-move site.

Invitation and browser-session secrets are stored only as domain-separated
SHA-256 digests.  Invitations do not expire automatically; the operator can
revoke one explicitly, which also invalidates every browser session it issued.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Iterator, Optional
from urllib.parse import quote


TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOLS_DIR)
DEFAULT_GAME_DATABASE_PATH = os.path.join(
    REPO_ROOT, ".runtime", "public-play", "games.sqlite3"
)
ACCESS_COOKIE = "dm_access"
ACCESS_COOKIE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
DEFAULT_INVITATION_USES = 5
MAX_INVITATION_USES = 100
MODE_OPEN = "open"
MODE_INVITE_ONLY = "invite_only"
ACCESS_MODES = (MODE_OPEN, MODE_INVITE_ONLY)
SCHEMA_VERSION = 2
TRANSFER_MAX_AGE_SECONDS = 10 * 60
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_INVITATION_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_write_locks: dict[str, threading.RLock] = {}
_write_locks_guard = threading.Lock()


class AccessStoreError(Exception):
    """Base class for access-control failures."""


class InvalidInvitationError(AccessStoreError):
    """The supplied invitation cannot issue an access session."""


class InvalidTransferError(AccessStoreError):
    """The supplied legacy-host transfer is invalid, expired, or used."""


class InvitationLimitError(AccessStoreError):
    """The invitation has issued its configured number of sessions."""


class InvitationNotFoundError(AccessStoreError):
    """The operator supplied an unknown invitation id."""


@dataclass(frozen=True)
class CreatedInvitation:
    id: str
    token: str
    label: str
    max_redemptions: int
    created_at: int


@dataclass(frozen=True)
class Redemption:
    session_token: str
    invitation_id: str
    created: bool
    remaining_redemptions: int


@dataclass(frozen=True)
class Authorization:
    mode: str
    authorized: bool
    invitation_id: Optional[str] = None


@dataclass(frozen=True)
class TransferGrant:
    token: str
    invitation_id: str
    expires_at: int


def default_access_database_path(environ=None) -> str:
    """Resolve the access database beside the persistent game database."""

    environ = os.environ if environ is None else environ
    configured = environ.get("DM_ACCESS_DATABASE_PATH")
    if configured:
        return os.path.abspath(configured)
    game_database = os.path.abspath(
        environ.get("DM_DATABASE_PATH") or DEFAULT_GAME_DATABASE_PATH
    )
    return os.path.join(os.path.dirname(game_database), "access.sqlite3")


def _write_lock_for(path: str) -> threading.RLock:
    normalized = os.path.abspath(path)
    with _write_locks_guard:
        return _write_locks.setdefault(normalized, threading.RLock())


def _digest(kind: bytes, token: str) -> bytes:
    return hashlib.sha256(
        b"double-move-access-v1\0" + kind + b"\0" + token.encode("ascii")
    ).digest()


def _new_token() -> str:
    token = secrets.token_urlsafe(32)
    if not _TOKEN_RE.fullmatch(token):  # pragma: no cover - defensive
        raise RuntimeError("secure token generator returned an invalid token")
    return token


class AccessStore:
    """SQLite-backed access mode, invitations, and browser sessions."""

    def __init__(
        self,
        database_path: os.PathLike[str] | str,
        *,
        secure_cookie: bool = False,
        clock=time.time,
    ):
        raw_path = os.fspath(database_path)
        if not raw_path or raw_path == ":memory:":
            raise ValueError("access database must be a persistent file")
        self.database_path = os.path.abspath(raw_path)
        self.secure_cookie = bool(secure_cookie)
        self._clock = clock
        self._write_lock = _write_lock_for(self.database_path)
        parent = os.path.dirname(self.database_path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                pass
        self._prepare_database_file()
        self._initialize_schema()
        self._make_private()

    def _prepare_database_file(self) -> None:
        try:
            descriptor = os.open(
                self.database_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            metadata = os.stat(self.database_path, follow_symlinks=False)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("access database is not a regular file")
        else:
            os.close(descriptor)

    def _make_private(self) -> None:
        try:
            os.chmod(self.database_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        database_uri = (
            "file:"
            + quote(self.database_path, safe="/")
            + "?mode=rw"
        )
        connection = sqlite3.connect(
            database_uri,
            timeout=5,
            isolation_level=None,
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _initialize_schema(self) -> None:
        with self._write() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise AccessStoreError(
                    f"access database schema {version} is newer than supported "
                    f"schema {SCHEMA_VERSION}"
                )
            if version == 0:
                statements = (
                    """
                    CREATE TABLE access_settings (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        mode TEXT NOT NULL
                            CHECK (mode IN ('open', 'invite_only')),
                        public_origin TEXT
                    )
                    """,
                    """
                    INSERT INTO access_settings(singleton, mode, public_origin)
                    VALUES (1, 'open', NULL)
                    """,
                    """
                    CREATE TABLE invitations (
                        id TEXT PRIMARY KEY,
                        token_hash BLOB NOT NULL UNIQUE,
                        label TEXT NOT NULL,
                        max_redemptions INTEGER NOT NULL
                            CHECK (
                                max_redemptions >= 1
                                AND max_redemptions <= 100
                            ),
                        redemption_count INTEGER NOT NULL DEFAULT 0
                            CHECK (
                                redemption_count >= 0
                                AND redemption_count <= max_redemptions
                            ),
                        created_at INTEGER NOT NULL,
                        revoked_at INTEGER
                    )
                    """,
                    """
                    CREATE TABLE access_sessions (
                        token_hash BLOB PRIMARY KEY,
                        invitation_id TEXT NOT NULL
                            REFERENCES invitations(id) ON DELETE RESTRICT,
                        created_at INTEGER NOT NULL,
                        last_seen_at INTEGER NOT NULL,
                        revoked_at INTEGER
                    )
                    """,
                    """
                    CREATE INDEX access_sessions_invitation
                    ON access_sessions(invitation_id)
                    """,
                    """
                    CREATE TABLE access_transfers (
                        token_hash BLOB PRIMARY KEY,
                        source_session_hash BLOB NOT NULL
                            REFERENCES access_sessions(token_hash)
                            ON DELETE CASCADE,
                        invitation_id TEXT NOT NULL
                            REFERENCES invitations(id) ON DELETE CASCADE,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                            CHECK(expires_at > created_at),
                        redeemed_at INTEGER
                    )
                    """,
                    """
                    CREATE INDEX access_transfers_expiry
                    ON access_transfers(expires_at)
                    """,
                )
                for statement in statements:
                    connection.execute(statement)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version == 1:
                connection.execute(
                    """
                    CREATE TABLE access_transfers (
                        token_hash BLOB PRIMARY KEY,
                        source_session_hash BLOB NOT NULL
                            REFERENCES access_sessions(token_hash)
                            ON DELETE CASCADE,
                        invitation_id TEXT NOT NULL
                            REFERENCES invitations(id) ON DELETE CASCADE,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL
                            CHECK(expires_at > created_at),
                        redeemed_at INTEGER
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX access_transfers_expiry
                    ON access_transfers(expires_at)
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:  # pragma: no cover - future guard
                raise AccessStoreError("unsupported access database schema")

    def _now(self) -> int:
        return int(self._clock())

    @staticmethod
    def parse_session_cookie(cookie_header: Optional[str]) -> Optional[str]:
        if not cookie_header or len(cookie_header) > 4096:
            return None
        found = None
        for item in cookie_header.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == ACCESS_COOKIE:
                if found is not None or not _TOKEN_RE.fullmatch(value):
                    return None
                found = value
        return found

    def session_cookie_header(self, token: str) -> str:
        if not _TOKEN_RE.fullmatch(token):
            raise ValueError("invalid access session token")
        parts = [
            f"{ACCESS_COOKIE}={token}",
            "Path=/",
            f"Max-Age={ACCESS_COOKIE_MAX_AGE_SECONDS}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        public_origin = self.get_public_origin()
        if self.secure_cookie or (
            public_origin
            and public_origin.lower().startswith("https://")
        ):
            parts.append("Secure")
        return "; ".join(parts)

    def get_mode(self) -> str:
        with self._read() as connection:
            row = connection.execute(
                "SELECT mode FROM access_settings WHERE singleton = 1"
            ).fetchone()
        if row is None or row["mode"] not in ACCESS_MODES:
            raise AccessStoreError("access mode is unavailable")
        return str(row["mode"])

    def set_mode(self, mode: str) -> str:
        if mode not in ACCESS_MODES:
            raise ValueError("access mode must be 'open' or 'invite_only'")
        with self._write() as connection:
            cursor = connection.execute(
                "UPDATE access_settings SET mode = ? WHERE singleton = 1",
                (mode,),
            )
            if cursor.rowcount != 1:
                raise AccessStoreError("access settings are unavailable")
        return mode

    def get_public_origin(self) -> Optional[str]:
        with self._read() as connection:
            row = connection.execute(
                "SELECT public_origin FROM access_settings WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise AccessStoreError("access settings are unavailable")
        return row["public_origin"]

    def set_public_origin(self, origin: Optional[str]) -> Optional[str]:
        value = origin.rstrip("/") if origin else None
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE access_settings
                SET public_origin = ?
                WHERE singleton = 1
                """,
                (value,),
            )
            if cursor.rowcount != 1:
                raise AccessStoreError("access settings are unavailable")
        return value

    def create_invitation(
        self,
        label: str = "",
        *,
        max_redemptions: int = DEFAULT_INVITATION_USES,
    ) -> CreatedInvitation:
        if isinstance(max_redemptions, bool) or not isinstance(
            max_redemptions, int
        ):
            raise ValueError("invitation uses must be an integer")
        if not 1 <= max_redemptions <= MAX_INVITATION_USES:
            raise ValueError(
                f"invitation uses must be between 1 and {MAX_INVITATION_USES}"
            )
        normalized_label = " ".join(str(label).split())[:160]
        created_at = self._now()
        for _attempt in range(4):
            invitation_id = secrets.token_hex(8)
            token = _new_token()
            try:
                with self._write() as connection:
                    connection.execute(
                        """
                        INSERT INTO invitations(
                            id,
                            token_hash,
                            label,
                            max_redemptions,
                            created_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            invitation_id,
                            _digest(b"invitation", token),
                            normalized_label,
                            max_redemptions,
                            created_at,
                        ),
                    )
            except sqlite3.IntegrityError:
                continue
            return CreatedInvitation(
                id=invitation_id,
                token=token,
                label=normalized_label,
                max_redemptions=max_redemptions,
                created_at=created_at,
            )
        raise AccessStoreError("could not allocate a unique invitation")

    def list_invitations(self) -> list[dict]:
        with self._read() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    label,
                    max_redemptions,
                    redemption_count,
                    created_at,
                    revoked_at
                FROM invitations
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        return [
            {
                "id": row["id"],
                "label": row["label"],
                "max_redemptions": int(row["max_redemptions"]),
                "redemption_count": int(row["redemption_count"]),
                "remaining_redemptions": max(
                    0,
                    int(row["max_redemptions"])
                    - int(row["redemption_count"]),
                ),
                "created_at": int(row["created_at"]),
                "revoked_at": (
                    int(row["revoked_at"])
                    if row["revoked_at"] is not None
                    else None
                ),
            }
            for row in rows
        ]

    def revoke_invitation(self, invitation_id: str) -> dict:
        if not _INVITATION_ID_RE.fullmatch(str(invitation_id)):
            raise InvitationNotFoundError("invitation does not exist")
        now = self._now()
        with self._write() as connection:
            row = connection.execute(
                "SELECT revoked_at FROM invitations WHERE id = ?",
                (invitation_id,),
            ).fetchone()
            if row is None:
                raise InvitationNotFoundError("invitation does not exist")
            connection.execute(
                """
                UPDATE invitations
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ?
                """,
                (now, invitation_id),
            )
            cursor = connection.execute(
                """
                UPDATE access_sessions
                SET revoked_at = COALESCE(revoked_at, ?)
                WHERE invitation_id = ?
                  AND revoked_at IS NULL
                """,
                (now, invitation_id),
            )
        return {
            "id": invitation_id,
            "already_revoked": row["revoked_at"] is not None,
            "sessions_revoked": int(cursor.rowcount),
        }

    def purge_revoked_invitations(self) -> dict:
        """Permanently remove revoked invitations and their access records."""

        with self._write() as connection:
            invitation_count = int(connection.execute(
                "SELECT COUNT(*) FROM invitations WHERE revoked_at IS NOT NULL"
            ).fetchone()[0])
            session_count = int(connection.execute(
                """
                SELECT COUNT(*)
                FROM access_sessions
                WHERE invitation_id IN (
                    SELECT id FROM invitations WHERE revoked_at IS NOT NULL
                )
                """
            ).fetchone()[0])
            transfer_count = int(connection.execute(
                """
                SELECT COUNT(*)
                FROM access_transfers
                WHERE invitation_id IN (
                    SELECT id FROM invitations WHERE revoked_at IS NOT NULL
                )
                """
            ).fetchone()[0])
            connection.execute(
                """
                DELETE FROM access_transfers
                WHERE invitation_id IN (
                    SELECT id FROM invitations WHERE revoked_at IS NOT NULL
                )
                """
            )
            connection.execute(
                """
                DELETE FROM access_sessions
                WHERE invitation_id IN (
                    SELECT id FROM invitations WHERE revoked_at IS NOT NULL
                )
                """
            )
            connection.execute(
                "DELETE FROM invitations WHERE revoked_at IS NOT NULL"
            )
        return {
            "invitations_deleted": invitation_count,
            "sessions_deleted": session_count,
            "transfers_deleted": transfer_count,
        }

    @staticmethod
    def _active_session_row(
        connection: sqlite3.Connection, session_token: Optional[str]
    ) -> Optional[sqlite3.Row]:
        if not session_token or not _TOKEN_RE.fullmatch(session_token):
            return None
        return connection.execute(
            """
            SELECT
                s.invitation_id,
                i.revoked_at AS invitation_revoked_at,
                s.revoked_at AS session_revoked_at
            FROM access_sessions AS s
            JOIN invitations AS i ON i.id = s.invitation_id
            WHERE s.token_hash = ?
            """,
            (_digest(b"session", session_token),),
        ).fetchone()

    def authorize(self, session_token: Optional[str]) -> Authorization:
        with self._read() as connection:
            mode_row = connection.execute(
                "SELECT mode FROM access_settings WHERE singleton = 1"
            ).fetchone()
            if mode_row is None or mode_row["mode"] not in ACCESS_MODES:
                raise AccessStoreError("access mode is unavailable")
            mode = str(mode_row["mode"])
            if mode == MODE_OPEN:
                return Authorization(mode=mode, authorized=True)
            session = self._active_session_row(connection, session_token)
        active = bool(
            session is not None
            and session["session_revoked_at"] is None
            and session["invitation_revoked_at"] is None
        )
        return Authorization(
            mode=mode,
            authorized=active,
            invitation_id=(
                str(session["invitation_id"]) if active else None
            ),
        )

    def redeem(
        self,
        invitation_token: str,
        current_session_token: Optional[str] = None,
    ) -> Redemption:
        if not isinstance(invitation_token, str) or not _TOKEN_RE.fullmatch(
            invitation_token
        ):
            raise InvalidInvitationError("invitation is invalid or revoked")
        invitation_hash = _digest(b"invitation", invitation_token)
        now = self._now()
        with self._write() as connection:
            invitation = connection.execute(
                """
                SELECT id, max_redemptions, redemption_count, revoked_at
                FROM invitations
                WHERE token_hash = ?
                """,
                (invitation_hash,),
            ).fetchone()
            if invitation is None or invitation["revoked_at"] is not None:
                raise InvalidInvitationError("invitation is invalid or revoked")

            current = self._active_session_row(
                connection, current_session_token
            )
            if (
                current is not None
                and current["session_revoked_at"] is None
                and current["invitation_revoked_at"] is None
                and current["invitation_id"] == invitation["id"]
            ):
                connection.execute(
                    """
                    UPDATE access_sessions
                    SET last_seen_at = ?
                    WHERE token_hash = ?
                    """,
                    (
                        now,
                        _digest(b"session", current_session_token),
                    ),
                )
                return Redemption(
                    session_token=current_session_token,
                    invitation_id=str(invitation["id"]),
                    created=False,
                    remaining_redemptions=(
                        int(invitation["max_redemptions"])
                        - int(invitation["redemption_count"])
                    ),
                )

            if (
                int(invitation["redemption_count"])
                >= int(invitation["max_redemptions"])
            ):
                raise InvitationLimitError(
                    "invitation has used all available browser sessions"
                )

            for _attempt in range(4):
                session_token = _new_token()
                try:
                    connection.execute(
                        """
                        INSERT INTO access_sessions(
                            token_hash,
                            invitation_id,
                            created_at,
                            last_seen_at
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            _digest(b"session", session_token),
                            invitation["id"],
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError:
                    continue
                break
            else:  # pragma: no cover - cryptographically implausible
                raise AccessStoreError(
                    "could not allocate a unique access session"
                )

            connection.execute(
                """
                UPDATE invitations
                SET redemption_count = redemption_count + 1
                WHERE id = ?
                """,
                (invitation["id"],),
            )
            remaining = (
                int(invitation["max_redemptions"])
                - int(invitation["redemption_count"])
                - 1
            )
        return Redemption(
            session_token=session_token,
            invitation_id=str(invitation["id"]),
            created=True,
            remaining_redemptions=remaining,
        )

    def create_transfer(self, session_token: str) -> TransferGrant:
        """Issue a short-lived, one-use grant for a live access session."""

        if not isinstance(session_token, str) or not _TOKEN_RE.fullmatch(
            session_token
        ):
            raise InvalidTransferError("access transfer is invalid or expired")
        session_hash = _digest(b"session", session_token)
        now = self._now()
        expires_at = now + TRANSFER_MAX_AGE_SECONDS
        for _attempt in range(4):
            token = _new_token()
            try:
                with self._write() as connection:
                    session = self._active_session_row(
                        connection, session_token
                    )
                    if (
                        session is None
                        or session["session_revoked_at"] is not None
                        or session["invitation_revoked_at"] is not None
                    ):
                        raise InvalidTransferError(
                            "access transfer is invalid or expired"
                        )
                    connection.execute(
                        """
                        DELETE FROM access_transfers
                        WHERE expires_at <= ? OR redeemed_at IS NOT NULL
                        """,
                        (now,),
                    )
                    connection.execute(
                        """
                        UPDATE access_sessions
                        SET last_seen_at = ?
                        WHERE token_hash = ?
                        """,
                        (now, session_hash),
                    )
                    connection.execute(
                        """
                        INSERT INTO access_transfers(
                            token_hash,
                            source_session_hash,
                            invitation_id,
                            created_at,
                            expires_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            _digest(b"transfer", token),
                            session_hash,
                            session["invitation_id"],
                            now,
                            expires_at,
                        ),
                    )
            except sqlite3.IntegrityError:
                continue
            return TransferGrant(
                token=token,
                invitation_id=str(session["invitation_id"]),
                expires_at=expires_at,
            )
        raise AccessStoreError("could not allocate an access transfer")

    def redeem_transfer(
        self,
        transfer_token: str,
        current_session_token: Optional[str] = None,
    ) -> Redemption:
        """Exchange a legacy-host grant without consuming an invitation use."""

        if not isinstance(transfer_token, str) or not _TOKEN_RE.fullmatch(
            transfer_token
        ):
            raise InvalidTransferError("access transfer is invalid or expired")
        now = self._now()
        transfer_hash = _digest(b"transfer", transfer_token)
        with self._write() as connection:
            transfer = connection.execute(
                """
                SELECT
                    t.invitation_id,
                    t.expires_at,
                    t.redeemed_at,
                    source.revoked_at AS source_revoked_at,
                    invitation.revoked_at AS invitation_revoked_at,
                    invitation.max_redemptions,
                    invitation.redemption_count
                FROM access_transfers AS t
                JOIN access_sessions AS source
                    ON source.token_hash = t.source_session_hash
                JOIN invitations AS invitation
                    ON invitation.id = t.invitation_id
                WHERE t.token_hash = ?
                """,
                (transfer_hash,),
            ).fetchone()
            if (
                transfer is None
                or transfer["redeemed_at"] is not None
                or int(transfer["expires_at"]) <= now
                or transfer["source_revoked_at"] is not None
                or transfer["invitation_revoked_at"] is not None
            ):
                raise InvalidTransferError(
                    "access transfer is invalid or expired"
                )

            current = self._active_session_row(
                connection, current_session_token
            )
            if (
                current is not None
                and current["session_revoked_at"] is None
                and current["invitation_revoked_at"] is None
                and current["invitation_id"] == transfer["invitation_id"]
            ):
                session_token = current_session_token
                created = False
                connection.execute(
                    """
                    UPDATE access_sessions
                    SET last_seen_at = ?
                    WHERE token_hash = ?
                    """,
                    (now, _digest(b"session", session_token)),
                )
            else:
                created = True
                for _attempt in range(4):
                    session_token = _new_token()
                    try:
                        connection.execute(
                            """
                            INSERT INTO access_sessions(
                                token_hash,
                                invitation_id,
                                created_at,
                                last_seen_at
                            ) VALUES (?, ?, ?, ?)
                            """,
                            (
                                _digest(b"session", session_token),
                                transfer["invitation_id"],
                                now,
                                now,
                            ),
                        )
                    except sqlite3.IntegrityError:
                        continue
                    break
                else:  # pragma: no cover - cryptographically implausible
                    raise AccessStoreError(
                        "could not allocate a transferred access session"
                    )

            cursor = connection.execute(
                """
                UPDATE access_transfers
                SET redeemed_at = ?
                WHERE token_hash = ?
                  AND redeemed_at IS NULL
                  AND expires_at > ?
                """,
                (now, transfer_hash, now),
            )
            if cursor.rowcount != 1:
                raise InvalidTransferError(
                    "access transfer is invalid or expired"
                )
            remaining = max(
                0,
                int(transfer["max_redemptions"])
                - int(transfer["redemption_count"]),
            )
        return Redemption(
            session_token=session_token,
            invitation_id=str(transfer["invitation_id"]),
            created=created,
            remaining_redemptions=remaining,
        )

    def health_check(self) -> bool:
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT mode
                FROM access_settings
                WHERE singleton = 1
                """
            ).fetchone()
            connection.execute(
                """
                SELECT
                    id,
                    token_hash,
                    label,
                    max_redemptions,
                    redemption_count,
                    created_at,
                    revoked_at
                FROM invitations
                LIMIT 0
                """
            ).fetchall()
            connection.execute(
                """
                SELECT
                    token_hash,
                    source_session_hash,
                    invitation_id,
                    created_at,
                    expires_at,
                    redeemed_at
                FROM access_transfers
                LIMIT 0
                """
            ).fetchall()
            connection.execute(
                """
                SELECT
                    token_hash,
                    invitation_id,
                    created_at,
                    last_seen_at,
                    revoked_at
                FROM access_sessions
                LIMIT 0
                """
            ).fetchall()
        if row is None or row["mode"] not in ACCESS_MODES:
            raise AccessStoreError("access database is missing required state")
        return True


__all__ = [
    "ACCESS_COOKIE",
    "ACCESS_COOKIE_MAX_AGE_SECONDS",
    "ACCESS_MODES",
    "AccessStore",
    "AccessStoreError",
    "Authorization",
    "CreatedInvitation",
    "DEFAULT_INVITATION_USES",
    "InvitationLimitError",
    "InvitationNotFoundError",
    "InvalidInvitationError",
    "InvalidTransferError",
    "MAX_INVITATION_USES",
    "MODE_INVITE_ONLY",
    "MODE_OPEN",
    "Redemption",
    "TRANSFER_MAX_AGE_SECONDS",
    "TransferGrant",
    "default_access_database_path",
]
