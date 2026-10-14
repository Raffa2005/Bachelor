#!/usr/bin/env python3
"""Persistent storage for anonymous double-move public games.

The repository deliberately owns its SQLite connections: every public
operation opens a fresh connection, applies the safety pragmas, and closes it
again.  Writes are serialized inside the process and use short
``BEGIN IMMEDIATE`` transactions.  SQLite's rollback journal is used instead
of WAL so deployments do not depend on WAL-reset behaviour in the host
SQLite version.

Only hashes of anonymous bearer tokens are persisted.  This module has no API
for IP addresses or user agents, and event payload validation rejects fields
that look like credentials or request metadata.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence
from urllib.parse import quote


SCHEMA_VERSION = 4

DEFAULT_SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
DEFAULT_ABANDON_AFTER_SECONDS = 2 * 24 * 60 * 60
DEFAULT_ABANDONED_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_COMMAND_LEASE_SECONDS = 30

MAX_FEN_LENGTH = 256
MAX_MOVES = 2048
MAX_JSON_BYTES = 64 * 1024
MAX_NAME_LENGTH = 80
MAX_MODEL_LENGTH = 160
MAX_PRESET_LENGTH = 80
MAX_BACKEND_LENGTH = 80
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_EVENT_TYPE_LENGTH = 64
MAX_CLOCK_MS = 14 * 24 * 60 * 60 * 1000

_TOKEN_MIN_LENGTH = 20
_TOKEN_MAX_LENGTH = 512
_PUBLIC_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?\+?$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+@/ -]*$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_COMMAND_STAGE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FORBIDDEN_PAYLOAD_KEYS = {
    "authorization",
    "cookie",
    "ip",
    "ip_address",
    "raw_token",
    "remote_addr",
    "session",
    "session_id",
    "session_token",
    "token",
    "ua",
    "user_agent",
}
_FORBIDDEN_COLLAPSED_PAYLOAD_KEYS = {
    "authorization",
    "clientip",
    "cookie",
    "ip",
    "ipaddress",
    "rawtoken",
    "remoteaddr",
    "remoteip",
    "requestip",
    "session",
    "sessionid",
    "sessiontoken",
    "token",
    "ua",
    "useragent",
}

_LOCKS_GUARD = threading.Lock()
_WRITE_LOCKS: dict[str, threading.RLock] = {}


class GameStoreError(Exception):
    """Base class for repository errors safe for application handling."""


class ValidationError(GameStoreError):
    """Input did not satisfy the repository contract."""


class NotFoundError(GameStoreError):
    """The requested session, game, turn, or command does not exist."""


class OwnershipError(GameStoreError):
    """The supplied anonymous session does not own the requested game."""


class InvalidTransition(GameStoreError):
    """The requested state transition is not legal from the current state."""


class PendingCommand(InvalidTransition):
    """A durable command must be recovered before timeout can advance."""

    def __init__(self, retry_after_ms: int):
        super().__init__(
            "a pending command must be recovered before timeout"
        )
        self.retry_after_ms = retry_after_ms


class RevisionConflict(GameStoreError):
    """An optimistic write raced with another state transition."""

    def __init__(self, expected: int, actual: int):
        super().__init__(
            f"game revision conflict: expected {expected}, current is {actual}"
        )
        self.expected = expected
        self.actual = actual


class IdempotencyConflict(GameStoreError):
    """An idempotency key was reused for a different request."""


def _canonical_json(value: Any, *, label: str = "JSON") -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValidationError(f"{label} exceeds {MAX_JSON_BYTES} bytes")
    return encoded


def _decode_json(value: Optional[str], fallback: Any = None) -> Any:
    if value is None:
        return fallback
    return json.loads(value)


def _require_string(
    value: Any,
    label: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValidationError(f"{label} must not be empty")
    if len(value) > maximum:
        raise ValidationError(f"{label} exceeds {maximum} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValidationError(f"{label} must be valid UTF-8") from None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValidationError(f"{label} contains control characters")
    return value


def _validate_fen(value: Any, label: str = "FEN") -> str:
    fen = _require_string(value, label, maximum=MAX_FEN_LENGTH)
    fields = fen.split()
    if len(fields) < 4 or len(fields) > 7:
        raise ValidationError(f"{label} must have between 4 and 7 fields")
    return fen


def _validate_move(value: Any) -> str:
    if not isinstance(value, str) or not _MOVE_RE.fullmatch(value):
        raise ValidationError(f"invalid UCI move leg: {value!r}")
    return value


def _validate_public_id(value: Any, label: str = "game id") -> str:
    if not isinstance(value, str) or not _PUBLIC_ID_RE.fullmatch(value):
        raise ValidationError(f"{label} must be a 32-character lowercase UUID")
    return value


def _validate_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("expected_revision must be a non-negative integer")
    return value


def _validate_received_at_ms(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError(
            "received_at_ms must be a non-negative integer"
        )
    return value


def _validate_recoverable_command_request(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(
            "recoverable command request must be an object"
        )
    operation = value.get("operation")
    if operation == "move":
        if set(value) != {
            "operation",
            "move",
            "expected_revision",
        }:
            raise ValidationError(
                "recoverable move request has invalid fields"
            )
        move = value.get("move")
        if not isinstance(move, str) or not _MOVE_RE.fullmatch(move):
            raise ValidationError(
                "recoverable move request has an invalid move"
            )
        revision = _validate_revision(value.get("expected_revision"))
        return {
            "operation": "move",
            "move": move,
            "expected_revision": revision,
        }
    if operation in ("takeback", "resign"):
        if set(value) != {"operation", "expected_revision"}:
            raise ValidationError(
                "recoverable command request has invalid fields"
            )
        revision = _validate_revision(value.get("expected_revision"))
        return {
            "operation": operation,
            "expected_revision": revision,
        }
    raise ValidationError(
        "recoverable command operation is invalid"
    )


def _validate_color(value: Any, label: str = "color") -> str:
    if value not in ("white", "black"):
        raise ValidationError(f"{label} must be 'white' or 'black'")
    return value


def _other_color(color: str) -> str:
    return "black" if color == "white" else "white"


def _validate_label(value: Any, label: str, maximum: int) -> str:
    text = _require_string(value, label, maximum=maximum)
    if not _SAFE_LABEL_RE.fullmatch(text):
        raise ValidationError(f"{label} contains unsupported characters")
    return text


def _validate_payload_keys(value: Any, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValidationError(f"{path} keys must be strings")
            normalized = key.strip().lower().replace("-", "_")
            collapsed = re.sub(r"[^a-z0-9]", "", normalized)
            if (
                normalized in _FORBIDDEN_PAYLOAD_KEYS
                or collapsed in _FORBIDDEN_COLLAPSED_PAYLOAD_KEYS
            ):
                raise ValidationError(
                    f"{path} must not contain request metadata or credentials"
                )
            _validate_payload_keys(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_payload_keys(child, f"{path}[{index}]")


def _validate_payload_secrets(
    value: Any,
    secrets_to_reject: Sequence[Any],
    *,
    label: str,
) -> None:
    """Reject bearer credentials hidden anywhere in a persisted payload.

    Key-name filtering is intentionally insufficient here: an otherwise
    harmless key such as ``opaque`` could still contain a raw session or
    command-lease token.  Check mapping keys and values recursively, and do
    not include the offending value in the validation error.
    """

    text_secrets = tuple(
        secret
        for secret in secrets_to_reject
        if isinstance(secret, str) and secret
    )
    byte_secrets = tuple(
        secret.encode("utf-8") for secret in text_secrets
    )
    if isinstance(value, str):
        if any(secret in value for secret in text_secrets):
            raise ValidationError(
                f"{label} must not contain bearer credentials"
            )
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        if any(secret in raw for secret in byte_secrets):
            raise ValidationError(
                f"{label} must not contain bearer credentials"
            )
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_payload_secrets(
                key, text_secrets, label=label
            )
            _validate_payload_secrets(
                child, text_secrets, label=label
            )
    elif isinstance(value, (list, tuple)):
        for child in value:
            _validate_payload_secrets(
                child, text_secrets, label=label
            )


def _hash_token(token: str) -> bytes:
    if not isinstance(token, str):
        raise ValidationError("session token must be a string")
    if not _TOKEN_MIN_LENGTH <= len(token) <= _TOKEN_MAX_LENGTH:
        raise ValidationError(
            f"session token must be {_TOKEN_MIN_LENGTH}-"
            f"{_TOKEN_MAX_LENGTH} characters"
        )
    return hashlib.sha256(token.encode("utf-8")).digest()


def _request_hash(request: Any) -> tuple[str, str]:
    encoded = _canonical_json(request, label="command request")
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest(), encoded


def _write_lock_for(path: str) -> threading.RLock:
    try:
        metadata = os.stat(path)
        normalized = f"inode:{metadata.st_dev}:{metadata.st_ino}"
    except OSError:
        normalized = "path:" + os.path.realpath(path)
    with _LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _WRITE_LOCKS[normalized] = lock
        return lock


class GameStore:
    """SQLite repository for anonymous human-vs-engine games.

    Public methods return plain dictionaries so the HTTP layer can serialize
    results without depending on repository-specific classes.
    """

    def __init__(
        self,
        database_path: os.PathLike[str] | str,
        *,
        busy_timeout_ms: int = 5000,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        abandon_after_seconds: int = DEFAULT_ABANDON_AFTER_SECONDS,
        abandoned_retention_seconds: int = (
            DEFAULT_ABANDONED_RETENTION_SECONDS
        ),
        command_lease_seconds: int = DEFAULT_COMMAND_LEASE_SECONDS,
        clock: Callable[[], float] = time.time,
    ):
        raw_path = os.fspath(database_path)
        if not raw_path or raw_path == ":memory:":
            raise ValidationError("database_path must be a persistent file")
        if isinstance(busy_timeout_ms, bool) or not 1 <= busy_timeout_ms <= 60_000:
            raise ValidationError("busy_timeout_ms must be between 1 and 60000")
        for value, label in (
            (session_ttl_seconds, "session_ttl_seconds"),
            (abandon_after_seconds, "abandon_after_seconds"),
            (
                abandoned_retention_seconds,
                "abandoned_retention_seconds",
            ),
            (command_lease_seconds, "command_lease_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"{label} must be a positive integer")

        self.database_path = os.path.abspath(raw_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.session_ttl_seconds = session_ttl_seconds
        self.abandon_after_seconds = abandon_after_seconds
        self.abandoned_retention_seconds = abandoned_retention_seconds
        if not 5 <= command_lease_seconds <= 300:
            raise ValidationError(
                "command_lease_seconds must be between 5 and 300"
            )
        self.command_lease_seconds = command_lease_seconds
        self._clock = clock

        parent = os.path.dirname(self.database_path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        self._prepare_private_database_file(self.database_path)
        self._write_lock = _write_lock_for(self.database_path)
        self._migrate()
        self._make_private(self.database_path)

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _connect(self, path: Optional[str] = None) -> sqlite3.Connection:
        database_path = self.database_path if path is None else path
        self._prepare_private_database_file(database_path)
        connection = sqlite3.connect(
            database_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=DELETE")
        self._make_private(database_path)
        return connection

    @staticmethod
    def _prepare_private_database_file(path: str) -> None:
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    @staticmethod
    def _make_private(path: str) -> None:
        if path != ":memory:" and os.path.exists(path):
            os.chmod(path, 0o600)

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

    def _migrate(self) -> None:
        with self._write_lock:
            connection = self._connect()
            try:
                # Serialize first-start and upgrade decisions across processes.
                # Reading user_version before this lock lets every contender
                # independently decide that an empty database needs creation.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    current = int(
                        connection.execute("PRAGMA user_version").fetchone()[0]
                    )
                    if current > SCHEMA_VERSION:
                        raise GameStoreError(
                            f"database schema {current} is newer than supported "
                            f"schema {SCHEMA_VERSION}"
                        )
                    if current == 0:
                        self._migration_1(connection)
                    elif current in (1, 2, 3):
                        if current == 1:
                            self._migration_2(connection)
                        if current <= 2:
                            self._migration_3(connection)
                        self._migration_4(connection)
                    if current < SCHEMA_VERSION:
                        connection.execute(
                            f"PRAGMA user_version={SCHEMA_VERSION}"
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            finally:
                connection.close()

    @staticmethod
    def _migration_1(connection: sqlite3.Connection) -> None:
        statements = (
            """
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY,
                token_hash BLOB NOT NULL UNIQUE
                    CHECK(typeof(token_hash) = 'blob' AND length(token_hash) = 32),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                last_seen_at_ms INTEGER NOT NULL CHECK(last_seen_at_ms >= created_at_ms),
                expires_at_ms INTEGER NOT NULL CHECK(expires_at_ms > created_at_ms),
                revoked_at_ms INTEGER
                    CHECK(revoked_at_ms IS NULL OR revoked_at_ms >= created_at_ms)
            )
            """,
            """
            CREATE TABLE games (
                id INTEGER PRIMARY KEY,
                public_id TEXT NOT NULL UNIQUE
                    CHECK(length(public_id) = 32
                          AND public_id NOT GLOB '*[^0-9a-f]*'),
                owner_session_id INTEGER REFERENCES sessions(id)
                    ON DELETE SET NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('active', 'completed', 'abandoned')),
                start_fen TEXT NOT NULL
                    CHECK(length(start_fen) BETWEEN 1 AND 256),
                current_fen TEXT NOT NULL
                    CHECK(length(current_fen) BETWEEN 1 AND 256),
                active_moves_json TEXT NOT NULL
                    CHECK(length(active_moves_json) <= 65536),
                human_color TEXT NOT NULL
                    CHECK(human_color IN ('white', 'black')),
                engine_color TEXT NOT NULL
                    CHECK(engine_color IN ('white', 'black')
                          AND engine_color <> human_color),
                model_id TEXT NOT NULL
                    CHECK(length(model_id) BETWEEN 1 AND 160),
                model_sha256 TEXT NOT NULL
                    CHECK(length(model_sha256) = 64
                          AND model_sha256 NOT GLOB '*[^0-9a-f]*'),
                preset TEXT NOT NULL
                    CHECK(length(preset) BETWEEN 1 AND 80),
                nodes INTEGER NOT NULL CHECK(nodes BETWEEN 0 AND 1000000000),
                temperature REAL NOT NULL CHECK(temperature BETWEEN 0.0 AND 10.0),
                temp_decay_moves INTEGER NOT NULL
                    CHECK(temp_decay_moves BETWEEN 0 AND 640),
                backend TEXT NOT NULL
                    CHECK(length(backend) BETWEEN 1 AND 80),
                clock_mode TEXT NOT NULL
                    CHECK(clock_mode IN ('unlimited', 'timed')),
                clock_initial_ms INTEGER,
                clock_increment_ms INTEGER NOT NULL,
                white_time_ms INTEGER,
                black_time_ms INTEGER,
                clock_running_color TEXT
                    CHECK(clock_running_color IS NULL
                          OR clock_running_color IN ('white', 'black')),
                clock_anchor_at_ms INTEGER,
                revision INTEGER NOT NULL DEFAULT 0 CHECK(revision >= 0),
                result TEXT CHECK(result IS NULL OR result IN
                    ('1-0', '0-1', '1/2-1/2', '*')),
                termination TEXT
                    CHECK(termination IS NULL OR length(termination) BETWEEN 1 AND 64),
                player_name TEXT CHECK(player_name IS NULL
                    OR length(player_name) BETWEEN 1 AND 80),
                name_visible INTEGER NOT NULL DEFAULT 0
                    CHECK(name_visible IN (0, 1)
                          AND (name_visible = 0 OR player_name IS NOT NULL)),
                publish_on_completion INTEGER NOT NULL DEFAULT 1
                    CHECK(publish_on_completion IN (0, 1)),
                public_visible INTEGER NOT NULL DEFAULT 0
                    CHECK(public_visible IN (0, 1)
                          AND public_visible <= publish_on_completion),
                takeback_count INTEGER NOT NULL DEFAULT 0
                    CHECK(takeback_count >= 0),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
                completed_at_ms INTEGER,
                CHECK(
                    (clock_mode = 'unlimited'
                     AND clock_initial_ms IS NULL
                     AND clock_increment_ms = 0
                     AND white_time_ms IS NULL
                     AND black_time_ms IS NULL
                     AND clock_running_color IS NULL
                     AND clock_anchor_at_ms IS NULL)
                    OR
                    (clock_mode = 'timed'
                     AND clock_initial_ms BETWEEN 1000 AND 86400000
                     AND clock_increment_ms BETWEEN 0 AND 3600000
                     AND white_time_ms BETWEEN 0 AND 1209600000
                     AND black_time_ms BETWEEN 0 AND 1209600000
                     AND (clock_running_color IS NULL
                          OR clock_anchor_at_ms IS NOT NULL))
                ),
                CHECK(
                    (status = 'active'
                     AND result IS NULL
                     AND termination IS NULL
                     AND completed_at_ms IS NULL
                     AND public_visible = 0)
                    OR
                    (status = 'completed'
                     AND result IS NOT NULL
                     AND termination IS NOT NULL
                     AND completed_at_ms IS NOT NULL)
                    OR
                    (status = 'abandoned'
                     AND result = '*'
                     AND termination = 'abandoned'
                     AND completed_at_ms IS NOT NULL
                     AND public_visible = 0)
                )
            )
            """,
            """
            CREATE TABLE turns (
                id INTEGER PRIMARY KEY,
                public_id TEXT NOT NULL UNIQUE
                    CHECK(length(public_id) = 32
                          AND public_id NOT GLOB '*[^0-9a-f]*'),
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                turn_index INTEGER NOT NULL CHECK(turn_index >= 0),
                actor TEXT NOT NULL CHECK(actor IN ('human', 'engine')),
                color TEXT NOT NULL CHECK(color IN ('white', 'black')),
                before_fen TEXT NOT NULL CHECK(length(before_fen) BETWEEN 1 AND 256),
                after_fen TEXT NOT NULL CHECK(length(after_fen) BETWEEN 1 AND 256),
                before_moves_json TEXT NOT NULL CHECK(length(before_moves_json) <= 65536),
                after_moves_json TEXT NOT NULL CHECK(length(after_moves_json) <= 65536),
                before_clock_json TEXT NOT NULL CHECK(length(before_clock_json) <= 65536),
                after_clock_json TEXT NOT NULL CHECK(length(after_clock_json) <= 65536),
                completed INTEGER NOT NULL CHECK(completed IN (0, 1)),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                completed_at_ms INTEGER,
                undone_at_ms INTEGER,
                CHECK((active = 1 AND undone_at_ms IS NULL)
                      OR (active = 0 AND undone_at_ms IS NOT NULL)),
                CHECK((completed = 0 AND completed_at_ms IS NULL)
                      OR (completed = 1 AND completed_at_ms IS NOT NULL)),
                UNIQUE(game_id, turn_index)
            )
            """,
            """
            CREATE TABLE move_legs (
                id INTEGER PRIMARY KEY,
                turn_id INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                leg_index INTEGER NOT NULL CHECK(leg_index IN (1, 2)),
                uci TEXT NOT NULL
                    CHECK(length(uci) BETWEEN 4 AND 7)
                    CHECK(leg_index = 1 OR substr(uci, -1) <> '+'),
                before_fen TEXT NOT NULL CHECK(length(before_fen) BETWEEN 1 AND 256),
                after_fen TEXT NOT NULL CHECK(length(after_fen) BETWEEN 1 AND 256),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                undone_at_ms INTEGER,
                CHECK((active = 1 AND undone_at_ms IS NULL)
                      OR (active = 0 AND undone_at_ms IS NOT NULL)),
                UNIQUE(turn_id, leg_index)
            )
            """,
            """
            CREATE TABLE game_events (
                id INTEGER PRIMARY KEY,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                event_index INTEGER NOT NULL CHECK(event_index >= 0),
                revision INTEGER NOT NULL CHECK(revision >= 0),
                event_type TEXT NOT NULL
                    CHECK(length(event_type) BETWEEN 1 AND 64),
                actor TEXT NOT NULL
                    CHECK(actor IN ('human', 'engine', 'system')),
                payload_json TEXT NOT NULL CHECK(length(payload_json) <= 65536),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                UNIQUE(game_id, event_index)
            )
            """,
            """
            CREATE TABLE idempotent_commands (
                id INTEGER PRIMARY KEY,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL
                    CHECK(length(idempotency_key) BETWEEN 8 AND 128),
                request_hash TEXT NOT NULL
                    CHECK(length(request_hash) = 64
                          AND request_hash NOT GLOB '*[^0-9a-f]*'),
                status TEXT NOT NULL
                    CHECK(status IN ('in_progress', 'completed', 'failed')),
                lease_hash BLOB
                    CHECK(lease_hash IS NULL OR
                          (typeof(lease_hash) = 'blob' AND length(lease_hash) = 32)),
                lease_expires_at_ms INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
                stage TEXT NOT NULL DEFAULT 'started'
                    CHECK(length(stage) BETWEEN 1 AND 64),
                stage_json TEXT NOT NULL DEFAULT '{}'
                    CHECK(length(stage_json) <= 65536),
                completed_revision INTEGER CHECK(
                    completed_revision IS NULL OR completed_revision >= 0
                ),
                response_json TEXT CHECK(response_json IS NULL
                    OR length(response_json) <= 65536),
                error_json TEXT CHECK(error_json IS NULL
                    OR length(error_json) <= 65536),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
                CHECK(
                    (status = 'in_progress'
                     AND lease_hash IS NOT NULL
                     AND lease_expires_at_ms IS NOT NULL
                     AND response_json IS NULL AND error_json IS NULL
                     AND completed_revision IS NULL)
                    OR
                    (status = 'completed'
                     AND lease_hash IS NULL AND lease_expires_at_ms IS NULL
                     AND response_json IS NOT NULL AND error_json IS NULL)
                    OR
                    (status = 'failed'
                     AND lease_hash IS NULL AND lease_expires_at_ms IS NULL
                     AND response_json IS NULL AND error_json IS NOT NULL)
                ),
                UNIQUE(game_id, idempotency_key)
            )
            """,
            """
            CREATE TABLE session_commands (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id)
                    ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL
                    CHECK(length(idempotency_key) BETWEEN 8 AND 128),
                request_hash TEXT NOT NULL
                    CHECK(length(request_hash) = 64
                          AND request_hash NOT GLOB '*[^0-9a-f]*'),
                status TEXT NOT NULL
                    CHECK(status IN ('in_progress', 'completed')),
                game_id INTEGER REFERENCES games(id) ON DELETE SET NULL,
                response_json TEXT CHECK(response_json IS NULL
                    OR length(response_json) <= 65536),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
                CHECK(
                    (status = 'in_progress'
                     AND game_id IS NULL AND response_json IS NULL)
                    OR
                    (status = 'completed' AND response_json IS NOT NULL)
                ),
                UNIQUE(session_id, idempotency_key)
            )
            """,
            """
            CREATE INDEX sessions_expiry_idx ON sessions(expires_at_ms)
            """,
            """
            CREATE INDEX games_owner_idx ON games(owner_session_id, updated_at_ms)
            """,
            """
            CREATE UNIQUE INDEX games_one_active_owner_idx
            ON games(owner_session_id)
            WHERE status = 'active' AND owner_session_id IS NOT NULL
            """,
            """
            CREATE INDEX games_public_idx
            ON games(status, public_visible, completed_at_ms DESC, public_id DESC)
            """,
            """
            CREATE INDEX turns_game_active_idx
            ON turns(game_id, active, turn_index)
            """,
            """
            CREATE INDEX events_game_idx
            ON game_events(game_id, event_index)
            """,
            """
            CREATE TRIGGER games_immutable_configuration
            BEFORE UPDATE OF
                public_id, start_fen, human_color, engine_color,
                model_id, model_sha256, preset, nodes, temperature,
                temp_decay_moves, backend,
                clock_mode, clock_initial_ms, clock_increment_ms,
                publish_on_completion, created_at_ms
            ON games
            WHEN
                NEW.public_id IS NOT OLD.public_id
                OR NEW.start_fen IS NOT OLD.start_fen
                OR NEW.human_color IS NOT OLD.human_color
                OR NEW.engine_color IS NOT OLD.engine_color
                OR NEW.model_id IS NOT OLD.model_id
                OR NEW.model_sha256 IS NOT OLD.model_sha256
                OR NEW.preset IS NOT OLD.preset
                OR NEW.nodes IS NOT OLD.nodes
                OR NEW.temperature IS NOT OLD.temperature
                OR NEW.temp_decay_moves IS NOT OLD.temp_decay_moves
                OR NEW.backend IS NOT OLD.backend
                OR NEW.clock_mode IS NOT OLD.clock_mode
                OR NEW.clock_initial_ms IS NOT OLD.clock_initial_ms
                OR NEW.clock_increment_ms IS NOT OLD.clock_increment_ms
                OR NEW.publish_on_completion IS NOT OLD.publish_on_completion
                OR NEW.created_at_ms IS NOT OLD.created_at_ms
            BEGIN
                SELECT RAISE(ABORT, 'immutable game configuration');
            END
            """,
            """
            CREATE TRIGGER games_forward_status_only
            BEFORE UPDATE OF status ON games
            WHEN NEW.status <> OLD.status
                 AND NOT (
                    OLD.status = 'active'
                    AND NEW.status IN ('completed', 'abandoned')
                 )
            BEGIN
                SELECT RAISE(ABORT, 'invalid game status transition');
            END
            """,
            """
            CREATE TRIGGER games_terminal_state_immutable
            BEFORE UPDATE OF
                status, current_fen, active_moves_json,
                white_time_ms, black_time_ms,
                clock_running_color, clock_anchor_at_ms,
                revision, result, termination, public_visible,
                takeback_count, updated_at_ms, completed_at_ms
            ON games
            WHEN OLD.status IN ('completed', 'abandoned')
                 AND (
                    NEW.status IS NOT OLD.status
                    OR NEW.current_fen IS NOT OLD.current_fen
                    OR NEW.active_moves_json IS NOT OLD.active_moves_json
                    OR NEW.white_time_ms IS NOT OLD.white_time_ms
                    OR NEW.black_time_ms IS NOT OLD.black_time_ms
                    OR NEW.clock_running_color IS NOT OLD.clock_running_color
                    OR NEW.clock_anchor_at_ms IS NOT OLD.clock_anchor_at_ms
                    OR NEW.revision IS NOT OLD.revision
                    OR NEW.result IS NOT OLD.result
                    OR NEW.termination IS NOT OLD.termination
                    OR NEW.public_visible IS NOT OLD.public_visible
                    OR NEW.takeback_count IS NOT OLD.takeback_count
                    OR NEW.updated_at_ms IS NOT OLD.updated_at_ms
                    OR NEW.completed_at_ms IS NOT OLD.completed_at_ms
                 )
            BEGIN
                SELECT RAISE(ABORT, 'terminal game state is immutable');
            END
            """,
            """
            CREATE TRIGGER game_events_no_update
            BEFORE UPDATE ON game_events
            BEGIN
                SELECT RAISE(ABORT, 'game events are append-only');
            END
            """,
            """
            CREATE TRIGGER move_legs_second_requires_activation
            BEFORE INSERT ON move_legs
            WHEN NEW.leg_index = 2
                 AND (
                    substr(NEW.uci, -1) = '+'
                    OR NOT EXISTS (
                        SELECT 1 FROM move_legs
                        WHERE turn_id = NEW.turn_id
                          AND leg_index = 1
                          AND substr(uci, -1) = '+'
                    )
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'second leg requires a first activation leg'
                );
            END
            """,
            """
            CREATE TRIGGER turns_shape_before_completion
            BEFORE UPDATE OF completed ON turns
            WHEN OLD.completed = 0 AND NEW.completed = 1
                 AND NOT (
                    (
                        (SELECT COUNT(*) FROM move_legs
                         WHERE turn_id = OLD.id) = 1
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 1) <> '+'
                    )
                    OR
                    (
                        (SELECT COUNT(*) FROM move_legs
                         WHERE turn_id = OLD.id) = 2
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 1) = '+'
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 2) <> '+'
                    )
                 )
            BEGIN
                SELECT RAISE(ABORT, 'invalid completed turn leg shape');
            END
            """,
            """
            CREATE TRIGGER turns_reject_precompleted_insert
            BEFORE INSERT ON turns
            WHEN NEW.completed <> 0
                 OR NEW.completed_at_ms IS NOT NULL
                 OR NEW.active <> 1
                 OR NEW.undone_at_ms IS NOT NULL
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'turns must be inserted as active incomplete drafts'
                );
            END
            """,
            """
            CREATE TRIGGER move_legs_update_guard
            BEFORE UPDATE ON move_legs
            WHEN NOT (
                OLD.active = 1
                AND NEW.active = 0
                AND OLD.undone_at_ms IS NULL
                AND NEW.undone_at_ms IS NOT NULL
                AND NEW.id IS OLD.id
                AND NEW.turn_id IS OLD.turn_id
                AND NEW.leg_index IS OLD.leg_index
                AND NEW.uci IS OLD.uci
                AND NEW.before_fen IS OLD.before_fen
                AND NEW.after_fen IS OLD.after_fen
                AND NEW.created_at_ms IS OLD.created_at_ms
            )
            BEGIN
                SELECT RAISE(ABORT, 'move legs are immutable after insertion');
            END
            """,
            """
            CREATE TRIGGER turns_update_guard
            BEFORE UPDATE ON turns
            WHEN NOT (
                (
                    OLD.completed = 0
                    AND NEW.completed = 1
                    AND NEW.completed_at_ms IS NOT NULL
                    AND OLD.active = 1
                    AND NEW.active = 1
                    AND OLD.undone_at_ms IS NULL
                    AND NEW.undone_at_ms IS NULL
                    AND NEW.id IS OLD.id
                    AND NEW.public_id IS OLD.public_id
                    AND NEW.game_id IS OLD.game_id
                    AND NEW.turn_index IS OLD.turn_index
                    AND NEW.actor IS OLD.actor
                    AND NEW.color IS OLD.color
                    AND NEW.before_fen IS OLD.before_fen
                    AND NEW.before_moves_json IS OLD.before_moves_json
                    AND NEW.before_clock_json IS OLD.before_clock_json
                    AND NEW.created_at_ms IS OLD.created_at_ms
                )
                OR
                (
                    OLD.active = 1
                    AND NEW.active = 0
                    AND OLD.undone_at_ms IS NULL
                    AND NEW.undone_at_ms IS NOT NULL
                    AND NEW.id IS OLD.id
                    AND NEW.public_id IS OLD.public_id
                    AND NEW.game_id IS OLD.game_id
                    AND NEW.turn_index IS OLD.turn_index
                    AND NEW.actor IS OLD.actor
                    AND NEW.color IS OLD.color
                    AND NEW.before_fen IS OLD.before_fen
                    AND NEW.after_fen IS OLD.after_fen
                    AND NEW.before_moves_json IS OLD.before_moves_json
                    AND NEW.after_moves_json IS OLD.after_moves_json
                    AND NEW.before_clock_json IS OLD.before_clock_json
                    AND NEW.after_clock_json IS OLD.after_clock_json
                    AND NEW.completed IS OLD.completed
                    AND NEW.completed_at_ms IS OLD.completed_at_ms
                    AND NEW.created_at_ms IS OLD.created_at_ms
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid turn history update');
            END
            """,
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _migration_2(connection: sqlite3.Connection) -> None:
        """Add reclaimable command leases to schema-v1 databases."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(idempotent_commands)"
            ).fetchall()
        }
        if "lease_hash" in columns:
            return
        connection.execute(
            "ALTER TABLE idempotent_commands RENAME TO idempotent_commands_v1"
        )
        connection.execute(
            """
            CREATE TABLE idempotent_commands (
                id INTEGER PRIMARY KEY,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                idempotency_key TEXT NOT NULL
                    CHECK(length(idempotency_key) BETWEEN 8 AND 128),
                request_hash TEXT NOT NULL
                    CHECK(length(request_hash) = 64
                          AND request_hash NOT GLOB '*[^0-9a-f]*'),
                status TEXT NOT NULL
                    CHECK(status IN ('in_progress', 'completed', 'failed')),
                lease_hash BLOB
                    CHECK(lease_hash IS NULL OR
                          (typeof(lease_hash) = 'blob' AND length(lease_hash) = 32)),
                lease_expires_at_ms INTEGER,
                attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt >= 1),
                stage TEXT NOT NULL DEFAULT 'started'
                    CHECK(length(stage) BETWEEN 1 AND 64),
                stage_json TEXT NOT NULL DEFAULT '{}'
                    CHECK(length(stage_json) <= 65536),
                completed_revision INTEGER CHECK(
                    completed_revision IS NULL OR completed_revision >= 0
                ),
                response_json TEXT CHECK(response_json IS NULL
                    OR length(response_json) <= 65536),
                error_json TEXT CHECK(error_json IS NULL
                    OR length(error_json) <= 65536),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                updated_at_ms INTEGER NOT NULL CHECK(updated_at_ms >= created_at_ms),
                CHECK(
                    (status = 'in_progress'
                     AND lease_hash IS NOT NULL
                     AND lease_expires_at_ms IS NOT NULL
                     AND response_json IS NULL AND error_json IS NULL
                     AND completed_revision IS NULL)
                    OR
                    (status = 'completed'
                     AND lease_hash IS NULL AND lease_expires_at_ms IS NULL
                     AND response_json IS NOT NULL AND error_json IS NULL)
                    OR
                    (status = 'failed'
                     AND lease_hash IS NULL AND lease_expires_at_ms IS NULL
                     AND response_json IS NULL AND error_json IS NOT NULL)
                ),
                UNIQUE(game_id, idempotency_key)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO idempotent_commands(
                id, game_id, idempotency_key, request_hash, status,
                lease_hash, lease_expires_at_ms, attempt, stage, stage_json,
                completed_revision, response_json, error_json,
                created_at_ms, updated_at_ms
            )
            SELECT
                id, game_id, idempotency_key, request_hash, status,
                CASE WHEN status = 'in_progress' THEN randomblob(32) END,
                CASE WHEN status = 'in_progress' THEN 0 END,
                1, 'started', '{}', NULL, response_json, error_json,
                created_at_ms, updated_at_ms
            FROM idempotent_commands_v1
            """
        )
        connection.execute("DROP TABLE idempotent_commands_v1")
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS move_legs_second_requires_activation
            BEFORE INSERT ON move_legs
            WHEN NEW.leg_index = 2
                 AND (
                    substr(NEW.uci, -1) = '+'
                    OR NOT EXISTS (
                        SELECT 1 FROM move_legs
                        WHERE turn_id = NEW.turn_id
                          AND leg_index = 1
                          AND substr(uci, -1) = '+'
                    )
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'second leg requires a first activation leg'
                );
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS turns_shape_before_completion
            BEFORE UPDATE OF completed ON turns
            WHEN OLD.completed = 0 AND NEW.completed = 1
                 AND NOT (
                    (
                        (SELECT COUNT(*) FROM move_legs
                         WHERE turn_id = OLD.id) = 1
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 1) <> '+'
                    )
                    OR
                    (
                        (SELECT COUNT(*) FROM move_legs
                         WHERE turn_id = OLD.id) = 2
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 1) = '+'
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 2) <> '+'
                    )
                 )
            BEGIN
                SELECT RAISE(ABORT, 'invalid completed turn leg shape');
            END
            """
        )

    @staticmethod
    def _migration_3(connection: sqlite3.Connection) -> None:
        """Validate and lock down immutable turn history.

        Historical databases using the reversed activation marker cannot be
        normalized safely: changing ``+`` changes the represented game state.
        Migration therefore fails before rebuilding anything and names the
        first invalid turn for operator review.
        """

        invalid = connection.execute(
            """
            WITH shapes AS (
                SELECT
                    t.id,
                    t.public_id,
                    t.completed,
                    t.active,
                    COUNT(l.id) AS leg_count,
                    SUM(CASE WHEN l.leg_index = 1 THEN 1 ELSE 0 END)
                        AS first_count,
                    SUM(CASE WHEN l.leg_index = 2 THEN 1 ELSE 0 END)
                        AS second_count,
                    MAX(CASE
                        WHEN l.leg_index = 1 AND substr(l.uci, -1) = '+'
                        THEN 1 ELSE 0 END
                    ) AS first_plus,
                    MAX(CASE
                        WHEN l.leg_index = 2 AND substr(l.uci, -1) = '+'
                        THEN 1 ELSE 0 END
                    ) AS second_plus,
                    SUM(CASE
                        WHEN l.id IS NOT NULL AND l.active <> t.active
                        THEN 1 ELSE 0 END
                    ) AS active_mismatches
                FROM turns AS t
                LEFT JOIN move_legs AS l ON l.turn_id = t.id
                GROUP BY t.id
            )
            SELECT public_id
            FROM shapes
            WHERE
                first_count <> 1
                OR second_count NOT IN (0, 1)
                OR active_mismatches <> 0
                OR (
                    completed = 1
                    AND NOT (
                        (
                            leg_count = 1
                            AND first_plus = 0
                            AND second_count = 0
                        )
                        OR
                        (
                            leg_count = 2
                            AND first_plus = 1
                            AND second_count = 1
                            AND second_plus = 0
                        )
                    )
                )
                OR (
                    completed = 0
                    AND NOT (
                        leg_count = 1
                        AND first_plus = 1
                        AND second_count = 0
                    )
                )
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()
        if invalid is not None:
            raise GameStoreError(
                "cannot migrate game database: turn "
                f"{invalid['public_id']} has an invalid move-leg shape; "
                "manual review is required"
            )

        for trigger in (
            "turns_shape_before_completion",
            "move_legs_second_requires_activation",
            "turns_reject_precompleted_insert",
            "move_legs_update_guard",
            "turns_update_guard",
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")

        connection.execute(
            "ALTER TABLE move_legs RENAME TO move_legs_before_v3"
        )
        connection.execute(
            """
            CREATE TABLE move_legs (
                id INTEGER PRIMARY KEY,
                turn_id INTEGER NOT NULL REFERENCES turns(id) ON DELETE CASCADE,
                leg_index INTEGER NOT NULL CHECK(leg_index IN (1, 2)),
                uci TEXT NOT NULL
                    CHECK(length(uci) BETWEEN 4 AND 7)
                    CHECK(leg_index = 1 OR substr(uci, -1) <> '+'),
                before_fen TEXT NOT NULL
                    CHECK(length(before_fen) BETWEEN 1 AND 256),
                after_fen TEXT NOT NULL
                    CHECK(length(after_fen) BETWEEN 1 AND 256),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                created_at_ms INTEGER NOT NULL CHECK(created_at_ms >= 0),
                undone_at_ms INTEGER,
                CHECK((active = 1 AND undone_at_ms IS NULL)
                      OR (active = 0 AND undone_at_ms IS NOT NULL)),
                UNIQUE(turn_id, leg_index)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO move_legs(
                id, turn_id, leg_index, uci, before_fen, after_fen,
                active, created_at_ms, undone_at_ms
            )
            SELECT
                id, turn_id, leg_index, uci, before_fen, after_fen,
                active, created_at_ms, undone_at_ms
            FROM move_legs_before_v3
            ORDER BY id
            """
        )
        connection.execute("DROP TABLE move_legs_before_v3")

        trigger_statements = (
            """
            CREATE TRIGGER move_legs_second_requires_activation
            BEFORE INSERT ON move_legs
            WHEN NEW.leg_index = 2
                 AND (
                    substr(NEW.uci, -1) = '+'
                    OR NOT EXISTS (
                        SELECT 1 FROM move_legs
                        WHERE turn_id = NEW.turn_id
                          AND leg_index = 1
                          AND substr(uci, -1) = '+'
                    )
                 )
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'second leg requires a first activation leg'
                );
            END
            """,
            """
            CREATE TRIGGER turns_shape_before_completion
            BEFORE UPDATE OF completed ON turns
            WHEN OLD.completed = 0 AND NEW.completed = 1
                 AND NOT (
                    (
                        (SELECT COUNT(*) FROM move_legs
                         WHERE turn_id = OLD.id) = 1
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 1) <> '+'
                    )
                    OR
                    (
                        (SELECT COUNT(*) FROM move_legs
                         WHERE turn_id = OLD.id) = 2
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 1) = '+'
                        AND
                        (SELECT substr(uci, -1) FROM move_legs
                         WHERE turn_id = OLD.id AND leg_index = 2) <> '+'
                    )
                 )
            BEGIN
                SELECT RAISE(ABORT, 'invalid completed turn leg shape');
            END
            """,
            """
            CREATE TRIGGER turns_reject_precompleted_insert
            BEFORE INSERT ON turns
            WHEN NEW.completed <> 0
                 OR NEW.completed_at_ms IS NOT NULL
                 OR NEW.active <> 1
                 OR NEW.undone_at_ms IS NOT NULL
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'turns must be inserted as active incomplete drafts'
                );
            END
            """,
            """
            CREATE TRIGGER move_legs_update_guard
            BEFORE UPDATE ON move_legs
            WHEN NOT (
                OLD.active = 1
                AND NEW.active = 0
                AND OLD.undone_at_ms IS NULL
                AND NEW.undone_at_ms IS NOT NULL
                AND NEW.id IS OLD.id
                AND NEW.turn_id IS OLD.turn_id
                AND NEW.leg_index IS OLD.leg_index
                AND NEW.uci IS OLD.uci
                AND NEW.before_fen IS OLD.before_fen
                AND NEW.after_fen IS OLD.after_fen
                AND NEW.created_at_ms IS OLD.created_at_ms
            )
            BEGIN
                SELECT RAISE(ABORT, 'move legs are immutable after insertion');
            END
            """,
            """
            CREATE TRIGGER turns_update_guard
            BEFORE UPDATE ON turns
            WHEN NOT (
                (
                    OLD.completed = 0
                    AND NEW.completed = 1
                    AND NEW.completed_at_ms IS NOT NULL
                    AND OLD.active = 1
                    AND NEW.active = 1
                    AND OLD.undone_at_ms IS NULL
                    AND NEW.undone_at_ms IS NULL
                    AND NEW.id IS OLD.id
                    AND NEW.public_id IS OLD.public_id
                    AND NEW.game_id IS OLD.game_id
                    AND NEW.turn_index IS OLD.turn_index
                    AND NEW.actor IS OLD.actor
                    AND NEW.color IS OLD.color
                    AND NEW.before_fen IS OLD.before_fen
                    AND NEW.before_moves_json IS OLD.before_moves_json
                    AND NEW.before_clock_json IS OLD.before_clock_json
                    AND NEW.created_at_ms IS OLD.created_at_ms
                )
                OR
                (
                    OLD.active = 1
                    AND NEW.active = 0
                    AND OLD.undone_at_ms IS NULL
                    AND NEW.undone_at_ms IS NOT NULL
                    AND NEW.id IS OLD.id
                    AND NEW.public_id IS OLD.public_id
                    AND NEW.game_id IS OLD.game_id
                    AND NEW.turn_index IS OLD.turn_index
                    AND NEW.actor IS OLD.actor
                    AND NEW.color IS OLD.color
                    AND NEW.before_fen IS OLD.before_fen
                    AND NEW.after_fen IS OLD.after_fen
                    AND NEW.before_moves_json IS OLD.before_moves_json
                    AND NEW.after_moves_json IS OLD.after_moves_json
                    AND NEW.before_clock_json IS OLD.before_clock_json
                    AND NEW.after_clock_json IS OLD.after_clock_json
                    AND NEW.completed IS OLD.completed
                    AND NEW.completed_at_ms IS OLD.completed_at_ms
                    AND NEW.created_at_ms IS OLD.created_at_ms
                )
            )
            BEGIN
                SELECT RAISE(ABORT, 'invalid turn history update');
            END
            """,
        )
        for statement in trigger_statements:
            connection.execute(statement)

    @staticmethod
    def _migration_4(connection: sqlite3.Connection) -> None:
        """Persist the exact public-play temperature-decay setting."""

        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }
        if "temp_decay_moves" not in columns:
            connection.execute(
                """
                ALTER TABLE games
                ADD COLUMN temp_decay_moves INTEGER NOT NULL DEFAULT 0
                    CHECK(temp_decay_moves BETWEEN 0 AND 640)
                """
            )

        connection.execute(
            "DROP TRIGGER IF EXISTS games_immutable_configuration"
        )
        connection.execute(
            """
            CREATE TRIGGER games_immutable_configuration
            BEFORE UPDATE OF
                public_id, start_fen, human_color, engine_color,
                model_id, model_sha256, preset, nodes, temperature,
                temp_decay_moves, backend,
                clock_mode, clock_initial_ms, clock_increment_ms,
                publish_on_completion, created_at_ms
            ON games
            WHEN
                NEW.public_id IS NOT OLD.public_id
                OR NEW.start_fen IS NOT OLD.start_fen
                OR NEW.human_color IS NOT OLD.human_color
                OR NEW.engine_color IS NOT OLD.engine_color
                OR NEW.model_id IS NOT OLD.model_id
                OR NEW.model_sha256 IS NOT OLD.model_sha256
                OR NEW.preset IS NOT OLD.preset
                OR NEW.nodes IS NOT OLD.nodes
                OR NEW.temperature IS NOT OLD.temperature
                OR NEW.temp_decay_moves IS NOT OLD.temp_decay_moves
                OR NEW.backend IS NOT OLD.backend
                OR NEW.clock_mode IS NOT OLD.clock_mode
                OR NEW.clock_initial_ms IS NOT OLD.clock_initial_ms
                OR NEW.clock_increment_ms IS NOT OLD.clock_increment_ms
                OR NEW.publish_on_completion IS NOT OLD.publish_on_completion
                OR NEW.created_at_ms IS NOT OLD.created_at_ms
            BEGIN
                SELECT RAISE(ABORT, 'immutable game configuration');
            END
            """
        )

        # Schema-v3 creation retries cached the projected game before this
        # field existed. Keep those durable responses shape-compatible after
        # upgrading without rewriting append-only historical events.
        rows = connection.execute(
            """
            SELECT id, response_json
            FROM session_commands
            WHERE status = 'completed' AND response_json IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            response = _decode_json(row["response_json"])
            if (
                isinstance(response, dict)
                and "temp_decay_moves" not in response
            ):
                response["temp_decay_moves"] = 0
                connection.execute(
                    """
                    UPDATE session_commands SET response_json = ?
                    WHERE id = ?
                    """,
                    (
                        _canonical_json(
                            response, label="cached game creation response"
                        ),
                        row["id"],
                    ),
                )

    # -- sessions ---------------------------------------------------------

    def create_session(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create an anonymous session and return its one-time bearer token.

        ``token`` exists for deterministic integrations and tests.  Production
        callers should omit it and use the cryptographically random value
        returned by this method.  CSRF values are deliberately outside the
        repository: the HTTP service should derive one from this high-entropy
        bearer token and its server secret.  No raw CSRF secret is persisted.
        """

        lifetime = self.session_ttl_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(lifetime, bool) or not isinstance(lifetime, int):
            raise ValidationError("ttl_seconds must be an integer")
        if not 60 <= lifetime <= 365 * 24 * 60 * 60:
            raise ValidationError("ttl_seconds must be between 60 and 31536000")
        raw_token = secrets.token_urlsafe(32) if token is None else token
        token_hash = _hash_token(raw_token)
        now = self._now_ms()
        expires = now + lifetime * 1000
        with self._write() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO sessions(
                        token_hash, created_at_ms, last_seen_at_ms, expires_at_ms
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (token_hash, now, now, expires),
                )
            except sqlite3.IntegrityError as exc:
                raise ValidationError("session token is already registered") from exc
        return {
            "token": raw_token,
            "created_at_ms": now,
            "last_seen_at_ms": now,
            "expires_at_ms": expires,
        }

    @staticmethod
    def _session_projection(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "created_at_ms": row["created_at_ms"],
            "last_seen_at_ms": row["last_seen_at_ms"],
            "expires_at_ms": row["expires_at_ms"],
        }

    def lookup_session(self, token: str) -> Optional[dict[str, Any]]:
        token_hash = _hash_token(token)
        now = self._now_ms()
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT created_at_ms, last_seen_at_ms, expires_at_ms
                FROM sessions
                WHERE token_hash = ?
                  AND revoked_at_ms IS NULL
                  AND expires_at_ms > ?
                """,
                (token_hash, now),
            ).fetchone()
        return None if row is None else self._session_projection(row)

    def touch_session(
        self,
        token: str,
        *,
        ttl_seconds: Optional[int] = None,
    ) -> dict[str, Any]:
        token_hash = _hash_token(token)
        lifetime = self.session_ttl_seconds if ttl_seconds is None else ttl_seconds
        if isinstance(lifetime, bool) or not isinstance(lifetime, int):
            raise ValidationError("ttl_seconds must be an integer")
        if not 60 <= lifetime <= 365 * 24 * 60 * 60:
            raise ValidationError("ttl_seconds must be between 60 and 31536000")
        now = self._now_ms()
        expires = now + lifetime * 1000
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET last_seen_at_ms = ?, expires_at_ms = ?
                WHERE token_hash = ?
                  AND revoked_at_ms IS NULL
                  AND expires_at_ms > ?
                """,
                (now, expires, token_hash, now),
            )
            if cursor.rowcount != 1:
                raise NotFoundError("anonymous session is missing or expired")
            row = connection.execute(
                """
                SELECT created_at_ms, last_seen_at_ms, expires_at_ms
                FROM sessions WHERE token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
        return self._session_projection(row)

    def revoke_session(self, token: str) -> bool:
        """Revoke a bearer token without deleting its games."""

        token_hash = _hash_token(token)
        now = self._now_ms()
        with self._write() as connection:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET revoked_at_ms = ?
                WHERE token_hash = ? AND revoked_at_ms IS NULL
                """,
                (now, token_hash),
            )
        return cursor.rowcount == 1

    @staticmethod
    def _require_live_session(
        connection: sqlite3.Connection,
        token_hash: bytes,
        now: int,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT id, created_at_ms, last_seen_at_ms, expires_at_ms
            FROM sessions
            WHERE token_hash = ?
              AND revoked_at_ms IS NULL
              AND expires_at_ms > ?
            """,
            (token_hash, now),
        ).fetchone()
        if row is None:
            raise OwnershipError("anonymous session is missing or expired")
        return row

    # -- validation/projection helpers -----------------------------------

    @staticmethod
    def _validate_clock_settings(clock: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(clock, Mapping):
            raise ValidationError("clock must be an object")
        mode = clock.get("mode")
        if mode == "unlimited":
            extra = set(clock) - {"mode"}
            if extra:
                raise ValidationError(
                    "unlimited clock must not include timed settings"
                )
            return {"mode": "unlimited", "initial_ms": None, "increment_ms": 0}
        if mode != "timed":
            raise ValidationError("clock mode must be 'unlimited' or 'timed'")
        allowed = {"mode", "initial_ms", "increment_ms"}
        if set(clock) - allowed:
            raise ValidationError("clock contains unsupported settings")
        initial = clock.get("initial_ms")
        increment = clock.get("increment_ms")
        if (
            isinstance(initial, bool)
            or not isinstance(initial, int)
            or not 1000 <= initial <= 24 * 60 * 60 * 1000
        ):
            raise ValidationError("clock initial_ms is outside the supported range")
        if (
            isinstance(increment, bool)
            or not isinstance(increment, int)
            or not 0 <= increment <= 60 * 60 * 1000
        ):
            raise ValidationError(
                "clock increment_ms is outside the supported range"
            )
        return {
            "mode": "timed",
            "initial_ms": initial,
            "increment_ms": increment,
        }

    @staticmethod
    def _validate_clock_state(
        settings: Mapping[str, Any],
        state: Optional[Mapping[str, Any]],
        now: int,
    ) -> dict[str, Any]:
        if settings["mode"] == "unlimited":
            canonical_unlimited = {
                "white_ms": None,
                "black_ms": None,
                "running_color": None,
                "anchor_at_ms": None,
            }
            if state not in (
                None,
                {},
                {"running_color": None},
                canonical_unlimited,
            ):
                raise ValidationError("unlimited games cannot have clock state")
            return canonical_unlimited
        if state is None:
            return {
                "white_ms": settings["initial_ms"],
                "black_ms": settings["initial_ms"],
                "running_color": "white",
                "anchor_at_ms": now,
            }
        if not isinstance(state, Mapping):
            raise ValidationError("clock_state must be an object")
        allowed = {"white_ms", "black_ms", "running_color", "anchor_at_ms"}
        if set(state) - allowed:
            raise ValidationError("clock_state contains unsupported fields")
        white = state.get("white_ms")
        black = state.get("black_ms")
        for value, label in ((white, "white_ms"), (black, "black_ms")):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= MAX_CLOCK_MS
            ):
                raise ValidationError(f"clock_state {label} is invalid")
        running = state.get("running_color")
        if running is not None:
            _validate_color(running, "clock_state running_color")
        anchor = state.get("anchor_at_ms")
        if running is None:
            if anchor is not None:
                raise ValidationError(
                    "paused clock_state must have a null anchor_at_ms"
                )
        elif isinstance(anchor, bool) or not isinstance(anchor, int) or anchor < 0:
            raise ValidationError(
                "running clock_state must have a non-negative anchor_at_ms"
            )
        return {
            "white_ms": white,
            "black_ms": black,
            "running_color": running,
            "anchor_at_ms": anchor,
        }

    @staticmethod
    def _clock_settings_from_row(row: sqlite3.Row) -> dict[str, Any]:
        if row["clock_mode"] == "unlimited":
            return {"mode": "unlimited"}
        return {
            "mode": "timed",
            "initial_ms": row["clock_initial_ms"],
            "increment_ms": row["clock_increment_ms"],
        }

    @staticmethod
    def _clock_state_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "white_ms": row["white_time_ms"],
            "black_ms": row["black_time_ms"],
            "running_color": row["clock_running_color"],
            "anchor_at_ms": row["clock_anchor_at_ms"],
        }

    @staticmethod
    def _clock_columns(state: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            state["white_ms"],
            state["black_ms"],
            state["running_color"],
            state["anchor_at_ms"],
        )

    @staticmethod
    def _clock_at(
        game: sqlite3.Row, at_ms: int
    ) -> tuple[dict[str, Any], Optional[str]]:
        """Settle the persisted running clock at one server timestamp."""

        state = GameStore._clock_state_from_row(game)
        if game["clock_mode"] != "timed":
            return state, None
        running = state["running_color"]
        if running is None:
            return state, None
        anchor = state["anchor_at_ms"]
        if anchor is None:
            raise GameStoreError("running clock is missing its anchor")
        remaining_key = f"{running}_ms"
        remaining = state[remaining_key]
        if remaining is None:
            raise GameStoreError("running clock is missing its remaining time")
        elapsed_ms = max(0, at_ms - anchor)
        state[remaining_key] = max(0, remaining - elapsed_ms)
        state["anchor_at_ms"] = at_ms
        expired = running if state[remaining_key] == 0 else None
        return state, expired

    @staticmethod
    def _validate_moves(moves: Optional[Sequence[str]]) -> list[str]:
        if moves is None:
            return []
        if isinstance(moves, (str, bytes)) or not isinstance(moves, Sequence):
            raise ValidationError("moves must be a sequence")
        if len(moves) > MAX_MOVES:
            raise ValidationError(f"moves exceed the {MAX_MOVES}-leg limit")
        return [_validate_move(move) for move in moves]

    @staticmethod
    def _validate_actor(actor: Any) -> str:
        if actor not in ("human", "engine"):
            raise ValidationError("actor must be 'human' or 'engine'")
        return actor

    def _normalize_mutation_command(
        self,
        *,
        idempotency_key: Optional[str],
        command_lease: Optional[str],
        command_stage: Optional[str],
        command_complete: bool,
    ) -> Optional[tuple[str, str, Optional[str], bool]]:
        if not isinstance(command_complete, bool):
            raise ValidationError("command_complete must be a boolean")
        if idempotency_key is None:
            if (
                command_lease is not None
                or command_stage is not None
                or command_complete
            ):
                raise ValidationError(
                    "command options require an idempotency_key"
                )
            return None
        key = self._validate_idempotency_key(idempotency_key)
        if command_lease is None:
            raise ValidationError(
                "command_lease is required with idempotency_key"
            )
        self._lease_hash(command_lease)
        stage = None
        if not command_complete:
            if command_stage is None:
                raise ValidationError(
                    "command_stage is required until a command is completed"
                )
            stage = self._validate_command_stage(command_stage)
        elif command_stage is not None:
            raise ValidationError(
                "command_stage must be omitted when command_complete is true"
            )
        return key, command_lease, stage, command_complete

    @staticmethod
    def _command_binding_at_stage(
        binding: Optional[tuple[str, str, Optional[str], bool]],
        stage: str,
    ) -> Optional[tuple[str, str, Optional[str], bool]]:
        if binding is None or binding[3]:
            return binding
        return binding[0], binding[1], stage, False

    def _apply_mutation_command(
        self,
        connection: sqlite3.Connection,
        *,
        game_id: int,
        game_revision: int,
        binding: Optional[tuple[str, str, Optional[str], bool]],
        result: Mapping[str, Any],
        stage_data: Mapping[str, Any],
        now: int,
    ) -> None:
        if binding is None:
            return
        key, lease_token, stage, should_complete = binding
        if should_complete:
            self._complete_command_in_transaction(
                connection,
                game_id=game_id,
                game_revision=game_revision,
                idempotency_key=key,
                lease_token=lease_token,
                response=result,
                now=now,
            )
        else:
            self._advance_command_in_transaction(
                connection,
                game_id=game_id,
                idempotency_key=key,
                lease_token=lease_token,
                stage=stage,
                stage_data=stage_data,
                now=now,
            )

    @staticmethod
    def _validate_event_type(event_type: Any) -> str:
        value = _require_string(
            event_type, "event_type", maximum=MAX_EVENT_TYPE_LENGTH
        )
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
            raise ValidationError("event_type must be lowercase snake_case")
        return value

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        game_id: int,
        revision: int,
        event_type: str,
        actor: str,
        payload: Any,
        now: int,
    ) -> None:
        event_type = GameStore._validate_event_type(event_type)
        if actor not in ("human", "engine", "system"):
            raise ValidationError("event actor is invalid")
        _validate_payload_keys(payload)
        encoded = _canonical_json(payload, label="event payload")
        next_index = connection.execute(
            """
            SELECT COALESCE(MAX(event_index), -1) + 1
            FROM game_events WHERE game_id = ?
            """,
            (game_id,),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO game_events(
                game_id, event_index, revision, event_type, actor,
                payload_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                next_index,
                revision,
                event_type,
                actor,
                encoded,
                now,
            ),
        )

    @staticmethod
    def _game_row_for_owner(
        connection: sqlite3.Connection,
        *,
        public_id: str,
        token_hash: bytes,
        now: int,
    ) -> sqlite3.Row:
        session = GameStore._require_live_session(connection, token_hash, now)
        row = connection.execute(
            "SELECT * FROM games WHERE public_id = ?",
            (public_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("game does not exist")
        if row["owner_session_id"] != session["id"]:
            raise OwnershipError("anonymous session does not own this game")
        return row

    @staticmethod
    def _check_active_revision(row: sqlite3.Row, expected_revision: int) -> None:
        if row["status"] != "active":
            raise InvalidTransition(
                f"game is {row['status']}; only active games may change"
            )
        if row["revision"] != expected_revision:
            raise RevisionConflict(expected_revision, row["revision"])

    @staticmethod
    def _game_projection(
        row: sqlite3.Row,
        *,
        owner: bool,
        summary: bool = False,
    ) -> dict[str, Any]:
        game = {
            "id": row["public_id"],
            "status": row["status"],
            "human_color": row["human_color"],
            "engine_color": row["engine_color"],
            "model": {
                "id": row["model_id"],
                "sha256": row["model_sha256"],
            },
            "preset": row["preset"],
            "nodes": row["nodes"],
            "temperature": row["temperature"],
            "temp_decay_moves": row["temp_decay_moves"],
            "backend": row["backend"],
            "clock": GameStore._clock_settings_from_row(row),
            "revision": row["revision"],
            "result": row["result"],
            "termination": row["termination"],
            "takeback_count": row["takeback_count"],
            "created_at_ms": row["created_at_ms"],
            "updated_at_ms": row["updated_at_ms"],
            "completed_at_ms": row["completed_at_ms"],
        }
        if owner:
            game["player_name"] = row["player_name"]
            game["name_visible"] = bool(row["name_visible"])
            game["publish_on_completion"] = bool(row["publish_on_completion"])
            game["public_visible"] = bool(row["public_visible"])
        else:
            game["name_visible"] = bool(row["name_visible"])
            if row["name_visible"] and row["player_name"]:
                game["player_name"] = row["player_name"]
        if summary:
            game["final_fen"] = row["current_fen"]
        else:
            game.update(
                {
                    "start_fen": row["start_fen"],
                    "current_fen": row["current_fen"],
                    "active_moves": _decode_json(
                        row["active_moves_json"], fallback=[]
                    ),
                    "clock_state": GameStore._clock_state_from_row(row),
                }
            )
        return game

    # -- games and history ------------------------------------------------

    def create_game(
        self,
        owner_token: str,
        *,
        start_fen: str,
        human_color: str,
        model_id: str,
        model_sha256: str,
        preset: str,
        nodes: int,
        temperature: float,
        backend: str,
        clock: Mapping[str, Any],
        temp_decay_moves: int = 0,
        clock_state: Optional[Mapping[str, Any]] = None,
        initial_moves: Optional[Sequence[str]] = None,
        player_name: Optional[str] = None,
        name_visible: bool = False,
        publish_on_completion: bool = True,
        public_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        """Atomically create an active game and its initial audit event.

        When ``idempotency_key`` is supplied, the normalized creation request,
        game insertion, and cached response are committed in one transaction.
        A retry after process restart returns that exact cached response; reuse
        of the key with any changed option raises :class:`IdempotencyConflict`.
        """

        token_hash = _hash_token(owner_token)
        start_fen = _validate_fen(start_fen, "start_fen")
        human_color = _validate_color(human_color, "human_color")
        model_id = _validate_label(
            model_id, "model_id", maximum=MAX_MODEL_LENGTH
        )
        if (
            not isinstance(model_sha256, str)
            or not _SHA256_RE.fullmatch(model_sha256)
        ):
            raise ValidationError(
                "model_sha256 must be exactly 64 lowercase hexadecimal characters"
            )
        preset = _validate_label(preset, "preset", maximum=MAX_PRESET_LENGTH)
        backend = _validate_label(
            backend, "backend", maximum=MAX_BACKEND_LENGTH
        )
        if isinstance(nodes, bool) or not isinstance(nodes, int):
            raise ValidationError("nodes must be an integer")
        if not 0 <= nodes <= 1_000_000_000:
            raise ValidationError("nodes is outside the supported range")
        if isinstance(temperature, bool) or not isinstance(
            temperature, (int, float)
        ):
            raise ValidationError("temperature must be a number")
        temperature = float(temperature)
        if not 0.0 <= temperature <= 10.0:
            raise ValidationError("temperature is outside the supported range")
        if (
            isinstance(temp_decay_moves, bool)
            or not isinstance(temp_decay_moves, int)
        ):
            raise ValidationError("temp_decay_moves must be an integer")
        if not 0 <= temp_decay_moves <= 640:
            raise ValidationError(
                "temp_decay_moves is outside the supported range"
            )
        settings = self._validate_clock_settings(clock)
        now = self._now_ms()
        state = self._validate_clock_state(settings, clock_state, now)
        moves = self._validate_moves(initial_moves)
        moves_json = _canonical_json(moves, label="initial moves")
        if player_name is not None:
            player_name = _require_string(
                player_name, "player_name", maximum=MAX_NAME_LENGTH
            ).strip()
            if not player_name:
                raise ValidationError("player_name must not be blank")
        if not isinstance(name_visible, bool):
            raise ValidationError("name_visible must be a boolean")
        if name_visible and player_name is None:
            raise ValidationError("visible player name is missing")
        if not isinstance(publish_on_completion, bool):
            raise ValidationError("publish_on_completion must be a boolean")
        requested_public_id = (
            None if public_id is None else _validate_public_id(public_id)
        )
        game_public_id = requested_public_id or uuid.uuid4().hex
        command_request_hash = None
        if idempotency_key is not None:
            idempotency_key = self._validate_idempotency_key(idempotency_key)
            command_request = {
                "operation": "create_game",
                "start_fen": start_fen,
                "human_color": human_color,
                "model_id": model_id,
                "model_sha256": model_sha256,
                "preset": preset,
                "nodes": nodes,
                "temperature": temperature,
                "backend": backend,
                "clock": settings,
                "clock_state": (
                    None if clock_state is None else state
                ),
                "initial_moves": moves,
                "player_name": player_name,
                "name_visible": name_visible,
                "publish_on_completion": publish_on_completion,
                "requested_public_id": requested_public_id,
            }
            # Omitting the historical default keeps schema-v3 durable create
            # retries compatible after migration.
            if temp_decay_moves:
                command_request["temp_decay_moves"] = temp_decay_moves
            command_request_hash, _ = _request_hash(command_request)

        with self._write() as connection:
            session = self._require_live_session(connection, token_hash, now)
            if idempotency_key is not None:
                existing_command = connection.execute(
                    """
                    SELECT * FROM session_commands
                    WHERE session_id = ? AND idempotency_key = ?
                    """,
                    (session["id"], idempotency_key),
                ).fetchone()
                if existing_command is not None:
                    if existing_command["request_hash"] != command_request_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used for another "
                            "game creation request"
                        )
                    if existing_command["status"] == "completed":
                        return _decode_json(existing_command["response_json"])
                    raise InvalidTransition(
                        "game creation command is still in progress"
                    )
            active_game = connection.execute(
                """
                SELECT public_id FROM games
                WHERE owner_session_id = ? AND status = 'active'
                """,
                (session["id"],),
            ).fetchone()
            if active_game is not None:
                raise InvalidTransition(
                    "anonymous session already owns active game "
                    f"{active_game['public_id']}"
                )
            if idempotency_key is not None:
                connection.execute(
                    """
                    INSERT INTO session_commands(
                        session_id, idempotency_key, request_hash, status,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, 'in_progress', ?, ?)
                    """,
                    (
                        session["id"],
                        idempotency_key,
                        command_request_hash,
                        now,
                        now,
                    ),
                )
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO games(
                        public_id, owner_session_id, status,
                        start_fen, current_fen, active_moves_json,
                        human_color, engine_color,
                        model_id, model_sha256, preset, nodes, temperature,
                        temp_decay_moves, backend, clock_mode, clock_initial_ms,
                        clock_increment_ms, white_time_ms, black_time_ms,
                        clock_running_color, clock_anchor_at_ms,
                        revision, player_name, name_visible,
                        publish_on_completion, public_visible,
                        takeback_count, created_at_ms, updated_at_ms
                    ) VALUES (
                        ?, ?, 'active',
                        ?, ?, ?,
                        ?, ?,
                        ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?, ?,
                        0, ?, ?, ?, 0, 0, ?, ?
                    )
                    """,
                    (
                        game_public_id,
                        session["id"],
                        start_fen,
                        start_fen,
                        moves_json,
                        human_color,
                        _other_color(human_color),
                        model_id,
                        model_sha256,
                        preset,
                        nodes,
                        temperature,
                        temp_decay_moves,
                        backend,
                        settings["mode"],
                        settings["initial_ms"],
                        settings["increment_ms"],
                        *self._clock_columns(state),
                        player_name,
                        int(name_visible),
                        int(publish_on_completion),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                active_game = connection.execute(
                    """
                    SELECT public_id FROM games
                    WHERE owner_session_id = ? AND status = 'active'
                    """,
                    (session["id"],),
                ).fetchone()
                if active_game is not None:
                    raise InvalidTransition(
                        "anonymous session already owns active game "
                        f"{active_game['public_id']}"
                    ) from exc
                if public_id is not None:
                    raise ValidationError("game id is already registered") from exc
                raise
            game_id = cursor.lastrowid
            self._insert_event(
                connection,
                game_id=game_id,
                revision=0,
                event_type="game_created",
                actor="system",
                payload={
                    "human_color": human_color,
                    "model_sha256": model_sha256,
                    "preset": preset,
                    "nodes": nodes,
                    "temperature": temperature,
                    "temp_decay_moves": temp_decay_moves,
                    "backend": backend,
                    "clock": (
                        {"mode": "unlimited"}
                        if settings["mode"] == "unlimited"
                        else {
                            "mode": "timed",
                            "initial_ms": settings["initial_ms"],
                            "increment_ms": settings["increment_ms"],
                        }
                    ),
                },
                now=now,
            )
            connection.execute(
                """
                UPDATE sessions SET last_seen_at_ms = ?
                WHERE id = ? AND last_seen_at_ms < ?
                """,
                (now, session["id"], now),
            )
            row = connection.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()
            result = self._game_projection(row, owner=True)
            if idempotency_key is not None:
                response_json = _canonical_json(
                    result, label="game creation response"
                )
                connection.execute(
                    """
                    UPDATE session_commands
                    SET status = 'completed', game_id = ?,
                        response_json = ?, updated_at_ms = ?
                    WHERE session_id = ? AND idempotency_key = ?
                      AND status = 'in_progress'
                    """,
                    (
                        game_id,
                        response_json,
                        now,
                        session["id"],
                        idempotency_key,
                    ),
                )
        return result

    def get_game_for_owner(
        self,
        owner_token: str,
        public_id: str,
        *,
        include_history: bool = False,
    ) -> dict[str, Any]:
        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        now = self._now_ms()
        with self._read() as connection:
            row = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            result = self._game_projection(row, owner=True)
            if include_history:
                result["turns"] = self._turns_projection(
                    connection, row["id"]
                )
                result["events"] = self._events_projection(
                    connection, row["id"]
                )
        return result

    def get_active_game_for_owner(
        self, owner_token: str
    ) -> Optional[dict[str, Any]]:
        """Return the session's resumable active game, if one exists."""

        token_hash = _hash_token(owner_token)
        now = self._now_ms()
        with self._read() as connection:
            session = self._require_live_session(connection, token_hash, now)
            row = connection.execute(
                """
                SELECT * FROM games
                WHERE owner_session_id = ? AND status = 'active'
                """,
                (session["id"],),
            ).fetchone()
        return None if row is None else self._game_projection(row, owner=True)

    def get_public_game(
        self,
        public_id: str,
        *,
        include_history: bool = True,
    ) -> dict[str, Any]:
        public_id = _validate_public_id(public_id)
        with self._read() as connection:
            row = connection.execute(
                """
                SELECT * FROM games
                WHERE public_id = ?
                  AND status = 'completed'
                  AND public_visible = 1
                """,
                (public_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("completed public game does not exist")
            result = self._game_projection(row, owner=False)
            if include_history:
                result["turns"] = self._turns_projection(
                    connection, row["id"]
                )
                result["events"] = self._events_projection(
                    connection, row["id"]
                )
        return result

    @staticmethod
    def _turns_projection(
        connection: sqlite3.Connection, game_id: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT * FROM turns
            WHERE game_id = ?
            ORDER BY turn_index
            """,
            (game_id,),
        ).fetchall()
        turns: list[dict[str, Any]] = []
        for row in rows:
            legs = connection.execute(
                """
                SELECT leg_index, uci, before_fen, after_fen,
                       active, created_at_ms, undone_at_ms
                FROM move_legs
                WHERE turn_id = ?
                ORDER BY leg_index
                """,
                (row["id"],),
            ).fetchall()
            turns.append(
                {
                    "id": row["public_id"],
                    "index": row["turn_index"],
                    "actor": row["actor"],
                    "color": row["color"],
                    "before_fen": row["before_fen"],
                    "after_fen": row["after_fen"],
                    "before_moves": _decode_json(row["before_moves_json"], []),
                    "after_moves": _decode_json(row["after_moves_json"], []),
                    "before_clock": _decode_json(row["before_clock_json"], {}),
                    "after_clock": _decode_json(row["after_clock_json"], {}),
                    "completed": bool(row["completed"]),
                    "active": bool(row["active"]),
                    "created_at_ms": row["created_at_ms"],
                    "completed_at_ms": row["completed_at_ms"],
                    "undone_at_ms": row["undone_at_ms"],
                    "legs": [
                        {
                            "index": leg["leg_index"],
                            "uci": leg["uci"],
                            "before_fen": leg["before_fen"],
                            "after_fen": leg["after_fen"],
                            "active": bool(leg["active"]),
                            "created_at_ms": leg["created_at_ms"],
                            "undone_at_ms": leg["undone_at_ms"],
                        }
                        for leg in legs
                    ],
                }
            )
        return turns

    @staticmethod
    def _events_projection(
        connection: sqlite3.Connection, game_id: int
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT event_index, revision, event_type, actor,
                   payload_json, created_at_ms
            FROM game_events
            WHERE game_id = ?
            ORDER BY event_index
            """,
            (game_id,),
        ).fetchall()
        return [
            {
                "index": row["event_index"],
                "revision": row["revision"],
                "type": row["event_type"],
                "actor": row["actor"],
                "payload": _decode_json(row["payload_json"], {}),
                "created_at_ms": row["created_at_ms"],
            }
            for row in rows
        ]

    @staticmethod
    def _validated_legs(
        legs: Sequence[Mapping[str, Any]],
        before_fen: str,
        *,
        complete: bool,
    ) -> tuple[list[dict[str, str]], str]:
        if isinstance(legs, (str, bytes)) or not isinstance(legs, Sequence):
            raise ValidationError("legs must be a sequence")
        if not 1 <= len(legs) <= 2:
            raise ValidationError("a turn must contain one or two move legs")
        normalized: list[dict[str, str]] = []
        expected_before = before_fen
        for index, leg in enumerate(legs, start=1):
            if not isinstance(leg, Mapping):
                raise ValidationError("each move leg must be an object")
            if set(leg) - {"uci", "after_fen"}:
                raise ValidationError("move leg contains unsupported fields")
            uci = _validate_move(leg.get("uci"))
            after_fen = _validate_fen(
                leg.get("after_fen"), f"leg {index} after_fen"
            )
            normalized.append(
                {
                    "uci": uci,
                    "before_fen": expected_before,
                    "after_fen": after_fen,
                }
            )
            expected_before = after_fen
        first_activates = normalized[0]["uci"].endswith("+")
        if len(normalized) == 2:
            if not first_activates:
                raise ValidationError(
                    "the first leg of a double move must end with '+'"
                )
            if normalized[1]["uci"].endswith("+"):
                raise ValidationError(
                    "the second leg of a double move must not end with '+'"
                )
        elif complete and first_activates:
            raise ValidationError(
                "a completed one-leg turn must not activate a second move"
            )
        elif not complete and not first_activates:
            raise ValidationError(
                "an unfinished first leg must end with '+'"
            )
        return normalized, expected_before

    def record_turn(
        self,
        owner_token: str,
        public_id: str,
        *,
        expected_revision: int,
        actor: str,
        color: str,
        legs: Sequence[Mapping[str, Any]],
        clock_state: Mapping[str, Any],
        complete: bool = True,
        idempotency_key: Optional[str] = None,
        command_lease: Optional[str] = None,
        command_stage: Optional[str] = None,
        command_complete: bool = False,
    ) -> dict[str, Any]:
        """Record one complete turn, or the first leg of a human turn.

        Each leg contains ``uci`` and its authoritative ``after_fen``.  The
        repository derives all before-state and move-history fields from the
        current game row.  Incomplete turns are restricted to one human leg
        and can only be finished with :meth:`append_turn_leg`.
        """

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        expected_revision = _validate_revision(expected_revision)
        actor = self._validate_actor(actor)
        color = _validate_color(color)
        if not isinstance(complete, bool):
            raise ValidationError("complete must be a boolean")
        command_binding = self._normalize_mutation_command(
            idempotency_key=idempotency_key,
            command_lease=command_lease,
            command_stage=command_stage,
            command_complete=command_complete,
        )
        now = self._now_ms()

        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            self._check_active_revision(game, expected_revision)
            if command_binding is not None:
                self._require_command_lease(
                    connection,
                    game_id=game["id"],
                    idempotency_key=command_binding[0],
                    lease_token=command_binding[1],
                    now=now,
                )
            expected_color = (
                game["human_color"] if actor == "human" else game["engine_color"]
            )
            if color != expected_color:
                raise InvalidTransition(
                    f"{actor} must play {expected_color}, not {color}"
                )
            if not complete and actor != "human":
                raise InvalidTransition("only a human turn may remain unfinished")
            incomplete = connection.execute(
                """
                SELECT public_id FROM turns
                WHERE game_id = ? AND active = 1 AND completed = 0
                """,
                (game["id"],),
            ).fetchone()
            if incomplete is not None:
                raise InvalidTransition(
                    "finish or take back the unfinished human turn first"
                )

            normalized_legs, after_fen = self._validated_legs(
                legs, game["current_fen"], complete=complete
            )
            if not complete and len(normalized_legs) != 1:
                raise ValidationError(
                    "an unfinished turn must contain exactly one leg"
                )
            settings = self._clock_settings_from_row(game)
            after_clock = self._validate_clock_state(settings, clock_state, now)
            before_clock = self._clock_state_from_row(game)
            before_moves = _decode_json(game["active_moves_json"], [])
            after_moves = before_moves + [
                leg["uci"] for leg in normalized_legs
            ]
            if len(after_moves) > MAX_MOVES:
                raise InvalidTransition("game move history reached its limit")
            before_moves_json = _canonical_json(
                before_moves, label="before moves"
            )
            after_moves_json = _canonical_json(after_moves, label="after moves")
            before_clock_json = _canonical_json(
                before_clock, label="before clock"
            )
            after_clock_json = _canonical_json(
                after_clock, label="after clock"
            )
            turn_index = connection.execute(
                """
                SELECT COALESCE(MAX(turn_index), -1) + 1
                FROM turns WHERE game_id = ?
                """,
                (game["id"],),
            ).fetchone()[0]
            turn_public_id = uuid.uuid4().hex
            cursor = connection.execute(
                """
                INSERT INTO turns(
                    public_id, game_id, turn_index, actor, color,
                    before_fen, after_fen,
                    before_moves_json, after_moves_json,
                    before_clock_json, after_clock_json,
                    completed, active, created_at_ms, completed_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    turn_public_id,
                    game["id"],
                    turn_index,
                    actor,
                    color,
                    game["current_fen"],
                    after_fen,
                    before_moves_json,
                    after_moves_json,
                    before_clock_json,
                    after_clock_json,
                    0,
                    now,
                    None,
                ),
            )
            turn_id = cursor.lastrowid
            for leg_index, leg in enumerate(normalized_legs, start=1):
                connection.execute(
                    """
                    INSERT INTO move_legs(
                        turn_id, leg_index, uci, before_fen, after_fen,
                        active, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        turn_id,
                        leg_index,
                        leg["uci"],
                        leg["before_fen"],
                        leg["after_fen"],
                        now,
                    ),
                )
            if complete:
                connection.execute(
                    """
                    UPDATE turns
                    SET completed = 1, completed_at_ms = ?
                    WHERE id = ? AND completed = 0
                    """,
                    (now, turn_id),
                )
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE games
                SET current_fen = ?, active_moves_json = ?,
                    white_time_ms = ?, black_time_ms = ?,
                    clock_running_color = ?, clock_anchor_at_ms = ?,
                    revision = ?, updated_at_ms = ?
                WHERE id = ? AND status = 'active' AND revision = ?
                """,
                (
                    after_fen,
                    after_moves_json,
                    *self._clock_columns(after_clock),
                    new_revision,
                    now,
                    game["id"],
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                current = connection.execute(
                    "SELECT revision FROM games WHERE id = ?", (game["id"],)
                ).fetchone()[0]
                raise RevisionConflict(expected_revision, current)
            self._insert_event(
                connection,
                game_id=game["id"],
                revision=new_revision,
                event_type="turn_recorded" if complete else "turn_started",
                actor=actor,
                payload={
                    "turn_id": turn_public_id,
                    "color": color,
                    "legs": [leg["uci"] for leg in normalized_legs],
                    "complete": complete,
                },
                now=now,
            )
            updated = connection.execute(
                "SELECT * FROM games WHERE id = ?", (game["id"],)
            ).fetchone()
            result = self._game_projection(updated, owner=True)
            result["recorded_turn_id"] = turn_public_id
            self._apply_mutation_command(
                connection,
                game_id=game["id"],
                game_revision=new_revision,
                binding=command_binding,
                result=result,
                stage_data={
                    "revision": new_revision,
                    "recorded_turn_id": turn_public_id,
                    "actor": actor,
                    "turn_complete": complete,
                },
                now=now,
            )
        return result

    def append_turn_leg(
        self,
        owner_token: str,
        public_id: str,
        turn_public_id: str,
        *,
        expected_revision: int,
        uci: str,
        after_fen: str,
        clock_state: Mapping[str, Any],
        idempotency_key: Optional[str] = None,
        command_lease: Optional[str] = None,
        command_stage: Optional[str] = None,
        command_complete: bool = False,
    ) -> dict[str, Any]:
        """Append the second leg and complete an unfinished human turn."""

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        turn_public_id = _validate_public_id(turn_public_id, "turn id")
        expected_revision = _validate_revision(expected_revision)
        uci = _validate_move(uci)
        if uci.endswith("+"):
            raise ValidationError(
                "the second leg of a double move must not end with '+'"
            )
        after_fen = _validate_fen(after_fen, "after_fen")
        command_binding = self._normalize_mutation_command(
            idempotency_key=idempotency_key,
            command_lease=command_lease,
            command_stage=command_stage,
            command_complete=command_complete,
        )
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            self._check_active_revision(game, expected_revision)
            if command_binding is not None:
                self._require_command_lease(
                    connection,
                    game_id=game["id"],
                    idempotency_key=command_binding[0],
                    lease_token=command_binding[1],
                    now=now,
                )
            turn = connection.execute(
                """
                SELECT * FROM turns
                WHERE public_id = ? AND game_id = ?
                """,
                (turn_public_id, game["id"]),
            ).fetchone()
            if turn is None:
                raise NotFoundError("turn does not exist in this game")
            if not turn["active"] or turn["completed"]:
                raise InvalidTransition("turn is not an unfinished active turn")
            if turn["actor"] != "human":
                raise InvalidTransition("only an unfinished human turn may continue")
            legs = connection.execute(
                """
                SELECT * FROM move_legs
                WHERE turn_id = ? ORDER BY leg_index
                """,
                (turn["id"],),
            ).fetchall()
            if len(legs) != 1:
                raise InvalidTransition(
                    "unfinished turn must have exactly one existing leg"
                )
            if not legs[0]["uci"].endswith("+"):
                raise InvalidTransition(
                    "unfinished turn does not contain an activation leg"
                )
            settings = self._clock_settings_from_row(game)
            after_clock = self._validate_clock_state(settings, clock_state, now)
            current_moves = _decode_json(game["active_moves_json"], [])
            after_moves = current_moves + [uci]
            if len(after_moves) > MAX_MOVES:
                raise InvalidTransition("game move history reached its limit")
            after_moves_json = _canonical_json(after_moves, label="after moves")
            after_clock_json = _canonical_json(
                after_clock, label="after clock"
            )
            connection.execute(
                """
                INSERT INTO move_legs(
                    turn_id, leg_index, uci, before_fen, after_fen,
                    active, created_at_ms
                ) VALUES (?, 2, ?, ?, ?, 1, ?)
                """,
                (
                    turn["id"],
                    uci,
                    game["current_fen"],
                    after_fen,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE turns
                SET after_fen = ?, after_moves_json = ?, after_clock_json = ?,
                    completed = 1, completed_at_ms = ?
                WHERE id = ? AND active = 1 AND completed = 0
                """,
                (
                    after_fen,
                    after_moves_json,
                    after_clock_json,
                    now,
                    turn["id"],
                ),
            )
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE games
                SET current_fen = ?, active_moves_json = ?,
                    white_time_ms = ?, black_time_ms = ?,
                    clock_running_color = ?, clock_anchor_at_ms = ?,
                    revision = ?, updated_at_ms = ?
                WHERE id = ? AND status = 'active' AND revision = ?
                """,
                (
                    after_fen,
                    after_moves_json,
                    *self._clock_columns(after_clock),
                    new_revision,
                    now,
                    game["id"],
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                current = connection.execute(
                    "SELECT revision FROM games WHERE id = ?", (game["id"],)
                ).fetchone()[0]
                raise RevisionConflict(expected_revision, current)
            self._insert_event(
                connection,
                game_id=game["id"],
                revision=new_revision,
                event_type="turn_completed",
                actor="human",
                payload={
                    "turn_id": turn_public_id,
                    "color": turn["color"],
                    "leg": uci,
                },
                now=now,
            )
            updated = connection.execute(
                "SELECT * FROM games WHERE id = ?", (game["id"],)
            ).fetchone()
            result = self._game_projection(updated, owner=True)
            result["recorded_turn_id"] = turn_public_id
            self._apply_mutation_command(
                connection,
                game_id=game["id"],
                game_revision=new_revision,
                binding=command_binding,
                result=result,
                stage_data={
                    "revision": new_revision,
                    "recorded_turn_id": turn_public_id,
                    "actor": "human",
                    "turn_complete": True,
                },
                now=now,
            )
        return result

    def _complete_game_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        game: sqlite3.Row,
        expected_revision: int,
        result: str,
        termination: str,
        actor: str,
        command_binding: Optional[
            tuple[str, str, Optional[str], bool]
        ],
        now: int,
        void_unfinished_reason: Optional[str] = None,
        required_human_color: Optional[str] = None,
        settle_zero_color: Optional[str] = None,
        clock_state: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Complete an already-authorized game inside its write transaction."""

        if (
            required_human_color is not None
            and game["human_color"] != required_human_color
        ):
            raise InvalidTransition(
                "only the human player's color may resign"
            )
        incomplete_turns = connection.execute(
            """
            SELECT * FROM turns
            WHERE game_id = ? AND active = 1 AND completed = 0
            """,
            (game["id"],),
        ).fetchall()
        if len(incomplete_turns) > 1:
            raise GameStoreError(
                "game contains multiple unfinished active turns"
            )
        voided_turn_id = None
        target_fen = game["current_fen"]
        target_moves_json = game["active_moves_json"]
        if clock_state is None:
            target_white_ms = game["white_time_ms"]
            target_black_ms = game["black_time_ms"]
        else:
            target_white_ms = clock_state["white_ms"]
            target_black_ms = clock_state["black_ms"]
        if incomplete_turns:
            incomplete = incomplete_turns[0]
            may_void = (
                void_unfinished_reason == termination
                and termination in ("resignation", "timeout")
                and incomplete["actor"] == "human"
                and (
                    termination != "timeout"
                    or settle_zero_color == incomplete["color"]
                )
            )
            if not may_void:
                raise InvalidTransition(
                    "an unfinished human turn must finish or be taken back"
                )
            connection.execute(
                """
                UPDATE turns
                SET active = 0, undone_at_ms = ?
                WHERE id = ? AND active = 1 AND completed = 0
                """,
                (now, incomplete["id"]),
            )
            connection.execute(
                """
                UPDATE move_legs
                SET active = 0, undone_at_ms = ?
                WHERE turn_id = ? AND active = 1
                """,
                (now, incomplete["id"]),
            )
            target_fen = incomplete["before_fen"]
            target_moves_json = incomplete["before_moves_json"]
            if clock_state is None:
                restored_clock = _decode_json(
                    incomplete["before_clock_json"], fallback={}
                )
                restored_clock = self._validate_clock_state(
                    self._clock_settings_from_row(game),
                    restored_clock,
                    now,
                )
                target_white_ms = restored_clock["white_ms"]
                target_black_ms = restored_clock["black_ms"]
            voided_turn_id = incomplete["public_id"]
        if settle_zero_color == "white":
            target_white_ms = 0
        elif settle_zero_color == "black":
            target_black_ms = 0
        new_revision = expected_revision + 1
        cursor = connection.execute(
            """
            UPDATE games
            SET status = 'completed',
                current_fen = ?, active_moves_json = ?,
                white_time_ms = ?, black_time_ms = ?,
                result = ?, termination = ?,
                public_visible = publish_on_completion,
                clock_running_color = NULL, clock_anchor_at_ms = NULL,
                revision = ?, updated_at_ms = ?, completed_at_ms = ?
            WHERE id = ? AND status = 'active' AND revision = ?
            """,
            (
                target_fen,
                target_moves_json,
                target_white_ms,
                target_black_ms,
                result,
                termination,
                new_revision,
                now,
                now,
                game["id"],
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            current = connection.execute(
                "SELECT revision FROM games WHERE id = ?", (game["id"],)
            ).fetchone()[0]
            raise RevisionConflict(expected_revision, current)
        self._insert_event(
            connection,
            game_id=game["id"],
            revision=new_revision,
            event_type="game_completed",
            actor=actor,
            payload={
                "result": result,
                "termination": termination,
                "voided_unfinished_turn_id": voided_turn_id,
                "timed_out_color": settle_zero_color,
            },
            now=now,
        )
        updated = connection.execute(
            "SELECT * FROM games WHERE id = ?", (game["id"],)
        ).fetchone()
        completion_result = self._game_projection(updated, owner=True)
        self._apply_mutation_command(
            connection,
            game_id=game["id"],
            game_revision=new_revision,
            binding=command_binding,
            result=completion_result,
            stage_data={
                "revision": new_revision,
                "status": "completed",
                "result": result,
                "termination": termination,
            },
            now=now,
        )
        return completion_result

    def takeback(
        self,
        owner_token: str,
        public_id: str,
        *,
        expected_revision: int,
        received_at_ms: int,
        idempotency_key: Optional[str] = None,
        command_lease: Optional[str] = None,
        command_stage: Optional[str] = None,
        command_complete: bool = False,
    ) -> dict[str, Any]:
        """Undo to the previous human decision without deleting history.

        An unfinished human turn is undone by itself.  Otherwise the latest
        active turn must be an engine turn and both it and the immediately
        preceding human turn are undone.  This deterministic rule is evaluated
        while holding the same optimistic write transaction that performs the
        restore.
        """

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        expected_revision = _validate_revision(expected_revision)
        received_at_ms = _validate_received_at_ms(received_at_ms)
        command_binding = self._normalize_mutation_command(
            idempotency_key=idempotency_key,
            command_lease=command_lease,
            command_stage=command_stage,
            command_complete=command_complete,
        )
        with self._write() as connection:
            now = self._now_ms()
            if received_at_ms > now:
                raise ValidationError(
                    "received_at_ms must not be later than transaction time"
                )
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            self._check_active_revision(game, expected_revision)
            if command_binding is not None:
                command = self._require_command_lease(
                    connection,
                    game_id=game["id"],
                    idempotency_key=command_binding[0],
                    lease_token=command_binding[1],
                    now=now,
                )
                self._require_started_command_matches(
                    command,
                    operation="takeback",
                    expected_revision=expected_revision,
                    received_at_ms=received_at_ms,
                )
            settled_clock, expired_color = self._clock_at(
                game, received_at_ms
            )
            if expired_color is not None:
                self._guard_timeout_against_pending_command(
                    connection,
                    game=game,
                    command_binding=command_binding,
                    now=now,
                )
                timeout_result = (
                    "0-1" if expired_color == "white" else "1-0"
                )
                return self._complete_game_in_transaction(
                    connection,
                    game=game,
                    expected_revision=expected_revision,
                    result=timeout_result,
                    termination="timeout",
                    actor="system",
                    command_binding=self._command_binding_at_stage(
                        command_binding, "game_completed"
                    ),
                    now=now,
                    void_unfinished_reason="timeout",
                    settle_zero_color=expired_color,
                    clock_state=settled_clock,
                )
            active_turns = connection.execute(
                """
                SELECT * FROM turns
                WHERE game_id = ? AND active = 1
                ORDER BY turn_index
                """,
                (game["id"],),
            ).fetchall()
            if not active_turns:
                raise InvalidTransition("there is no turn to take back")
            latest = active_turns[-1]
            if latest["actor"] == "human" and not latest["completed"]:
                affected = [latest]
                human_turn = latest
            else:
                if latest["actor"] != "engine" or not latest["completed"]:
                    raise InvalidTransition(
                        "wait for the engine reply before requesting takeback"
                    )
                if len(active_turns) < 2:
                    raise InvalidTransition(
                        "engine turn has no preceding human decision"
                    )
                preceding = active_turns[-2]
                if (
                    preceding["actor"] != "human"
                    or not preceding["completed"]
                    or preceding["turn_index"] + 1 != latest["turn_index"]
                ):
                    raise InvalidTransition(
                        "engine turn has no adjacent completed human turn"
                    )
                affected = [preceding, latest]
                human_turn = preceding

            affected_ids = [turn["id"] for turn in affected]
            placeholders = ",".join("?" for _ in affected_ids)
            cursor = connection.execute(
                f"""
                UPDATE turns
                SET active = 0, undone_at_ms = ?
                WHERE game_id = ? AND active = 1
                  AND id IN ({placeholders})
                """,
                (now, game["id"], *affected_ids),
            )
            if cursor.rowcount != len(affected_ids):
                raise InvalidTransition("takeback target changed")
            connection.execute(
                f"""
                UPDATE move_legs
                SET active = 0, undone_at_ms = ?
                WHERE active = 1 AND turn_id IN ({placeholders})
                """,
                (now, *affected_ids),
            )
            restored_clock = _decode_json(
                human_turn["before_clock_json"], fallback={}
            )
            settings = self._clock_settings_from_row(game)
            restored_clock = self._validate_clock_state(
                settings, restored_clock, now
            )
            if restored_clock["running_color"] is not None:
                # Restore the saved remaining milliseconds and side to move,
                # but do not charge the player for time spent in the now-
                # undone human turn and engine reply.
                restored_clock["anchor_at_ms"] = now
            restored_moves_json = human_turn["before_moves_json"]
            restored_moves = _decode_json(restored_moves_json, fallback=[])
            self._validate_moves(restored_moves)
            new_revision = expected_revision + 1
            cursor = connection.execute(
                """
                UPDATE games
                SET current_fen = ?, active_moves_json = ?,
                    white_time_ms = ?, black_time_ms = ?,
                    clock_running_color = ?, clock_anchor_at_ms = ?,
                    takeback_count = takeback_count + 1,
                    revision = ?, updated_at_ms = ?
                WHERE id = ? AND status = 'active' AND revision = ?
                """,
                (
                    human_turn["before_fen"],
                    restored_moves_json,
                    *self._clock_columns(restored_clock),
                    new_revision,
                    now,
                    game["id"],
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                current = connection.execute(
                    "SELECT revision FROM games WHERE id = ?", (game["id"],)
                ).fetchone()[0]
                raise RevisionConflict(expected_revision, current)
            affected_public_ids = [turn["public_id"] for turn in affected]
            self._insert_event(
                connection,
                game_id=game["id"],
                revision=new_revision,
                event_type="takeback",
                actor="human",
                payload={
                    "reverted_turn_ids": affected_public_ids,
                    "restored_fen": human_turn["before_fen"],
                    "restored_moves": restored_moves,
                },
                now=now,
            )
            updated = connection.execute(
                "SELECT * FROM games WHERE id = ?", (game["id"],)
            ).fetchone()
            result = self._game_projection(updated, owner=True)
            result["reverted_turn_ids"] = affected_public_ids
            self._apply_mutation_command(
                connection,
                game_id=game["id"],
                game_revision=new_revision,
                binding=command_binding,
                result=result,
                stage_data={
                    "revision": new_revision,
                    "reverted_turn_ids": affected_public_ids,
                },
                now=now,
            )
        return result

    def complete_game(
        self,
        owner_token: str,
        public_id: str,
        *,
        expected_revision: int,
        result: str,
        termination: str,
        actor: str = "system",
        idempotency_key: Optional[str] = None,
        command_lease: Optional[str] = None,
        command_stage: Optional[str] = None,
        command_complete: bool = False,
        _void_unfinished_reason: Optional[str] = None,
        _required_human_color: Optional[str] = None,
        _settle_zero_color: Optional[str] = None,
    ) -> dict[str, Any]:
        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        expected_revision = _validate_revision(expected_revision)
        if result not in ("1-0", "0-1", "1/2-1/2", "*"):
            raise ValidationError("result is invalid")
        termination = _validate_label(
            termination, "termination", maximum=MAX_EVENT_TYPE_LENGTH
        ).lower().replace(" ", "_")
        if actor not in ("human", "engine", "system"):
            raise ValidationError("completion actor is invalid")
        command_binding = self._normalize_mutation_command(
            idempotency_key=idempotency_key,
            command_lease=command_lease,
            command_stage=command_stage,
            command_complete=command_complete,
        )
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            self._check_active_revision(game, expected_revision)
            if command_binding is not None:
                self._require_command_lease(
                    connection,
                    game_id=game["id"],
                    idempotency_key=command_binding[0],
                    lease_token=command_binding[1],
                    now=now,
                )
            completion_clock = None
            if _settle_zero_color is not None:
                if game["clock_mode"] != "timed":
                    raise InvalidTransition(
                        "an unlimited game cannot end by timeout"
                    )
                if game["clock_running_color"] != _settle_zero_color:
                    raise InvalidTransition(
                        "only the side whose clock is running may time out"
                    )
                completion_clock, expired_color = self._clock_at(game, now)
                if expired_color != _settle_zero_color:
                    raise InvalidTransition(
                        "the running clock has not expired"
                    )
                self._guard_timeout_against_pending_command(
                    connection,
                    game=game,
                    command_binding=command_binding,
                    now=now,
                )
            completion_result = self._complete_game_in_transaction(
                connection,
                game=game,
                expected_revision=expected_revision,
                result=result,
                termination=termination,
                actor=actor,
                command_binding=command_binding,
                now=now,
                void_unfinished_reason=_void_unfinished_reason,
                required_human_color=_required_human_color,
                settle_zero_color=_settle_zero_color,
                clock_state=completion_clock,
            )
        return completion_result

    def resign_game(
        self,
        owner_token: str,
        public_id: str,
        *,
        expected_revision: int,
        resigning_color: str,
        received_at_ms: int,
        idempotency_key: Optional[str] = None,
        command_lease: Optional[str] = None,
        command_stage: Optional[str] = None,
        command_complete: bool = False,
    ) -> dict[str, Any]:
        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        expected_revision = _validate_revision(expected_revision)
        resigning_color = _validate_color(resigning_color, "resigning_color")
        received_at_ms = _validate_received_at_ms(received_at_ms)
        command_binding = self._normalize_mutation_command(
            idempotency_key=idempotency_key,
            command_lease=command_lease,
            command_stage=command_stage,
            command_complete=command_complete,
        )
        with self._write() as connection:
            now = self._now_ms()
            if received_at_ms > now:
                raise ValidationError(
                    "received_at_ms must not be later than transaction time"
                )
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            self._check_active_revision(game, expected_revision)
            if command_binding is not None:
                command = self._require_command_lease(
                    connection,
                    game_id=game["id"],
                    idempotency_key=command_binding[0],
                    lease_token=command_binding[1],
                    now=now,
                )
                self._require_started_command_matches(
                    command,
                    operation="resign",
                    expected_revision=expected_revision,
                    received_at_ms=received_at_ms,
                )
            settled_clock, expired_color = self._clock_at(
                game, received_at_ms
            )
            if expired_color is None:
                result = (
                    "0-1" if resigning_color == "white" else "1-0"
                )
                termination = "resignation"
                actor = "human"
                void_reason = "resignation"
                settle_zero_color = None
                completion_binding = command_binding
            else:
                self._guard_timeout_against_pending_command(
                    connection,
                    game=game,
                    command_binding=command_binding,
                    now=now,
                )
                result = "0-1" if expired_color == "white" else "1-0"
                termination = "timeout"
                actor = "system"
                void_reason = "timeout"
                settle_zero_color = expired_color
                completion_binding = self._command_binding_at_stage(
                    command_binding, "game_completed"
                )
            return self._complete_game_in_transaction(
                connection,
                game=game,
                expected_revision=expected_revision,
                result=result,
                termination=termination,
                actor=actor,
                command_binding=completion_binding,
                now=now,
                void_unfinished_reason=void_reason,
                required_human_color=resigning_color,
                settle_zero_color=settle_zero_color,
                clock_state=settled_clock,
            )

    def timeout_game(
        self,
        owner_token: str,
        public_id: str,
        *,
        expected_revision: int,
        timed_out_color: str,
        idempotency_key: Optional[str] = None,
        command_lease: Optional[str] = None,
        command_stage: Optional[str] = None,
        command_complete: bool = False,
    ) -> dict[str, Any]:
        timed_out_color = _validate_color(
            timed_out_color, "timed_out_color"
        )
        result = "0-1" if timed_out_color == "white" else "1-0"
        return self.complete_game(
            owner_token,
            public_id,
            expected_revision=expected_revision,
            result=result,
            termination="timeout",
            actor="system",
            idempotency_key=idempotency_key,
            command_lease=command_lease,
            command_stage=command_stage,
            command_complete=command_complete,
            _void_unfinished_reason="timeout",
            _settle_zero_color=timed_out_color,
        )

    # -- idempotent command lifecycle ------------------------------------

    @staticmethod
    def _validate_idempotency_key(key: Any) -> str:
        if not isinstance(key, str) or not _IDEMPOTENCY_RE.fullmatch(key):
            raise ValidationError(
                "idempotency key must be 8-128 URL-safe characters"
            )
        return key

    @staticmethod
    def _validate_command_stage(stage: Any) -> str:
        if not isinstance(stage, str) or not _COMMAND_STAGE_RE.fullmatch(stage):
            raise ValidationError(
                "command stage must be lowercase snake_case"
            )
        return stage

    @staticmethod
    def _command_projection(
        row: sqlite3.Row,
        *,
        now: Optional[int] = None,
        acquired: bool = False,
        reclaimed: bool = False,
        lease_token: Optional[str] = None,
    ) -> dict[str, Any]:
        result = {
            "status": row["status"],
            "attempt": row["attempt"],
            "stage": row["stage"],
            "stage_data": _decode_json(row["stage_json"], {}),
        }
        if row["status"] == "completed":
            result.update(
                {
                    "response": _decode_json(row["response_json"]),
                    "completed_revision": row["completed_revision"],
                    "replay": True,
                    "acquired": False,
                }
            )
        elif row["status"] == "failed":
            result.update(
                {
                    "error": _decode_json(row["error_json"]),
                    "replay": True,
                    "acquired": False,
                }
            )
        else:
            result.update(
                {
                    "lease_expires_at_ms": row["lease_expires_at_ms"],
                    "retry_after_ms": (
                        None
                        if now is None
                        else max(0, row["lease_expires_at_ms"] - now)
                    ),
                    "replay": not acquired,
                    "acquired": acquired,
                    "reclaimed": reclaimed,
                }
            )
            if acquired:
                result["lease_token"] = lease_token
        return result

    @staticmethod
    def _lease_hash(lease_token: str) -> bytes:
        if not isinstance(lease_token, str) or not 20 <= len(lease_token) <= 256:
            raise ValidationError("command lease token is invalid")
        return hashlib.sha256(lease_token.encode("utf-8")).digest()

    @staticmethod
    def _command_row(
        connection: sqlite3.Connection,
        game_id: int,
        idempotency_key: str,
    ) -> sqlite3.Row:
        command = connection.execute(
            """
            SELECT * FROM idempotent_commands
            WHERE game_id = ? AND idempotency_key = ?
            """,
            (game_id, idempotency_key),
        ).fetchone()
        if command is None:
            raise NotFoundError("idempotent command was not begun")
        return command

    @staticmethod
    def _started_command_recovery_data(
        command: sqlite3.Row,
    ) -> tuple[int, dict[str, Any]]:
        try:
            stage_data = _decode_json(command["stage_json"])
            if (
                not isinstance(stage_data, Mapping)
                or set(stage_data) != {"received_at_ms", "request"}
            ):
                raise ValueError
            idempotency_key = command["idempotency_key"]
            if (
                not isinstance(idempotency_key, str)
                or not _IDEMPOTENCY_RE.fullmatch(idempotency_key)
            ):
                raise ValueError
            received_at_ms = stage_data["received_at_ms"]
            if (
                isinstance(received_at_ms, bool)
                or not isinstance(received_at_ms, int)
                or received_at_ms < 0
            ):
                raise ValueError
            lease_expires_at_ms = command["lease_expires_at_ms"]
            if (
                isinstance(lease_expires_at_ms, bool)
                or not isinstance(lease_expires_at_ms, int)
                or lease_expires_at_ms < 0
            ):
                raise ValueError
            request = _validate_recoverable_command_request(
                stage_data["request"]
            )
            request_digest, _ = _request_hash(request)
            if not secrets.compare_digest(
                command["request_hash"], request_digest
            ):
                raise ValueError
        except (GameStoreError, TypeError, ValueError, json.JSONDecodeError):
            raise GameStoreError(
                "pending command recovery data is invalid"
            ) from None
        return received_at_ms, request

    @staticmethod
    def _started_command_rows(
        connection: sqlite3.Connection, game_id: int
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT *
            FROM idempotent_commands
            WHERE game_id = ? AND status = 'in_progress'
              AND stage = 'started'
            ORDER BY created_at_ms, id
            """,
            (game_id,),
        ).fetchall()

    def _require_started_command_matches(
        self,
        command: sqlite3.Row,
        *,
        operation: str,
        expected_revision: int,
        received_at_ms: int,
    ) -> None:
        if command["stage"] != "started":
            return
        stored_receipt, request = self._started_command_recovery_data(
            command
        )
        if (
            stored_receipt != received_at_ms
            or request["operation"] != operation
            or request["expected_revision"] != expected_revision
        ):
            raise IdempotencyConflict(
                "bound command does not match its durable request"
            )

    def _guard_timeout_against_pending_command(
        self,
        connection: sqlite3.Connection,
        *,
        game: sqlite3.Row,
        command_binding: Optional[
            tuple[str, str, Optional[str], bool]
        ],
        now: int,
    ) -> None:
        """Prevent timeout from overtaking the oldest timely command."""

        commands = self._started_command_rows(connection, game["id"])
        if (
            command_binding is not None
            and commands
            and commands[0]["idempotency_key"] != command_binding[0]
        ):
            self._started_command_recovery_data(commands[0])
            raise PendingCommand(
                max(0, commands[0]["lease_expires_at_ms"] - now)
            )
        for command in commands:
            received_at_ms, _ = self._started_command_recovery_data(command)
            settled_clock, expired_color = self._clock_at(
                game, received_at_ms
            )
            running_color = settled_clock["running_color"]
            timely = (
                running_color is not None
                and expired_color is None
                and settled_clock[f"{running_color}_ms"] > 0
            )
            if not timely:
                continue
            if (
                command_binding is not None
                and command["idempotency_key"] == command_binding[0]
            ):
                return
            raise PendingCommand(
                max(0, command["lease_expires_at_ms"] - now)
            )

    @classmethod
    def _require_command_lease(
        cls,
        connection: sqlite3.Connection,
        *,
        game_id: int,
        idempotency_key: str,
        lease_token: str,
        now: int,
    ) -> sqlite3.Row:
        command = cls._command_row(connection, game_id, idempotency_key)
        if command["status"] != "in_progress":
            raise InvalidTransition(
                f"command is already {command['status']}"
            )
        supplied_hash = cls._lease_hash(lease_token)
        if not secrets.compare_digest(command["lease_hash"], supplied_hash):
            raise InvalidTransition(
                "command lease is stale or belongs to another worker"
            )
        if command["lease_expires_at_ms"] <= now:
            raise InvalidTransition(
                "command lease expired; reacquire it with begin_command"
            )
        return command

    def _advance_command_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        game_id: int,
        idempotency_key: str,
        lease_token: str,
        stage: str,
        stage_data: Any,
        now: int,
    ) -> sqlite3.Row:
        stage = self._validate_command_stage(stage)
        _validate_payload_keys(stage_data, path="command stage data")
        encoded = _canonical_json(stage_data, label="command stage data")
        command = self._require_command_lease(
            connection,
            game_id=game_id,
            idempotency_key=idempotency_key,
            lease_token=lease_token,
            now=now,
        )
        new_expiry = now + self.command_lease_seconds * 1000
        connection.execute(
            """
            UPDATE idempotent_commands
            SET stage = ?, stage_json = ?, lease_expires_at_ms = ?,
                updated_at_ms = ?
            WHERE id = ? AND status = 'in_progress'
            """,
            (stage, encoded, new_expiry, now, command["id"]),
        )
        return connection.execute(
            "SELECT * FROM idempotent_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()

    def _complete_command_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        game_id: int,
        game_revision: int,
        idempotency_key: str,
        lease_token: str,
        response: Any,
        now: int,
    ) -> sqlite3.Row:
        _validate_payload_keys(response, path="command response")
        encoded = _canonical_json(response, label="command response")
        command = self._require_command_lease(
            connection,
            game_id=game_id,
            idempotency_key=idempotency_key,
            lease_token=lease_token,
            now=now,
        )
        connection.execute(
            """
            UPDATE idempotent_commands
            SET status = 'completed', stage = 'completed',
                lease_hash = NULL, lease_expires_at_ms = NULL,
                completed_revision = ?, response_json = ?, updated_at_ms = ?
            WHERE id = ? AND status = 'in_progress'
            """,
            (game_revision, encoded, now, command["id"]),
        )
        return connection.execute(
            "SELECT * FROM idempotent_commands WHERE id = ?",
            (command["id"],),
        ).fetchone()

    def begin_command(
        self,
        owner_token: str,
        public_id: str,
        idempotency_key: str,
        request: Any,
        *,
        received_at_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        """Acquire, observe, or reclaim a durable command lease.

        When supplied, ``received_at_ms`` is persisted only by the winning
        insert in the initial ``started`` stage.  Retries and lease reclaims
        always observe that immutable first receipt instead of replacing it
        with the retry's timestamp.
        """

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        if received_at_ms is not None:
            received_at_ms = _validate_received_at_ms(received_at_ms)
            request = _validate_recoverable_command_request(request)
            _validate_payload_keys(
                request, path="recoverable command request"
            )
            _validate_payload_secrets(
                request,
                (owner_token,),
                label="recoverable command request",
            )
        request_digest, _ = _request_hash(request)
        initial_stage_json = _canonical_json(
            (
                {}
                if received_at_ms is None
                else {
                    "received_at_ms": received_at_ms,
                    "request": request,
                }
            ),
            label="initial command stage data",
        )
        with self._write() as connection:
            now = self._now_ms()
            if received_at_ms is not None and received_at_ms > now:
                raise ValidationError(
                    "received_at_ms must not be later than transaction time"
                )
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            existing = connection.execute(
                """
                SELECT * FROM idempotent_commands
                WHERE game_id = ? AND idempotency_key = ?
                """,
                (game["id"], idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != request_digest:
                    raise IdempotencyConflict(
                        "idempotency key was already used for another request"
                    )
                if existing["status"] != "in_progress":
                    return self._command_projection(existing, now=now)
                if existing["lease_expires_at_ms"] > now:
                    return self._command_projection(existing, now=now)
                lease_token = secrets.token_urlsafe(24)
                lease_hash = self._lease_hash(lease_token)
                lease_expires = now + self.command_lease_seconds * 1000
                connection.execute(
                    """
                    UPDATE idempotent_commands
                    SET lease_hash = ?, lease_expires_at_ms = ?,
                        attempt = attempt + 1, updated_at_ms = ?
                    WHERE id = ? AND status = 'in_progress'
                      AND lease_expires_at_ms <= ?
                    """,
                    (
                        lease_hash,
                        lease_expires,
                        now,
                        existing["id"],
                        now,
                    ),
                )
                reclaimed = connection.execute(
                    "SELECT * FROM idempotent_commands WHERE id = ?",
                    (existing["id"],),
                ).fetchone()
                return self._command_projection(
                    reclaimed,
                    now=now,
                    acquired=True,
                    reclaimed=True,
                    lease_token=lease_token,
                )
            if game["status"] != "active":
                raise InvalidTransition(
                    "new commands cannot begin after a game has ended"
                )
            lease_token = secrets.token_urlsafe(24)
            lease_hash = self._lease_hash(lease_token)
            lease_expires = now + self.command_lease_seconds * 1000
            connection.execute(
                """
                INSERT INTO idempotent_commands(
                    game_id, idempotency_key, request_hash, status,
                    lease_hash, lease_expires_at_ms, attempt,
                    stage, stage_json, created_at_ms, updated_at_ms
                ) VALUES (
                    ?, ?, ?, 'in_progress', ?, ?, 1, 'started', ?, ?, ?
                )
                """,
                (
                    game["id"],
                    idempotency_key,
                    request_digest,
                    lease_hash,
                    lease_expires,
                    initial_stage_json,
                    now,
                    now,
                ),
            )
            created = self._command_row(
                connection, game["id"], idempotency_key
            )
        return self._command_projection(
            created,
            now=now,
            acquired=True,
            lease_token=lease_token,
        )

    def get_pending_started_command(
        self, owner_token: str, public_id: str
    ) -> Optional[dict[str, Any]]:
        """Return the oldest recoverable command that has not mutated a game."""

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        now = self._now_ms()
        with self._read() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            commands = self._started_command_rows(
                connection, game["id"]
            )
            if not commands:
                return None
            command = commands[0]
            received_at_ms, request = (
                self._started_command_recovery_data(command)
            )
            lease_expires_at_ms = command["lease_expires_at_ms"]
            return {
                "idempotency_key": command["idempotency_key"],
                "request": request,
                "received_at_ms": received_at_ms,
                "lease_expires_at_ms": lease_expires_at_ms,
                "lease_live": lease_expires_at_ms > now,
            }

    def advance_command(
        self,
        owner_token: str,
        public_id: str,
        idempotency_key: str,
        *,
        lease_token: str,
        stage: str,
        stage_data: Any,
    ) -> dict[str, Any]:
        """Persist recovery metadata and renew an acquired command lease."""

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        _validate_payload_secrets(
            stage_data,
            (owner_token, lease_token),
            label="command stage data",
        )
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            updated = self._advance_command_in_transaction(
                connection,
                game_id=game["id"],
                idempotency_key=idempotency_key,
                lease_token=lease_token,
                stage=stage,
                stage_data=stage_data,
                now=now,
            )
        return self._command_projection(
            updated, now=now, acquired=True, lease_token=lease_token
        )

    def heartbeat_command(
        self,
        owner_token: str,
        public_id: str,
        idempotency_key: str,
        *,
        lease_token: str,
    ) -> dict[str, Any]:
        """Renew a command lease without changing its recovery stage."""

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            command = self._require_command_lease(
                connection,
                game_id=game["id"],
                idempotency_key=idempotency_key,
                lease_token=lease_token,
                now=now,
            )
            lease_expires = now + self.command_lease_seconds * 1000
            connection.execute(
                """
                UPDATE idempotent_commands
                SET lease_expires_at_ms = ?, updated_at_ms = ?
                WHERE id = ? AND status = 'in_progress'
                """,
                (lease_expires, now, command["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM idempotent_commands WHERE id = ?",
                (command["id"],),
            ).fetchone()
        return self._command_projection(
            updated, now=now, acquired=True, lease_token=lease_token
        )

    def release_command(
        self,
        owner_token: str,
        public_id: str,
        idempotency_key: str,
        *,
        lease_token: str,
    ) -> dict[str, Any]:
        """Release a lease immediately while preserving recovery progress."""

        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            command = self._require_command_lease(
                connection,
                game_id=game["id"],
                idempotency_key=idempotency_key,
                lease_token=lease_token,
                now=now,
            )
            # Keep a non-null unreachable hash to satisfy the in-progress
            # invariant while ensuring the released raw lease can never write.
            connection.execute(
                """
                UPDATE idempotent_commands
                SET lease_hash = ?, lease_expires_at_ms = ?, updated_at_ms = ?
                WHERE id = ? AND status = 'in_progress'
                """,
                (secrets.token_bytes(32), now, now, command["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM idempotent_commands WHERE id = ?",
                (command["id"],),
            ).fetchone()
        return self._command_projection(updated, now=now)

    def complete_command(
        self,
        owner_token: str,
        public_id: str,
        idempotency_key: str,
        response: Any,
        *,
        lease_token: str,
    ) -> dict[str, Any]:
        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        _validate_payload_secrets(
            response,
            (owner_token, lease_token),
            label="command response",
        )
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            command = self._command_row(
                connection, game["id"], idempotency_key
            )
            if command["status"] == "completed":
                return self._command_projection(command)
            if command["status"] == "failed":
                raise InvalidTransition("failed command cannot become completed")
            updated = self._complete_command_in_transaction(
                connection,
                game_id=game["id"],
                game_revision=game["revision"],
                idempotency_key=idempotency_key,
                lease_token=lease_token,
                response=response,
                now=now,
            )
        result = self._command_projection(updated)
        result["replay"] = False
        return result

    def fail_command(
        self,
        owner_token: str,
        public_id: str,
        idempotency_key: str,
        error: Any,
        *,
        lease_token: str,
    ) -> dict[str, Any]:
        token_hash = _hash_token(owner_token)
        public_id = _validate_public_id(public_id)
        idempotency_key = self._validate_idempotency_key(idempotency_key)
        _validate_payload_secrets(
            error,
            (owner_token, lease_token),
            label="command error",
        )
        _validate_payload_keys(error, path="command error")
        encoded = _canonical_json(error, label="command error")
        now = self._now_ms()
        with self._write() as connection:
            game = self._game_row_for_owner(
                connection,
                public_id=public_id,
                token_hash=token_hash,
                now=now,
            )
            command = self._command_row(
                connection, game["id"], idempotency_key
            )
            if command["status"] == "failed":
                return self._command_projection(command)
            if command["status"] == "completed":
                raise InvalidTransition("completed command cannot become failed")
            self._require_command_lease(
                connection,
                game_id=game["id"],
                idempotency_key=idempotency_key,
                lease_token=lease_token,
                now=now,
            )
            connection.execute(
                """
                UPDATE idempotent_commands
                SET status = 'failed', stage = 'failed',
                    lease_hash = NULL, lease_expires_at_ms = NULL,
                    error_json = ?, updated_at_ms = ?
                WHERE id = ? AND status = 'in_progress'
                """,
                (encoded, now, command["id"]),
            )
            updated = connection.execute(
                "SELECT * FROM idempotent_commands WHERE id = ?",
                (command["id"],),
            ).fetchone()
        result = self._command_projection(updated)
        result["replay"] = False
        return result

    # -- public archive and retention ------------------------------------

    @staticmethod
    def _encode_cursor(completed_at_ms: int, public_id: str) -> str:
        payload = f"{completed_at_ms}:{public_id}".encode("ascii")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[int, str]:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 128:
            raise ValidationError("cursor is invalid")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(
                padded.encode("ascii"), altchars=b"-_", validate=True
            ).decode("ascii")
            timestamp, public_id = raw.split(":", 1)
            completed_at = int(timestamp)
            _validate_public_id(public_id)
            if not 0 <= completed_at <= 2**63 - 1:
                raise ValueError
        except (ValueError, OverflowError, UnicodeError, binascii.Error) as exc:
            raise ValidationError("cursor is invalid") from exc
        return completed_at, public_id

    def list_completed_public_games(
        self,
        *,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValidationError("limit must be between 1 and 100")
        parameters: list[Any] = []
        boundary = ""
        if cursor is not None:
            completed_at, public_id = self._decode_cursor(cursor)
            boundary = """
                AND (
                    completed_at_ms < ?
                    OR (completed_at_ms = ? AND public_id < ?)
                )
            """
            parameters.extend((completed_at, completed_at, public_id))
        parameters.append(limit + 1)
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM games
                WHERE status = 'completed' AND public_visible = 1
                {boundary}
                ORDER BY completed_at_ms DESC, public_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = None
        if has_more and rows:
            last = rows[-1]
            next_cursor = self._encode_cursor(
                last["completed_at_ms"], last["public_id"]
            )
        return {
            "games": [
                self._game_projection(row, owner=False, summary=True)
                for row in rows
            ],
            "next_cursor": next_cursor,
        }

    def sanitized_export(
        self,
        public_id: str,
        *,
        owner_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return an archive without session IDs, token hashes, IPs, or UAs."""

        if owner_token is None:
            game = self.get_public_game(public_id, include_history=True)
        else:
            game = self.get_game_for_owner(
                owner_token, public_id, include_history=True
            )
        return {"format": "double-move-game-v1", "game": game}

    def cleanup(
        self,
        *,
        abandon_after_seconds: Optional[int] = None,
        abandoned_retention_seconds: Optional[int] = None,
    ) -> dict[str, int]:
        """Expire sessions, abandon stale active games, and purge old drafts."""

        abandon_after = (
            self.abandon_after_seconds
            if abandon_after_seconds is None
            else abandon_after_seconds
        )
        retention = (
            self.abandoned_retention_seconds
            if abandoned_retention_seconds is None
            else abandoned_retention_seconds
        )
        for value, label in (
            (abandon_after, "abandon_after_seconds"),
            (retention, "abandoned_retention_seconds"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationError(f"{label} must be a positive integer")
        now = self._now_ms()
        stale_before = now - abandon_after * 1000
        purge_before = now - retention * 1000
        with self._write() as connection:
            stale = connection.execute(
                """
                SELECT g.*
                FROM games AS g
                LEFT JOIN sessions AS s ON s.id = g.owner_session_id
                WHERE g.status = 'active'
                  AND (
                    g.updated_at_ms < ?
                    OR g.owner_session_id IS NULL
                    OR s.id IS NULL
                    OR s.revoked_at_ms IS NOT NULL
                    OR s.expires_at_ms <= ?
                  )
                ORDER BY g.id
                """,
                (stale_before, now),
            ).fetchall()
            for game in stale:
                revision = game["revision"] + 1
                connection.execute(
                    """
                    UPDATE games
                    SET status = 'abandoned', result = '*',
                        termination = 'abandoned', public_visible = 0,
                        clock_running_color = NULL, clock_anchor_at_ms = NULL,
                        revision = ?, updated_at_ms = ?, completed_at_ms = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (revision, now, now, game["id"]),
                )
                self._insert_event(
                    connection,
                    game_id=game["id"],
                    revision=revision,
                    event_type="game_abandoned",
                    actor="system",
                    payload={"reason": "expired_or_inactive"},
                    now=now,
                )
            # Once an anonymous session can no longer authenticate, a hidden
            # display name has no remaining product purpose.  Publicly opted-
            # in names are retained with the completed archive.
            connection.execute(
                """
                UPDATE games
                SET player_name = NULL
                WHERE name_visible = 0
                  AND owner_session_id IN (
                    SELECT id FROM sessions
                    WHERE expires_at_ms <= ? OR revoked_at_ms IS NOT NULL
                  )
                """,
                (now,),
            )
            scrubbed_commands = connection.execute(
                """
                DELETE FROM idempotent_commands
                WHERE game_id IN (
                    SELECT g.id
                    FROM games AS g
                    LEFT JOIN sessions AS s ON s.id = g.owner_session_id
                    WHERE g.status = 'abandoned'
                       OR g.owner_session_id IS NULL
                       OR s.id IS NULL
                       OR s.revoked_at_ms IS NOT NULL
                       OR s.expires_at_ms <= ?
                )
                """,
                (now,),
            ).rowcount
            expired_sessions = connection.execute(
                """
                DELETE FROM sessions
                WHERE expires_at_ms <= ? OR revoked_at_ms IS NOT NULL
                """,
                (now,),
            ).rowcount
            purged_games = connection.execute(
                """
                DELETE FROM games
                WHERE status = 'abandoned' AND completed_at_ms < ?
                """,
                (purge_before,),
            ).rowcount
        return {
            "abandoned_games": len(stale),
            "expired_sessions": expired_sessions,
            "purged_abandoned_games": purged_games,
            "scrubbed_command_caches": scrubbed_commands,
        }

    # -- maintenance ------------------------------------------------------

    def health_check(self) -> bool:
        """Perform a bounded, read-only readiness probe of the live database.

        Unlike normal repository reads, this deliberately bypasses
        :meth:`_connect` so a deleted database cannot be recreated or
        migrated by a health request.
        """

        if not os.path.exists(self.database_path):
            raise NotFoundError("game database is missing")
        database_uri = (
            "file:"
            + quote(self.database_path, safe="/")
            + "?mode=ro"
        )
        connection = None
        try:
            connection = sqlite3.connect(
                database_uri,
                uri=True,
                timeout=self.busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.execute(
                f"PRAGMA busy_timeout={self.busy_timeout_ms}"
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version != SCHEMA_VERSION:
                raise GameStoreError(
                    f"database schema {version} does not match supported "
                    f"schema {SCHEMA_VERSION}"
                )
            required_tables = {
                "sessions",
                "games",
                "turns",
                "move_legs",
                "game_events",
                "idempotent_commands",
                "session_commands",
            }
            placeholders = ",".join("?" for _ in required_tables)
            present = {
                row[0]
                for row in connection.execute(
                    f"""
                    SELECT name
                    FROM sqlite_schema
                    WHERE type = 'table' AND name IN ({placeholders})
                    """,
                    tuple(sorted(required_tables)),
                )
            }
            if present != required_tables:
                raise GameStoreError(
                    "game database is missing required schema"
                )
            core_schema_queries = (
                """
                SELECT id, token_hash, expires_at_ms
                FROM sessions LIMIT 1
                """,
                """
                SELECT id, public_id, status, revision, temp_decay_moves
                FROM games LIMIT 1
                """,
                """
                SELECT id, public_id, game_id, turn_index, completed, active
                FROM turns LIMIT 1
                """,
                """
                SELECT id, turn_id, leg_index, uci, active
                FROM move_legs LIMIT 1
                """,
                """
                SELECT id, game_id, event_index, revision, event_type
                FROM game_events LIMIT 1
                """,
                """
                SELECT id, game_id, idempotency_key, status, stage
                FROM idempotent_commands LIMIT 1
                """,
                """
                SELECT id, session_id, idempotency_key, status, request_hash
                FROM session_commands LIMIT 1
                """,
            )
            for query in core_schema_queries:
                connection.execute(query).fetchone()
            return True
        except GameStoreError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise GameStoreError(
                "game database health check failed"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def integrity_check(self) -> list[str]:
        with self._read() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
            ]

    def backup(
        self,
        destination_path: os.PathLike[str] | str,
        *,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        """Create a consistent SQLite backup and verify its integrity."""

        destination = os.path.abspath(os.fspath(destination_path))
        if destination == self.database_path:
            raise ValidationError("backup destination must differ from source")
        if os.path.exists(destination):
            try:
                aliases_source = os.path.samefile(
                    self.database_path, destination
                )
            except OSError as exc:
                raise ValidationError(
                    "backup destination cannot be inspected safely"
                ) from exc
            if aliases_source:
                raise ValidationError(
                    "backup destination aliases the source database"
                )
            if not overwrite:
                raise ValidationError("backup destination already exists")
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        if overwrite and os.path.exists(destination):
            # Opening the existing database and replacing its pages through the
            # backup API is safe; no unlink or broad destructive operation is
            # needed.
            pass
        with self._write_lock:
            source = self._connect()
            destination_connection = self._connect(destination)
            try:
                source.backup(destination_connection)
                self._make_private(destination)
                rows = destination_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchall()
                integrity = [row[0] for row in rows]
                if integrity != ["ok"]:
                    raise GameStoreError(
                        "backup integrity check failed: "
                        + "; ".join(integrity)
                    )
            finally:
                destination_connection.close()
                source.close()
        return {
            "path": destination,
            "size_bytes": os.path.getsize(destination),
            "integrity_check": integrity,
        }


__all__ = [
    "GameStore",
    "GameStoreError",
    "IdempotencyConflict",
    "InvalidTransition",
    "NotFoundError",
    "OwnershipError",
    "PendingCommand",
    "RevisionConflict",
    "ValidationError",
]
