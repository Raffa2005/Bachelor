#!/usr/bin/env python3
"""Application service for anonymous public double-move games.

The HTTP server is deliberately kept out of this module.  ``PublicPlayService``
owns validation, clocks, idempotent game commands, authoritative engine
checks, and response shaping; the request handler only translates cookies,
headers, paths, and JSON.

The service never persists a raw session token, IP address, or user agent.
``GameStore`` hashes the bearer token before storage, and the in-memory rate
limiter keys buckets by a separate SHA-256 digest.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import base64
import copy
import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from typing import Any, Callable, ContextManager, Mapping, Optional, Protocol

try:
    from .game_store import (
        GameStore,
        GameStoreError,
        IdempotencyConflict,
        InvalidTransition,
        NotFoundError,
        OwnershipError,
        PendingCommand,
        RevisionConflict,
        ValidationError,
    )
except ImportError:
    from game_store import (  # type: ignore
        GameStore,
        GameStoreError,
        IdempotencyConflict,
        InvalidTransition,
        NotFoundError,
        OwnershipError,
        PendingCommand,
        RevisionConflict,
        ValidationError,
    )


START_FEN = (
    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
    "w KQkq - 0 1"
)
SESSION_COOKIE = "dm_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
MAX_DISPLAY_NAME = 60
MAX_PUBLIC_LIMIT = 100
MAX_PLAY_NODES = 1_000_000
MAX_TEMP_DECAY_MOVES = 640
MAX_CLOCK_INITIAL_MS = 24 * 60 * 60 * 1000
MAX_CLOCK_INCREMENT_MS = 60 * 60 * 1000

_MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?\+?$")
_GAME_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,128}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+@/-]{0,159}$")
_BACKEND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    description: str
    nodes: int
    temperature: float
    temp_decay_moves: int


@dataclass(frozen=True)
class ModelChoice:
    id: str
    label: str
    sha256: str
    compatibility_backends: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClockChoice:
    id: str
    label: str
    description: str
    initial_ms: Optional[int]
    increment_ms: int

    def store_value(self) -> dict[str, Any]:
        if self.initial_ms is None:
            return {"mode": "unlimited"}
        return {
            "mode": "timed",
            "initial_ms": self.initial_ms,
            "increment_ms": self.increment_ms,
        }


PRESETS: tuple[Preset, ...] = (
    Preset(
        "policy",
        "Policy only · varied",
        "The model moves almost immediately and plays with high variety.",
        1,
        0.5,
        10,
    ),
    Preset(
        "policy-deterministic",
        "Policy only · deterministic",
        "Policy-only play without move sampling.",
        1,
        0.0,
        0,
    ),
    Preset(
        "casual",
        "Casual",
        "A quick, varied opponent for learning the variant.",
        25,
        0.5,
        10,
    ),
    Preset(
        "club",
        "Club",
        "A steadier opponent with a modest search.",
        100,
        0.5,
        10,
    ),
    Preset(
        "full-temperature",
        "Full strength · varied",
        "Full search with controlled move variety.",
        800,
        0.5,
        10,
    ),
    Preset(
        "full",
        "Full strength · deterministic",
        "Full search without move sampling.",
        800,
        0.0,
        0,
    ),
)

CLOCKS: tuple[ClockChoice, ...] = (
    ClockChoice("unlimited", "Unlimited", "No clock", None, 0),
    ClockChoice("3+2", "3 + 2", "Three minutes, two-second increment", 180_000, 2_000),
    ClockChoice("5+3", "5 + 3", "Five minutes, three-second increment", 300_000, 3_000),
    ClockChoice("10+0", "10 + 0", "Ten minutes, no increment", 600_000, 0),
    ClockChoice(
        "15+10",
        "15 + 10",
        "Fifteen minutes, ten-second increment",
        900_000,
        10_000,
    ),
)

_PRESET_BY_ID = {preset.id: preset for preset in PRESETS}
_CLOCK_BY_ID = {clock.id: clock for clock in CLOCKS}


class EngineLike(Protocol):
    """The subset of the synchronous lc0 wrapper used by public play."""

    def set_position(self, fen: str, moves: list[str]) -> None: ...

    def position_info(self) -> Mapping[str, Any]: ...

    def set_option(self, name: str, value: Any) -> None: ...

    def go_nodes(self, nodes: int) -> tuple[Optional[str], Any, Any]: ...


EngineSessionFactory = Callable[[str, str, str], ContextManager[EngineLike]]


class PublicPlayError(Exception):
    """Safe application error suitable for an HTTP JSON response."""

    def __init__(self, message: str, status: int = 400, code: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.code = code

    def payload(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code}


@dataclass(frozen=True)
class SessionConfiguration:
    """Result of the config endpoint.

    ``session_token`` is transport-only and must be placed in the HttpOnly
    cookie.  It must never be included in the JSON payload.
    """

    payload: dict[str, Any]
    session_token: str
    set_cookie: bool


@dataclass
class _CommandLease:
    key: str
    lease_token: str
    stage: str
    stage_data: dict[str, Any]
    attempt: int
    reclaimed: bool


class TokenBucketLimiter:
    """Bounded, in-memory rate limiting keyed only by a token digest."""

    def __init__(
        self,
        *,
        capacity: float = 30.0,
        refill_per_second: float = 0.5,
        maximum_buckets: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ):
        if capacity <= 0 or refill_per_second <= 0 or maximum_buckets < 1:
            raise ValueError("invalid rate limiter configuration")
        self.capacity = float(capacity)
        self.refill_per_second = float(refill_per_second)
        self.maximum_buckets = maximum_buckets
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, key: str, cost: float = 1.0) -> None:
        if cost <= 0 or cost > self.capacity:
            raise ValueError("invalid token cost")
        now = self._clock()
        with self._lock:
            tokens, updated = self._buckets.pop(
                key, (self.capacity, now)
            )
            tokens = min(
                self.capacity,
                tokens + max(0.0, now - updated) * self.refill_per_second,
            )
            if tokens < cost:
                self._buckets[key] = (tokens, now)
                raise PublicPlayError(
                    "Too many requests; wait a moment and try again.",
                    429,
                    "rate_limited",
                )
            self._buckets[key] = (tokens - cost, now)
            while len(self._buckets) > self.maximum_buckets:
                self._buckets.popitem(last=False)


class _StripedLocks:
    """Fixed-size per-key lock pool without an unbounded game-id map."""

    def __init__(self, count: int = 64):
        self._locks = tuple(threading.RLock() for _ in range(count))

    def for_key(self, key: str) -> threading.RLock:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % len(self._locks)
        return self._locks[index]


class PublicPlayService:
    """Coordinates storage and one authoritative double-move lc0 process."""

    def __init__(
        self,
        store: GameStore,
        engine_session: EngineSessionFactory,
        *,
        models: tuple[ModelChoice, ...],
        compatibility_models: tuple[ModelChoice, ...] = (),
        default_model_id: str,
        backend: str,
        csrf_secret: Optional[bytes] = None,
        clock: Callable[[], float] = time.time,
        rate_limiter: Optional[TokenBucketLimiter] = None,
        config_rate_limiter: Optional[TokenBucketLimiter] = None,
        global_config_refresh_rate_limiter: Optional[
            TokenBucketLimiter
        ] = None,
        global_create_rate_limiter: Optional[TokenBucketLimiter] = None,
        global_mutation_rate_limiter: Optional[TokenBucketLimiter] = None,
        global_owner_read_rate_limiter: Optional[TokenBucketLimiter] = None,
        secure_cookie: bool = False,
        stage_hook: Optional[
            Callable[[str, Mapping[str, Any]], None]
        ] = None,
    ):
        if not isinstance(models, tuple) or not models:
            raise ValueError("models must be a non-empty tuple")
        if not isinstance(compatibility_models, tuple):
            raise ValueError("compatibility_models must be a tuple")
        model_catalog: dict[str, ModelChoice] = {}
        selectable_ids: set[str] = set()
        compatibility_ids = {model.id for model in compatibility_models}
        for model in models + compatibility_models:
            if not isinstance(model, ModelChoice):
                raise ValueError("models must contain ModelChoice values")
            if not _MODEL_ID_RE.fullmatch(model.id):
                raise ValueError("model id is invalid")
            if (
                not isinstance(model.label, str)
                or not model.label.strip()
                or len(model.label) > 160
            ):
                raise ValueError("model label is invalid")
            if not re.fullmatch(r"[0-9a-f]{64}", model.sha256 or ""):
                raise ValueError(
                    "model sha256 must be 64 lowercase hexadecimal characters"
                )
            if not isinstance(model.compatibility_backends, tuple) or any(
                not isinstance(candidate, str)
                or not _BACKEND_RE.fullmatch(candidate)
                for candidate in model.compatibility_backends
            ):
                raise ValueError("model compatibility backends are invalid")
            if model.id not in compatibility_ids and model.compatibility_backends:
                raise ValueError(
                    "selectable models cannot declare compatibility backends"
                )
            if model.id in model_catalog:
                raise ValueError("model ids must be unique")
            model_catalog[model.id] = model
        selectable_ids.update(model.id for model in models)
        if default_model_id not in selectable_ids:
            raise ValueError("default_model_id must identify a configured model")
        if not isinstance(backend, str) or not backend:
            raise ValueError("backend is required")
        secret = csrf_secret if csrf_secret is not None else secrets.token_bytes(32)
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ValueError("csrf_secret must contain at least 32 bytes")
        self.store = store
        self._engine_session = engine_session
        self.models = models
        self.default_model_id = default_model_id
        self._models_by_id = model_catalog
        self._selectable_models_by_id = {
            model_id: model_catalog[model_id] for model_id in selectable_ids
        }
        self._compatibility_model_ids = compatibility_ids
        self.backend = backend
        self._csrf_secret = secret
        self._clock = clock
        self.rate_limiter = rate_limiter or TokenBucketLimiter()
        self.config_rate_limiter = config_rate_limiter or TokenBucketLimiter(
            capacity=120,
            refill_per_second=2,
            maximum_buckets=1,
        )
        self.global_config_refresh_rate_limiter = (
            global_config_refresh_rate_limiter
            or TokenBucketLimiter(
                capacity=240,
                refill_per_second=10,
                maximum_buckets=1,
            )
        )
        self.global_create_rate_limiter = (
            global_create_rate_limiter
            or TokenBucketLimiter(
                capacity=30,
                refill_per_second=0.25,
                maximum_buckets=1,
            )
        )
        self.global_mutation_rate_limiter = (
            global_mutation_rate_limiter
            or TokenBucketLimiter(
                capacity=120,
                refill_per_second=2,
                maximum_buckets=1,
            )
        )
        self.global_owner_read_rate_limiter = (
            global_owner_read_rate_limiter
            or TokenBucketLimiter(
                capacity=120,
                refill_per_second=5,
                maximum_buckets=1,
            )
        )
        self.secure_cookie = secure_cookie
        self._stage_hook = stage_hook
        self._locks = _StripedLocks()
        self._position_cache: OrderedDict[
            tuple[str, int], dict[str, Any]
        ] = OrderedDict()
        self._position_cache_lock = threading.Lock()
        self._position_cache_limit = 256

    # -- sessions and request security ----------------------------------

    @staticmethod
    def parse_session_cookie(cookie_header: Optional[str]) -> Optional[str]:
        if not cookie_header or len(cookie_header) > 4096:
            return None
        found = None
        for item in cookie_header.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                if found is not None or not _SESSION_TOKEN_RE.fullmatch(value):
                    return None
                found = value
        return found

    def session_cookie_header(self, token: str) -> str:
        if not _SESSION_TOKEN_RE.fullmatch(token):
            raise ValueError("invalid session token")
        parts = [
            f"{SESSION_COOKIE}={token}",
            "Path=/",
            f"Max-Age={SESSION_MAX_AGE_SECONDS}",
            "HttpOnly",
            "SameSite=Lax",
        ]
        if self.secure_cookie:
            parts.append("Secure")
        return "; ".join(parts)

    def _csrf(self, token: str) -> str:
        digest = hmac.new(
            self._csrf_secret,
            b"double-move-csrf-v1\0" + token.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify_csrf(self, token: Optional[str], supplied: Optional[str]) -> str:
        token = self._require_session_token(token)
        if (
            not isinstance(supplied, str)
            or len(supplied) > 128
            or not hmac.compare_digest(self._csrf(token), supplied)
        ):
            raise PublicPlayError(
                "CSRF token is missing or invalid.", 403, "csrf_failed"
            )
        return token

    @staticmethod
    def validate_write_origin(
        *,
        host: Optional[str],
        origin: Optional[str],
        sec_fetch_site: Optional[str],
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        """Require a configured origin or an exact same-host HTTP(S) origin."""

        from urllib.parse import urlsplit

        if (sec_fetch_site or "").strip().lower() == "cross-site":
            raise PublicPlayError(
                "Cross-origin requests are not allowed.", 403, "origin_failed"
            )
        raw_origin = (origin or "").strip()
        raw_host = (host or "").strip().lower()
        if not raw_origin:
            raise PublicPlayError(
                "Origin header is required.", 403, "origin_failed"
            )
        try:
            parsed = urlsplit(raw_origin)
            port = parsed.port
        except ValueError as exc:
            raise PublicPlayError(
                "Origin header is invalid.", 403, "origin_failed"
            ) from exc
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise PublicPlayError(
                "Origin header is invalid.", 403, "origin_failed"
            )
        normalized_origin = (
            f"{parsed.scheme}://{parsed.hostname.lower()}"
            + (f":{port}" if port is not None else "")
        )
        normalized_allowed = tuple(value.rstrip("/").lower() for value in allowed_origins)
        if normalized_allowed:
            accepted = normalized_origin.lower() in normalized_allowed
        else:
            accepted = bool(raw_host) and parsed.netloc.lower() == raw_host
        if not accepted:
            raise PublicPlayError(
                "Cross-origin requests are not allowed.", 403, "origin_failed"
            )

    def _require_session_token(self, token: Optional[str]) -> str:
        if not isinstance(token, str) or not _SESSION_TOKEN_RE.fullmatch(token):
            raise PublicPlayError(
                "Anonymous session is missing or expired.",
                401,
                "session_required",
            )
        try:
            session = self.store.lookup_session(token)
        except GameStoreError as exc:
            raise PublicPlayError(
                "Anonymous session is missing or expired.",
                401,
                "session_required",
            ) from exc
        if session is None:
            raise PublicPlayError(
                "Anonymous session is missing or expired.",
                401,
                "session_required",
            )
        return token

    def _consume_write(self, token: str, *, creation: bool = False) -> None:
        key = hashlib.sha256(token.encode("ascii")).hexdigest()
        self.rate_limiter.consume(key)
        limiter = (
            self.global_create_rate_limiter
            if creation
            else self.global_mutation_rate_limiter
        )
        limiter.consume("global")

    def _consume_read(self, token: str) -> None:
        key = hashlib.sha256(token.encode("ascii")).hexdigest()
        self.rate_limiter.consume(key, cost=0.25)

    def get_config(self, cookie_token: Optional[str]) -> SessionConfiguration:
        token = cookie_token
        set_cookie = False
        if token is not None:
            try:
                token = self._require_session_token(token)
            except (PublicPlayError, GameStoreError):
                token = None
            else:
                self._consume_read(token)
                self.global_config_refresh_rate_limiter.consume("global")
                self.store.touch_session(token)
        if token is None:
            # This global bucket bounds anonymous session issuance only. An
            # attacker exhausting it must not block a legitimate owner from
            # refreshing/resuming an already authenticated session.
            self.config_rate_limiter.consume("global")
            created = self.store.create_session()
            token = created["token"]
            set_cookie = True

        active_game_id = None
        get_active = getattr(self.store, "get_active_game_for_owner", None)
        if get_active is not None:
            active = get_active(token)
            if active is not None:
                active_game_id = active["id"]

        payload = {
            "csrf_token": self._csrf(token),
            "presets": [
                {
                    "id": preset.id,
                    "label": preset.label,
                    "description": preset.description,
                    "nodes": preset.nodes,
                    "temperature": preset.temperature,
                    "temp_decay_moves": preset.temp_decay_moves,
                }
                for preset in PRESETS
            ],
            "clocks": [
                {
                    "id": clock.id,
                    "label": clock.label,
                    "description": clock.description,
                    "initial_ms": clock.initial_ms,
                    "increment_ms": clock.increment_ms,
                }
                for clock in CLOCKS
            ],
            "models": [
                {
                    "id": model.id,
                    "label": model.label,
                }
                for model in self.models
            ],
            "default_model_id": self.default_model_id,
            "active_game_id": active_game_id,
        }
        return SessionConfiguration(payload, token, set_cookie)

    # -- public methods --------------------------------------------------

    def create_game(
        self,
        session_token: Optional[str],
        csrf_token: Optional[str],
        idempotency_key: Optional[str],
        body: Any,
    ) -> dict[str, Any]:
        token = self.verify_csrf(session_token, csrf_token)
        self._consume_write(token, creation=True)
        key = self._validate_idempotency_key(idempotency_key)
        request = self._validate_create_body(body)
        color = request["color"]
        if color == "random":
            color = self._random_color(token, key)
        advanced = request.get("advanced")
        if advanced is None:
            preset = _PRESET_BY_ID[request["preset_id"]]
            model = self._models_by_id[self.default_model_id]
            preset_id = preset.id
            nodes = preset.nodes
            temperature = preset.temperature
            temp_decay_moves = preset.temp_decay_moves
            clock = _CLOCK_BY_ID[request["clock_id"]].store_value()
            start_fen = START_FEN
        else:
            model = self._selectable_models_by_id[advanced["model_id"]]
            preset_id = "advanced"
            nodes = advanced["nodes"]
            temperature = advanced["temperature"]
            temp_decay_moves = advanced["temp_decay_moves"]
            clock = advanced["clock"]
            start_fen = advanced["start_fen"]

        if start_fen != START_FEN:
            validation_context = (
                "validation:"
                + hashlib.sha256(
                    (token + "\0" + key + "\0" + start_fen).encode("utf-8")
                ).hexdigest()
            )
            try:
                with self._engine_session(
                    validation_context, model.id, self.backend
                ) as engine:
                    initial_position = self._inspect(
                        engine, start_fen, []
                    )
            except Exception as exc:
                raise PublicPlayError(
                    "Start FEN is not a valid Double Move position."
                ) from exc
            if initial_position["result"] != "ongoing":
                raise PublicPlayError(
                    "Start FEN must be an ongoing position with a legal move."
                )

        with self._locks.for_key(hashlib.sha256(token.encode("ascii")).hexdigest()):
            try:
                created = self.store.create_game(
                    token,
                    start_fen=start_fen,
                    human_color=color,
                    model_id=model.id,
                    model_sha256=model.sha256,
                    preset=preset_id,
                    nodes=nodes,
                    temperature=temperature,
                    temp_decay_moves=temp_decay_moves,
                    backend=self.backend,
                    clock=clock,
                    player_name=request["display_name"] or None,
                    name_visible=request["publish_name"],
                    publish_on_completion=True,
                    idempotency_key=key,
                )
                # Durable create replay caches revision zero.  Reload so a
                # Black-side retry sees the already-persisted engine reply.
                game = self.store.get_game_for_owner(token, created["id"])
                position = self._inspect_game(game)
                if (
                    game["status"] == "active"
                    and game["engine_color"] ==
                        self._color_name(position["turn"])
                    and not game["active_moves"]
                ):
                    game, position = self._run_engine_turn(token, game)
                return {"game": self._state(game, owner=True, position=position)}
            except Exception as exc:
                raise self._map_error(exc) from exc

    def get_owner_game(
        self, session_token: Optional[str], public_id: str
    ) -> dict[str, Any]:
        token = self._require_session_token(session_token)
        self._consume_read(token)
        self.global_owner_read_rate_limiter.consume("global")
        public_id = self._validate_game_id(public_id)
        with self._locks.for_key(public_id):
            try:
                game = self.store.get_game_for_owner(token, public_id)
                position = None
                # Another process can insert a timely durable command between
                # the pending-command read and timeout settlement.  Retry the
                # read/recovery sequence a bounded number of times when the
                # store reports that race.
                for _ in range(3):
                    recovered = self._recover_pending_started_command(
                        token,
                        public_id,
                        game,
                        received_at_ms=self._now_ms(),
                    )
                    if recovered is not None:
                        return recovered
                    game = self.store.get_game_for_owner(token, public_id)
                    try:
                        game, position = self._settle_timeout_if_needed(
                            token, game
                        )
                        break
                    except PendingCommand:
                        game = self.store.get_game_for_owner(token, public_id)
                else:
                    # A continuous cross-process race is safer to expose as a
                    # transient owner-only pending state than as an HTTP
                    # conflict.  The browser will poll this state at a bounded
                    # cadence and the next GET repeats authoritative recovery.
                    return self._pending_owner_response(
                        self.store.get_game_for_owner(token, public_id)
                    )
                if position is None and game["status"] == "active":
                    position = self._inspect_game(game)
                if (
                    game["status"] == "active"
                    and position is not None
                    and position["result"] != "ongoing"
                ):
                    game = self._complete_from_position(
                        token, game, position
                    )
                elif (
                    game["status"] == "active"
                    and position is not None
                    and position["turn"]
                    == self._color_code(game["engine_color"])
                ):
                    game, position = self._run_engine_turn(token, game)
                return {"game": self._state(game, owner=True, position=position)}
            except Exception as exc:
                raise self._map_error(exc, hide_ownership=True) from exc

    def move(
        self,
        session_token: Optional[str],
        csrf_token: Optional[str],
        public_id: str,
        idempotency_key: Optional[str],
        body: Any,
    ) -> dict[str, Any]:
        received_at_ms = self._now_ms()
        token = self.verify_csrf(session_token, csrf_token)
        self._consume_write(token)
        public_id = self._validate_game_id(public_id)
        key = self._validate_idempotency_key(idempotency_key)
        request = self._validate_move_body(body)
        return self._idempotent_mutation(
            token,
            public_id,
            key,
            {"operation": "move", **request},
            lambda command: self._apply_human_move(
                token,
                public_id,
                request,
                command=command,
                received_at_ms=received_at_ms,
            ),
            received_at_ms=received_at_ms,
        )

    def takeback(
        self,
        session_token: Optional[str],
        csrf_token: Optional[str],
        public_id: str,
        idempotency_key: Optional[str],
        body: Any,
    ) -> dict[str, Any]:
        received_at_ms = self._now_ms()
        token = self.verify_csrf(session_token, csrf_token)
        self._consume_write(token)
        public_id = self._validate_game_id(public_id)
        key = self._validate_idempotency_key(idempotency_key)
        request = self._validate_revision_body(body)

        return self._idempotent_mutation(
            token,
            public_id,
            key,
            {"operation": "takeback", **request},
            lambda command: self._apply_takeback(
                token,
                public_id,
                request,
                command=command,
                received_at_ms=received_at_ms,
            ),
            received_at_ms=received_at_ms,
        )

    def resign(
        self,
        session_token: Optional[str],
        csrf_token: Optional[str],
        public_id: str,
        idempotency_key: Optional[str],
        body: Any,
    ) -> dict[str, Any]:
        received_at_ms = self._now_ms()
        token = self.verify_csrf(session_token, csrf_token)
        self._consume_write(token)
        public_id = self._validate_game_id(public_id)
        key = self._validate_idempotency_key(idempotency_key)
        request = self._validate_revision_body(body)

        return self._idempotent_mutation(
            token,
            public_id,
            key,
            {"operation": "resign", **request},
            lambda command: self._apply_resignation(
                token,
                public_id,
                request,
                command=command,
                received_at_ms=received_at_ms,
            ),
            received_at_ms=received_at_ms,
        )

    def list_public_games(
        self, *, limit: int = 50, cursor: Optional[str] = None
    ) -> dict[str, Any]:
        try:
            page = self.store.list_completed_public_games(
                limit=limit, cursor=cursor
            )
            return {
                "games": [
                    self._state(game, owner=False, summary=True)
                    for game in page["games"]
                ],
                "next_cursor": page["next_cursor"],
            }
        except Exception as exc:
            raise self._map_error(exc) from exc

    def get_public_game(self, public_id: str) -> dict[str, Any]:
        public_id = self._validate_game_id(public_id)
        try:
            game = self.store.get_public_game(public_id, include_history=True)
            return {"game": self._state(game, owner=False)}
        except Exception as exc:
            raise self._map_error(exc, hide_ownership=True) from exc

    # -- move and engine coordination -----------------------------------

    def _recover_pending_started_command(
        self,
        token: str,
        public_id: str,
        game: Mapping[str, Any],
        *,
        received_at_ms: int,
    ) -> Optional[dict[str, Any]]:
        """Report a live command or reclaim the oldest expired command.

        The store chooses the oldest ``started`` command deterministically and
        protects timeout settlement against overtaking it.  Recovery uses the
        command's original request and durable first receipt, so a GET cannot
        change either the action or its clock semantics.
        """

        pending = self.store.get_pending_started_command(token, public_id)
        if pending is None:
            return None
        if pending["lease_live"]:
            return self._pending_owner_response(
                self.store.get_game_for_owner(token, public_id)
            )

        key = self._validate_idempotency_key(pending["idempotency_key"])
        durable_request = dict(pending["request"])

        def operation(command: _CommandLease) -> dict[str, Any]:
            operation_name, request, _ = (
                self._validate_recoverable_command_request(
                    durable_request
                )
            )
            if operation_name == "move":
                return self._apply_human_move(
                    token,
                    public_id,
                    request,
                    command=command,
                    received_at_ms=received_at_ms,
                )
            if operation_name == "takeback":
                return self._apply_takeback(
                    token,
                    public_id,
                    request,
                    command=command,
                    received_at_ms=received_at_ms,
                )
            return self._apply_resignation(
                token,
                public_id,
                request,
                command=command,
                received_at_ms=received_at_ms,
            )

        try:
            response = self._idempotent_mutation(
                token,
                public_id,
                key,
                durable_request,
                operation,
                received_at_ms=received_at_ms,
            )
            if (
                self.store.get_pending_started_command(token, public_id)
                is not None
            ):
                return self._pending_owner_response(
                    self.store.get_game_for_owner(token, public_id)
                )
            return response
        except PublicPlayError as exc:
            if exc.code == "request_in_progress":
                current = self.store.get_game_for_owner(token, public_id)
                return self._pending_owner_response(current)
            if 400 <= exc.status < 500:
                # The recoverable request was stale or invalid.  The command
                # lifecycle cached that deterministic failure.  If an older
                # failed command had hidden another started request, keep the
                # owner locked until a later bounded GET recovers that next
                # command instead of letting it be overtaken.
                current = self.store.get_game_for_owner(token, public_id)
                if (
                    self.store.get_pending_started_command(token, public_id)
                    is not None
                ):
                    return self._pending_owner_response(current)
                return None
            raise

    def _pending_owner_response(
        self, game: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "game": self._state(
                game,
                owner=True,
                pending_command=True,
            )
        }

    @classmethod
    def _validate_recoverable_command_request(
        cls, request: Any
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        if not isinstance(request, Mapping):
            raise PublicPlayError(
                "Stored command request is invalid.",
                409,
                "invalid_stored_command",
            )
        operation = request.get("operation")
        if operation == "move" and set(request) == {
            "operation",
            "move",
            "expected_revision",
        }:
            body = cls._validate_move_body(
                {
                    "move": request["move"],
                    "expected_revision": request["expected_revision"],
                }
            )
        elif operation in ("takeback", "resign") and set(request) == {
            "operation",
            "expected_revision",
        }:
            body = cls._validate_revision_body(
                {"expected_revision": request["expected_revision"]}
            )
        else:
            raise PublicPlayError(
                "Stored command request is invalid.",
                409,
                "invalid_stored_command",
            )
        return operation, body, {"operation": operation, **body}

    def _apply_takeback(
        self,
        token: str,
        public_id: str,
        request: Mapping[str, Any],
        *,
        command: _CommandLease,
        received_at_ms: int,
    ) -> dict[str, Any]:
        if command.stage == "started":
            operation_received_at_ms = self._started_command_receipt(
                command, received_at_ms
            )
            game = self.store.takeback(
                token,
                public_id,
                expected_revision=request["expected_revision"],
                received_at_ms=operation_received_at_ms,
                idempotency_key=command.key,
                command_lease=command.lease_token,
                command_stage="takeback_recorded",
            )
            if game["status"] == "completed":
                self._mark_command_stage(
                    command,
                    "game_completed",
                    game,
                    stage_data={"revision": game["revision"]},
                )
            else:
                self._mark_command_stage(
                    command,
                    "takeback_recorded",
                    game,
                    stage_data={"revision": game["revision"]},
                )
        elif command.stage == "takeback_recorded":
            game = self.store.get_game_for_owner(token, public_id)
            self._require_recovery_revision(command, game)
        elif command.stage == "game_completed":
            game = self.store.get_game_for_owner(token, public_id)
            self._require_recovery_revision(command, game)
            if (
                game["status"] != "completed"
                or game["termination"] != "timeout"
            ):
                raise RuntimeError(
                    "recovered takeback timeout is not terminal"
                )
        else:
            raise RuntimeError(
                f"unknown takeback recovery stage {command.stage!r}"
            )
        position = (
            self._inspect_game(game)
            if game["status"] == "active"
            else None
        )
        return {"game": self._state(game, owner=True, position=position)}

    def _apply_resignation(
        self,
        token: str,
        public_id: str,
        request: Mapping[str, Any],
        *,
        command: _CommandLease,
        received_at_ms: int,
    ) -> dict[str, Any]:
        if command.stage == "started":
            operation_received_at_ms = self._started_command_receipt(
                command, received_at_ms
            )
            current = self.store.get_game_for_owner(token, public_id)
            game = self.store.resign_game(
                token,
                public_id,
                expected_revision=request["expected_revision"],
                resigning_color=current["human_color"],
                received_at_ms=operation_received_at_ms,
                idempotency_key=command.key,
                command_lease=command.lease_token,
                command_stage="game_completed",
            )
            self._mark_command_stage(
                command,
                "game_completed",
                game,
                stage_data={"revision": game["revision"]},
            )
        elif command.stage == "game_completed":
            game = self.store.get_game_for_owner(token, public_id)
            self._require_recovery_revision(command, game)
            if game["status"] != "completed":
                raise RuntimeError("recovered resignation is not completed")
        else:
            raise RuntimeError(
                f"unknown resignation recovery stage {command.stage!r}"
            )
        return {"game": self._state(game, owner=True)}

    def _apply_human_move(
        self,
        token: str,
        public_id: str,
        request: Mapping[str, Any],
        *,
        command: _CommandLease,
        received_at_ms: int,
    ) -> dict[str, Any]:
        if command.stage == "human_recorded":
            return self._resume_after_human_stage(
                token,
                public_id,
                command,
                received_at_ms=received_at_ms,
            )
        if command.stage == "engine_recorded":
            return self._resume_after_engine_stage(
                token,
                public_id,
                command,
                received_at_ms=received_at_ms,
            )
        if command.stage == "game_completed":
            game = self.store.get_game_for_owner(token, public_id)
            self._require_recovery_revision(command, game)
            if game["status"] != "completed":
                raise RuntimeError("recovered game completion is not terminal")
            return {"game": self._state(game, owner=True)}
        if command.stage != "started":
            raise RuntimeError(
                f"unknown move recovery stage {command.stage!r}"
            )
        received_at_ms = self._started_command_receipt(
            command, received_at_ms
        )

        game = self.store.get_game_for_owner(
            token, public_id, include_history=True
        )
        if game["revision"] != request["expected_revision"]:
            raise RevisionConflict(
                request["expected_revision"], game["revision"]
            )
        expired_color = self._expired_running_color(
            game, now_ms=received_at_ms
        )
        if expired_color is not None:
            game = self.store.timeout_game(
                token,
                public_id,
                expected_revision=game["revision"],
                timed_out_color=expired_color,
                idempotency_key=command.key,
                command_lease=command.lease_token,
                command_stage="game_completed",
            )
            self._mark_command_stage(
                command,
                "game_completed",
                game,
                stage_data={"revision": game["revision"]},
            )
            position = self._inspect_game(game)
            return {
                "game": self._state(game, owner=True, position=position)
            }

        context_id = self._engine_context_id(public_id)
        model_id, backend = self._game_engine_config(game)
        with self._engine_session(context_id, model_id, backend) as engine:
            before = self._inspect(engine, game["start_fen"], game["active_moves"])
            human_turn = self._color_code(game["human_color"])
            if before["turn"] != human_turn:
                raise PublicPlayError(
                    "It is not the human player's turn.",
                    409,
                    "not_your_turn",
                )
            move = request["move"]
            if move not in before["legal"]:
                raise PublicPlayError("Move is not legal.", 422, "illegal_move")
            before_mid = before["ability"]["mid"]
            after_moves = list(game["active_moves"]) + [move]
            after = self._inspect(engine, game["start_fen"], after_moves)
            complete = self._validate_leg_transition(
                move, before, after, actor="human"
            )
            clock_state, timed_out = self._clock_after_turn_progress(
                game,
                game["human_color"],
                complete=complete,
                charge_until_ms=received_at_ms,
            )
            if timed_out:
                game = self.store.timeout_game(
                    token,
                    public_id,
                    expected_revision=game["revision"],
                    timed_out_color=game["human_color"],
                    idempotency_key=command.key,
                    command_lease=command.lease_token,
                    command_stage="game_completed",
                )
                self._mark_command_stage(
                    command,
                    "game_completed",
                    game,
                    stage_data={"revision": game["revision"]},
                )
                return {"game": self._state(game, owner=True, position=before)}

            incomplete_turn = self._incomplete_human_turn(game)
            if before_mid:
                if incomplete_turn is None:
                    raise RuntimeError(
                        "storage has no unfinished turn for a mid-double position"
                    )
                game = self.store.append_turn_leg(
                    token,
                    public_id,
                    incomplete_turn["id"],
                    expected_revision=game["revision"],
                    uci=move,
                    after_fen=after["fen"],
                    clock_state=clock_state,
                    idempotency_key=command.key,
                    command_lease=command.lease_token,
                    command_stage="human_recorded",
                )
            else:
                game = self.store.record_turn(
                    token,
                    public_id,
                    expected_revision=game["revision"],
                    actor="human",
                    color=game["human_color"],
                    legs=[{"uci": move, "after_fen": after["fen"]}],
                    clock_state=clock_state,
                    complete=complete,
                    idempotency_key=command.key,
                    command_lease=command.lease_token,
                    command_stage="human_recorded",
                )
            self._mark_command_stage(
                command,
                "human_recorded",
                game,
                stage_data={
                    "revision": game["revision"],
                    "recorded_turn_id": game["recorded_turn_id"],
                    "actor": "human",
                    "turn_complete": complete,
                },
            )

            if not complete:
                return {"game": self._state(game, owner=True, position=after)}
            if after["result"] != "ongoing":
                game = self._complete_from_position(
                    token, game, after, command=command
                )
                return {"game": self._state(game, owner=True, position=after)}

            game, final = self._run_engine_turn_with_engine(
                token, game, engine, after, command=command
            )
            return {"game": self._state(game, owner=True, position=final)}

    def _resume_after_human_stage(
        self,
        token: str,
        public_id: str,
        command: _CommandLease,
        *,
        received_at_ms: int,
    ) -> dict[str, Any]:
        game = self.store.get_game_for_owner(
            token, public_id, include_history=True
        )
        stage_revision = command.stage_data.get("revision")
        turn_id = command.stage_data.get("recorded_turn_id")
        turn_complete = command.stage_data.get("turn_complete")
        if (
            isinstance(stage_revision, bool)
            or not isinstance(stage_revision, int)
            or not isinstance(turn_id, str)
            or not isinstance(turn_complete, bool)
        ):
            raise RuntimeError("human recovery stage is malformed")
        matching = [
            turn for turn in game["turns"] if turn.get("id") == turn_id
        ]
        if len(matching) != 1 or matching[0].get("actor") != "human":
            raise RuntimeError("recorded human turn is missing")

        if self._is_system_completion_after_stage(game, stage_revision):
            self._advance_recovered_completion(
                token, public_id, command, game
            )
            return {"game": self._state(game, owner=True)}

        # A GET recovery may already have supplied the engine reply.  Reconcile
        # it only when the exact adjacent active engine turn is present.
        if game["revision"] != stage_revision:
            active = [turn for turn in game["turns"] if turn.get("active")]
            human_index = next(
                (
                    index
                    for index, turn in enumerate(active)
                    if turn.get("id") == turn_id
                ),
                None,
            )
            engine_already_recorded = (
                human_index is not None
                and human_index + 1 < len(active)
                and active[human_index + 1].get("actor") == "engine"
                and active[human_index + 1].get("completed") is True
                and game["revision"] in (
                    stage_revision + 1,
                    stage_revision + 2,
                )
            )
            if not engine_already_recorded:
                raise RuntimeError(
                    "game diverged from recorded human command stage"
                )
            if game["status"] == "completed":
                self.store.advance_command(
                    token,
                    public_id,
                    command.key,
                    lease_token=command.lease_token,
                    stage="game_completed",
                    stage_data={"revision": game["revision"]},
                )
                self._mark_command_stage(
                    command,
                    "game_completed",
                    game,
                    stage_data={"revision": game["revision"]},
                )
                return {"game": self._state(game, owner=True)}
            position = self._inspect_game(game)
            self.store.advance_command(
                token,
                public_id,
                command.key,
                lease_token=command.lease_token,
                stage="engine_recorded",
                stage_data={
                    "revision": game["revision"],
                    "actor": "engine",
                    "turn_complete": True,
                },
            )
            self._mark_command_stage(
                command,
                "engine_recorded",
                game,
                stage_data={"revision": game["revision"]},
            )
            return {"game": self._state(game, owner=True, position=position)}

        position = self._inspect_game(game)
        if position["result"] != "ongoing":
            if not turn_complete:
                raise RuntimeError(
                    "an incomplete human turn cannot be terminal"
                )
            game = self._complete_from_position(
                token, game, position, command=command
            )
            return {"game": self._state(game, owner=True, position=position)}
        timed_out = self._complete_recovered_timeout(
            token,
            public_id,
            game,
            command,
            received_at_ms=received_at_ms,
        )
        if timed_out is not None:
            return {"game": self._state(timed_out, owner=True)}
        if not turn_complete:
            return {"game": self._state(game, owner=True, position=position)}
        context_id = self._engine_context_id(public_id)
        model_id, backend = self._game_engine_config(game)
        with self._engine_session(context_id, model_id, backend) as engine:
            position = self._inspect(
                engine, game["start_fen"], game["active_moves"]
            )
            game, final = self._run_engine_turn_with_engine(
                token, game, engine, position, command=command
            )
        return {"game": self._state(game, owner=True, position=final)}

    def _resume_after_engine_stage(
        self,
        token: str,
        public_id: str,
        command: _CommandLease,
        *,
        received_at_ms: int,
    ) -> dict[str, Any]:
        game = self.store.get_game_for_owner(
            token, public_id, include_history=True
        )
        stage_revision = command.stage_data.get("revision")
        turn_id = command.stage_data.get("recorded_turn_id")
        if (
            isinstance(stage_revision, bool)
            or not isinstance(stage_revision, int)
            or not isinstance(turn_id, str)
        ):
            raise RuntimeError("engine recovery stage is malformed")
        matching = [
            turn for turn in game["turns"] if turn.get("id") == turn_id
        ]
        if len(matching) != 1 or matching[0].get("actor") != "engine":
            raise RuntimeError("recorded engine turn is missing")
        if self._is_system_completion_after_stage(game, stage_revision):
            self._advance_recovered_completion(
                token, public_id, command, game
            )
            return {"game": self._state(game, owner=True)}
        self._require_recovery_revision(command, game)
        position = self._inspect_game(game)
        if position["result"] != "ongoing":
            game = self._complete_from_position(
                token, game, position, command=command
            )
        else:
            timed_out = self._complete_recovered_timeout(
                token,
                public_id,
                game,
                command,
                received_at_ms=received_at_ms,
            )
            if timed_out is not None:
                game = timed_out
                position = None
        return {"game": self._state(game, owner=True, position=position)}

    def _complete_recovered_timeout(
        self,
        token: str,
        public_id: str,
        game: Mapping[str, Any],
        command: _CommandLease,
        *,
        received_at_ms: int,
    ) -> Optional[dict[str, Any]]:
        expired_color = self._expired_running_color(
            game, now_ms=received_at_ms
        )
        if expired_color is None:
            return None
        completed = self.store.timeout_game(
            token,
            public_id,
            expected_revision=game["revision"],
            timed_out_color=expired_color,
            idempotency_key=command.key,
            command_lease=command.lease_token,
            command_stage="game_completed",
        )
        self._mark_command_stage(
            command,
            "game_completed",
            completed,
            stage_data={"revision": completed["revision"]},
        )
        return completed

    def _run_engine_turn(
        self,
        token: str,
        game: Mapping[str, Any],
        *,
        command: Optional[_CommandLease] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        context_id = self._engine_context_id(game["id"])
        model_id, backend = self._game_engine_config(game)
        with self._engine_session(context_id, model_id, backend) as engine:
            before = self._inspect(
                engine, game["start_fen"], game["active_moves"]
            )
            return self._run_engine_turn_with_engine(
                token, dict(game), engine, before, command=command
            )

    def _run_engine_turn_with_engine(
        self,
        token: str,
        game: Mapping[str, Any],
        engine: EngineLike,
        before: Mapping[str, Any],
        *,
        command: Optional[_CommandLease] = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if before["result"] != "ongoing":
            completed = self._complete_from_position(
                token, game, before, command=command
            )
            return completed, dict(before)
        if before["turn"] != self._color_code(game["engine_color"]):
            raise RuntimeError("authoritative engine turn does not match game state")
        if not before["legal"]:
            raise RuntimeError("ongoing engine position has no legal moves")

        self._game_engine_config(game)
        nodes = game.get("nodes")
        temperature = game.get("temperature")
        temp_decay_moves = game.get("temp_decay_moves")
        if (
            isinstance(nodes, bool)
            or not isinstance(nodes, int)
            or not 1 <= nodes <= MAX_PLAY_NODES
        ):
            raise RuntimeError("stored game has invalid search nodes")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or not 0 <= float(temperature) <= 10
        ):
            raise RuntimeError("stored game has invalid search temperature")
        if (
            isinstance(temp_decay_moves, bool)
            or not isinstance(temp_decay_moves, int)
            or not 0 <= temp_decay_moves <= MAX_TEMP_DECAY_MOVES
        ):
            raise RuntimeError(
                "stored game has invalid search temperature decay"
            )
        heartbeat = (
            self._heartbeat_command(token, game["id"], command)
            if command is not None
            else nullcontext()
        )
        with heartbeat:
            try:
                engine.set_option(
                    "Temperature", self._float_option(float(temperature))
                )
                engine.set_option("TempDecayMoves", str(temp_decay_moves))
                engine.set_option(
                    "PolicyOnlyAvoidTwofold",
                    "true" if nodes == 1 else "false",
                )

                active_moves = list(game["active_moves"])
                legs: list[dict[str, str]] = []
                position = dict(before)
                while True:
                    best, _ponder, _info = engine.go_nodes(nodes)
                    if (
                        not isinstance(best, str)
                        or not _MOVE_RE.fullmatch(best)
                        or best not in position["legal"]
                    ):
                        raise RuntimeError(
                            "engine returned a non-authoritative move"
                        )
                    active_moves.append(best)
                    next_position = self._inspect(
                        engine, game["start_fen"], active_moves
                    )
                    complete = self._validate_leg_transition(
                        best, position, next_position, actor="engine"
                    )
                    legs.append(
                        {"uci": best, "after_fen": next_position["fen"]}
                    )
                    position = next_position
                    if complete:
                        break
                    if len(legs) >= 2:
                        raise RuntimeError(
                            "engine double move did not finish in two legs"
                        )
            finally:
                # Analysis shares the one lc0 process. Never leak a public-
                # play sampling configuration into deterministic analysis.
                try:
                    engine.set_option("PolicyOnlyAvoidTwofold", "false")
                finally:
                    try:
                        engine.set_option("Temperature", "0")
                    finally:
                        engine.set_option("TempDecayMoves", "0")

        clock_state, timed_out = self._clock_after_engine_search(game)
        if timed_out:
            command_arguments = (
                {
                    "idempotency_key": command.key,
                    "command_lease": command.lease_token,
                    "command_stage": "game_completed",
                }
                if command is not None
                else {}
            )
            completed = self.store.timeout_game(
                token,
                game["id"],
                expected_revision=game["revision"],
                timed_out_color=game["engine_color"],
                **command_arguments,
            )
            if command is not None:
                self._mark_command_stage(
                    command,
                    "game_completed",
                    completed,
                    stage_data={"revision": completed["revision"]},
                )
            return completed, dict(before)

        command_arguments = (
            {
                "idempotency_key": command.key,
                "command_lease": command.lease_token,
                "command_stage": "engine_recorded",
            }
            if command is not None
            else {}
        )
        updated = self.store.record_turn(
            token,
            game["id"],
            expected_revision=game["revision"],
            actor="engine",
            color=game["engine_color"],
            legs=legs,
            clock_state=clock_state,
            complete=True,
            **command_arguments,
        )
        if command is not None:
            self._mark_command_stage(
                command,
                "engine_recorded",
                updated,
                stage_data={
                    "revision": updated["revision"],
                    "recorded_turn_id": updated["recorded_turn_id"],
                    "actor": "engine",
                    "turn_complete": True,
                },
            )
        if position["result"] != "ongoing":
            updated = self._complete_from_position(
                token, updated, position, command=command
            )
        return updated, position

    def _complete_from_position(
        self,
        token: str,
        game: Mapping[str, Any],
        position: Mapping[str, Any],
        *,
        command: Optional[_CommandLease] = None,
    ) -> dict[str, Any]:
        result = {
            "white": "1-0",
            "black": "0-1",
            "draw": "1/2-1/2",
        }.get(position["result"])
        if result is None:
            raise RuntimeError("cannot complete a game from an ongoing position")
        command_arguments = (
            {
                "idempotency_key": command.key,
                "command_lease": command.lease_token,
                "command_stage": "game_completed",
            }
            if command is not None
            else {}
        )
        completed = self.store.complete_game(
            token,
            game["id"],
            expected_revision=game["revision"],
            result=result,
            termination="variant_result",
            actor="system",
            **command_arguments,
        )
        if command is not None:
            self._mark_command_stage(
                command,
                "game_completed",
                completed,
                stage_data={"revision": completed["revision"]},
            )
        return completed

    # -- clocks ----------------------------------------------------------

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _remaining_clock(
        self, game: Mapping[str, Any], *, now_ms: Optional[int] = None
    ) -> dict[str, Any]:
        state = dict(game["clock_state"])
        if game["clock"]["mode"] == "unlimited":
            return state
        now = self._now_ms() if now_ms is None else now_ms
        running = state["running_color"]
        anchor = state["anchor_at_ms"]
        if game["status"] == "active" and running is not None and anchor is not None:
            key = f"{running}_ms"
            state[key] = max(0, state[key] - max(0, now - anchor))
            state["anchor_at_ms"] = now
        return state

    def _settle_timeout_if_needed(
        self, token: str, game: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Optional[dict[str, Any]]]:
        if game["status"] != "active" or game["clock"]["mode"] == "unlimited":
            return dict(game), None
        running = self._expired_running_color(game)
        if running is None:
            return dict(game), None
        completed = self.store.timeout_game(
            token,
            game["id"],
            expected_revision=game["revision"],
            timed_out_color=running,
        )
        return completed, None

    def _expired_running_color(
        self,
        game: Mapping[str, Any],
        *,
        now_ms: Optional[int] = None,
    ) -> Optional[str]:
        if (
            game["status"] != "active"
            or game["clock"]["mode"] == "unlimited"
        ):
            return None
        clock_state = self._remaining_clock(game, now_ms=now_ms)
        running = clock_state["running_color"]
        if (
            running is not None
            and clock_state[f"{running}_ms"] <= 0
        ):
            return running
        return None

    def _clock_after_turn_progress(
        self,
        game: Mapping[str, Any],
        color: str,
        *,
        complete: bool,
        charge_until_ms: Optional[int] = None,
    ) -> tuple[dict[str, Any], bool]:
        if game["clock"]["mode"] == "unlimited":
            return {
                "white_ms": None,
                "black_ms": None,
                "running_color": None,
                "anchor_at_ms": None,
            }, False
        anchor_now = self._now_ms()
        charged_at = (
            anchor_now if charge_until_ms is None else charge_until_ms
        )
        state = self._remaining_clock(game, now_ms=charged_at)
        if state["running_color"] != color:
            raise RuntimeError("persisted clock is running for the wrong player")
        remaining_key = f"{color}_ms"
        if state[remaining_key] <= 0:
            return state, True
        if complete:
            state[remaining_key] += game["clock"]["increment_ms"]
            state["running_color"] = self._other_color(color)
        state["anchor_at_ms"] = anchor_now
        return state, False

    def _clock_after_engine_search(
        self, game: Mapping[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        if game["clock"]["mode"] == "unlimited":
            return {
                "white_ms": None,
                "black_ms": None,
                "running_color": None,
                "anchor_at_ms": None,
            }, False
        state = self._remaining_clock(game)
        color = game["engine_color"]
        if state["running_color"] != color:
            raise RuntimeError("persisted clock is not running for the engine")
        key = f"{color}_ms"
        if state[key] <= 0:
            state["anchor_at_ms"] = self._now_ms()
            return state, True
        state[key] += game["clock"]["increment_ms"]
        state["running_color"] = game["human_color"]
        state["anchor_at_ms"] = self._now_ms()
        return state, False

    # -- engine validation ----------------------------------------------

    @staticmethod
    def _position_cache_key(
        game: Mapping[str, Any],
    ) -> Optional[tuple[str, int]]:
        public_id = game.get("id")
        revision = game.get("revision")
        if (
            not isinstance(public_id, str)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
        ):
            return None
        return public_id, revision

    def _cached_position(
        self, game: Mapping[str, Any]
    ) -> Optional[dict[str, Any]]:
        key = self._position_cache_key(game)
        if key is None:
            return None
        with self._position_cache_lock:
            position = self._position_cache.pop(key, None)
            if position is None:
                return None
            self._position_cache[key] = position
            return copy.deepcopy(position)

    def _remember_position(
        self,
        game: Mapping[str, Any],
        position: Mapping[str, Any],
    ) -> None:
        key = self._position_cache_key(game)
        if key is None:
            return
        snapshot = copy.deepcopy(dict(position))
        with self._position_cache_lock:
            self._position_cache.pop(key, None)
            self._position_cache[key] = snapshot
            while len(self._position_cache) > self._position_cache_limit:
                self._position_cache.popitem(last=False)

    def _inspect_game(self, game: Mapping[str, Any]) -> dict[str, Any]:
        model_id, backend = self._game_engine_config(game)
        cached = self._cached_position(game)
        if cached is not None:
            return cached
        context_id = self._engine_context_id(game["id"])
        with self._engine_session(context_id, model_id, backend) as engine:
            position = self._inspect(
                engine, game["start_fen"], list(game["active_moves"])
            )
        self._remember_position(game, position)
        return position

    @staticmethod
    def _inspect(
        engine: EngineLike, start_fen: str, moves: list[str]
    ) -> dict[str, Any]:
        engine.set_position(start_fen, moves)
        raw = engine.position_info()
        if not isinstance(raw, Mapping):
            raise RuntimeError("engine returned invalid position metadata")
        fen = raw.get("fen")
        turn = raw.get("turn")
        result = raw.get("result")
        ability = raw.get("ability")
        legal = raw.get("legal")
        if not isinstance(fen, str) or not 4 <= len(fen.split()) <= 7:
            raise RuntimeError("engine returned invalid authoritative FEN")
        if turn not in ("w", "b"):
            raise RuntimeError("engine returned invalid side to move")
        if result not in ("ongoing", "white", "black", "draw"):
            raise RuntimeError("engine returned invalid result")
        if (
            not isinstance(ability, Mapping)
            or any(
                not isinstance(ability.get(key), bool)
                for key in ("white", "black", "mid")
            )
        ):
            raise RuntimeError("engine returned invalid ability state")
        if (
            not isinstance(legal, list)
            or any(not isinstance(move, str) or not _MOVE_RE.fullmatch(move) for move in legal)
            or len(set(legal)) != len(legal)
        ):
            raise RuntimeError("engine returned invalid legal moves")
        if result == "ongoing" and not legal:
            raise RuntimeError("ongoing position has no legal moves")
        if result != "ongoing" and legal:
            raise RuntimeError("terminal position unexpectedly has legal moves")
        return {
            "fen": fen,
            "turn": turn,
            "result": result,
            "ability": {
                "white": ability["white"],
                "black": ability["black"],
                "mid": ability["mid"],
            },
            "legal": list(legal),
        }

    @staticmethod
    def _validate_leg_transition(
        move: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        *,
        actor: str,
    ) -> bool:
        if move.endswith("+"):
            if before["ability"]["mid"]:
                raise RuntimeError(f"{actor} attempted a nested double move")
            if not after["ability"]["mid"]:
                raise RuntimeError(f"{actor} ability move did not enter its second leg")
            if after["result"] != "ongoing":
                raise RuntimeError("a first double-move leg cannot be terminal")
            if after["turn"] != before["turn"]:
                raise RuntimeError("side changed before double move completed")
            return False
        if after["ability"]["mid"]:
            raise RuntimeError(f"{actor} ordinary move left a mid-double state")
        if after["result"] == "ongoing" and after["turn"] == before["turn"]:
            raise RuntimeError("side did not change after a completed turn")
        return True

    # -- response shaping ------------------------------------------------

    def _state(
        self,
        game: Mapping[str, Any],
        *,
        owner: bool,
        position: Optional[Mapping[str, Any]] = None,
        summary: bool = False,
        pending_command: bool = False,
    ) -> dict[str, Any]:
        if position is not None:
            self._remember_position(game, position)
        preset = _PRESET_BY_ID.get(game["preset"])
        preset_label = (
            preset.label
            if preset is not None
            else "Advanced"
            if game["preset"] == "advanced"
            else game["preset"]
        )
        clock_choice = self._clock_choice_for_store(game["clock"])
        human = game["human_color"]
        engine = game["engine_color"]
        human_name = (
            game.get("player_name")
            if owner or game.get("name_visible") is True
            else None
        )
        players = {
            human: {
                "type": "human",
                "name": human_name or "Anonymous challenger",
            },
            engine: {
                "type": "engine",
                "name": "Double Move Engine",
                "preset_label": preset_label,
            },
        }
        state = {
            "id": game["id"],
            "status": game["status"],
            "human_color": human,
            "engine_color": engine,
            "players": {
                "white": players["white"],
                "black": players["black"],
            },
            "preset_id": game["preset"],
            "preset_label": preset_label,
            "model_id": game["model"]["id"],
            "nodes": game["nodes"],
            "temperature": game["temperature"],
            "temp_decay_moves": game["temp_decay_moves"],
            "backend": game["backend"],
            "clock_id": clock_choice.id,
            "clock_label": clock_choice.label,
            "revision": game["revision"],
            "result": game["result"],
            "termination": game["termination"],
            "takeback_count": game["takeback_count"],
            "created_at": game["created_at_ms"],
            "updated_at": game["updated_at_ms"],
            "completed_at": game["completed_at_ms"],
        }
        if summary:
            state["final_fen"] = game["final_fen"]
        if owner:
            state["name_visible"] = bool(game.get("name_visible"))
            state["pending_command"] = bool(pending_command)
        if summary:
            return state

        moves = list(game.get("active_moves", []))
        state.update(
            {
                "initial_fen": game["start_fen"],
                "current_fen": (
                    position["fen"]
                    if position is not None
                    else game["current_fen"]
                ),
                "moves": moves,
                "legal_moves": (
                    list(position["legal"])
                    if game["status"] == "active" and position is not None
                    else []
                ),
                "to_move": (
                    self._color_name(position["turn"])
                    if position is not None
                    else self._color_from_fen(game["current_fen"])
                ),
                "ability": (
                    dict(position["ability"])
                    if position is not None
                    else {"white": False, "black": False, "mid": False}
                ),
                "clocks": self._clock_projection(game, clock_choice),
                "server_time_ms": self._now_ms(),
            }
        )
        return state

    def _clock_projection(
        self, game: Mapping[str, Any], choice: ClockChoice
    ) -> dict[str, Any]:
        if game["clock"]["mode"] == "unlimited":
            return {
                "id": choice.id,
                "label": choice.label,
                "unlimited": True,
                "white_ms": None,
                "black_ms": None,
                "running_color": None,
            }
        state = self._remaining_clock(game)
        return {
            "id": choice.id,
            "label": choice.label,
            "unlimited": False,
            "initial_ms": game["clock"]["initial_ms"],
            "increment_ms": game["clock"]["increment_ms"],
            "white_ms": state["white_ms"],
            "black_ms": state["black_ms"],
            "running_color": state["running_color"],
        }

    @staticmethod
    def _clock_choice_for_store(clock: Mapping[str, Any]) -> ClockChoice:
        for choice in CLOCKS:
            if choice.store_value() == dict(clock):
                return choice
        if clock.get("mode") == "unlimited":
            return ClockChoice("unlimited", "Unlimited", "No clock", None, 0)
        initial = clock.get("initial_ms")
        increment = clock.get("increment_ms")
        if (
            isinstance(initial, bool)
            or not isinstance(initial, int)
            or isinstance(increment, bool)
            or not isinstance(increment, int)
        ):
            raise RuntimeError("stored game has invalid clock settings")
        initial_seconds, initial_remainder = divmod(initial, 1000)
        minutes, seconds = divmod(initial_seconds, 60)
        initial_label = f"{minutes}:{seconds:02d}"
        if initial_remainder:
            initial_label += (
                f".{initial_remainder:03d}".rstrip("0")
            )
        increment_seconds, increment_remainder = divmod(increment, 1000)
        increment_label = str(increment_seconds)
        if increment_remainder:
            increment_label += (
                f".{increment_remainder:03d}".rstrip("0")
            )
        identifier = f"custom-{initial}-{increment}"
        return ClockChoice(
            identifier,
            f"{initial_label} + {increment_label}",
            "Custom time control",
            initial,
            increment,
        )

    # -- command lifecycle and validation -------------------------------

    def _idempotent_mutation(
        self,
        token: str,
        public_id: str,
        key: str,
        request: Mapping[str, Any],
        operation: Callable[[_CommandLease], dict[str, Any]],
        *,
        received_at_ms: Optional[int] = None,
    ) -> dict[str, Any]:
        with self._locks.for_key(public_id):
            command = None
            try:
                begun = self.store.begin_command(
                    token,
                    public_id,
                    key,
                    request,
                    received_at_ms=received_at_ms,
                )
                if begun["status"] == "completed":
                    return begun["response"]
                if begun["status"] == "failed":
                    raise self._error_from_payload(begun["error"])
                if not begun.get("acquired"):
                    raise PublicPlayError(
                        "This request is still being processed.",
                        409,
                        "request_in_progress",
                    )
                command = _CommandLease(
                    key=key,
                    lease_token=begun["lease_token"],
                    stage=begun["stage"],
                    stage_data=dict(begun.get("stage_data") or {}),
                    attempt=begun["attempt"],
                    reclaimed=bool(begun.get("reclaimed")),
                )
                response = operation(command)
                self.store.complete_command(
                    token,
                    public_id,
                    key,
                    response,
                    lease_token=command.lease_token,
                )
                return response
            except PublicPlayError as exc:
                if command is not None:
                    self._finish_failed_attempt(
                        token, public_id, command, exc
                    )
                raise
            except Exception as exc:
                mapped = self._map_error(exc, hide_ownership=True)
                if command is not None:
                    self._finish_failed_attempt(
                        token, public_id, command, mapped
                    )
                raise mapped from exc

    def _finish_failed_attempt(
        self,
        token: str,
        public_id: str,
        command: _CommandLease,
        error: PublicPlayError,
    ) -> None:
        try:
            if command.stage == "started":
                self.store.fail_command(
                    token,
                    public_id,
                    command.key,
                    {
                        "error": str(error),
                        "code": error.code,
                        "status": error.status,
                    },
                    lease_token=command.lease_token,
                )
            else:
                # A durable game transition already happened.  Preserve its
                # recovery stage and let the next identical request resume it.
                self.store.release_command(
                    token,
                    public_id,
                    command.key,
                    lease_token=command.lease_token,
                )
        except Exception:
            pass

    def _mark_command_stage(
        self,
        command: _CommandLease,
        stage: str,
        game: Mapping[str, Any],
        *,
        stage_data: Optional[Mapping[str, Any]] = None,
    ) -> None:
        command.stage = stage
        command.stage_data = dict(stage_data or {})
        if self._stage_hook is not None:
            self._stage_hook(stage, game)

    @staticmethod
    def _started_command_receipt(
        command: _CommandLease, fallback_received_at_ms: int
    ) -> int:
        """Use the first durable receipt while no game transition exists."""

        value = command.stage_data.get("received_at_ms")
        if value is None:
            # Compatibility for a pre-upgrade command still at ``started``.
            # The retry time is conservative for clocks and cannot revive an
            # expired game.
            return fallback_received_at_ms
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError("started command receipt metadata is invalid")
        return value

    @staticmethod
    def _is_system_completion_after_stage(
        game: Mapping[str, Any], stage_revision: int
    ) -> bool:
        """Recognize a terminal transition safely performed by a later GET."""

        return (
            game.get("status") == "completed"
            and game.get("revision") == stage_revision + 1
            and game.get("termination")
            in ("variant_result", "timeout")
        )

    def _advance_recovered_completion(
        self,
        token: str,
        public_id: str,
        command: _CommandLease,
        game: Mapping[str, Any],
    ) -> None:
        self.store.advance_command(
            token,
            public_id,
            command.key,
            lease_token=command.lease_token,
            stage="game_completed",
            stage_data={"revision": game["revision"]},
        )
        self._mark_command_stage(
            command,
            "game_completed",
            game,
            stage_data={"revision": game["revision"]},
        )

    @staticmethod
    def _require_recovery_revision(
        command: _CommandLease, game: Mapping[str, Any]
    ) -> None:
        revision = command.stage_data.get("revision")
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or game.get("revision") != revision
        ):
            raise RuntimeError(
                "game state no longer matches the durable command stage"
            )

    @contextmanager
    def _heartbeat_command(
        self, token: str, public_id: str, command: _CommandLease
    ):
        stopped = threading.Event()
        failures: list[Exception] = []
        lease_seconds = getattr(self.store, "command_lease_seconds", 30)
        interval = max(1.0, min(10.0, lease_seconds / 3))

        def heartbeat() -> None:
            while not stopped.wait(interval):
                try:
                    self.store.heartbeat_command(
                        token,
                        public_id,
                        command.key,
                        lease_token=command.lease_token,
                    )
                except Exception as exc:
                    failures.append(exc)
                    return

        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            yield
        finally:
            stopped.set()
            worker.join(timeout=interval + 1)
        if failures:
            raise failures[0]

    @staticmethod
    def _error_from_payload(payload: Any) -> PublicPlayError:
        if not isinstance(payload, Mapping):
            return PublicPlayError("The cached request failed.", 409, "request_failed")
        message = payload.get("error")
        code = payload.get("code")
        status = payload.get("status")
        if not isinstance(message, str):
            message = "The cached request failed."
        if not isinstance(code, str):
            code = "request_failed"
        if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
            status = 409
        return PublicPlayError(message, status, code)

    def _validate_create_body(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, Mapping):
            raise PublicPlayError("JSON body must be an object.")
        allowed = {
            "display_name",
            "publish_name",
            "color",
            "preset_id",
            "clock_id",
            "advanced",
        }
        if set(body) - allowed:
            raise PublicPlayError("Game request contains unsupported fields.")
        display_name = body.get("display_name", "")
        if not isinstance(display_name, str):
            raise PublicPlayError("Display name must be a string.")
        display_name = display_name.strip()
        try:
            display_name.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise PublicPlayError("Display name is invalid.") from exc
        if (
            len(display_name) > MAX_DISPLAY_NAME
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in display_name
            )
        ):
            raise PublicPlayError("Display name is invalid.")
        publish_name = body.get("publish_name", False)
        if not isinstance(publish_name, bool):
            raise PublicPlayError("publish_name must be a boolean.")
        if publish_name and not display_name:
            raise PublicPlayError("A display name is required before publishing it.")
        color = body.get("color")
        if color not in ("white", "black", "random"):
            raise PublicPlayError("Color selection is invalid.")
        preset_id = body.get("preset_id")
        if preset_id not in _PRESET_BY_ID:
            raise PublicPlayError("Engine strength is invalid.")
        clock_id = body.get("clock_id")
        if clock_id not in _CLOCK_BY_ID:
            raise PublicPlayError("Clock selection is invalid.")
        result = {
            "display_name": display_name,
            "publish_name": publish_name,
            "color": color,
            "preset_id": preset_id,
            "clock_id": clock_id,
        }
        if "advanced" in body:
            result["advanced"] = self._validate_advanced_settings(
                body["advanced"]
            )
        return result

    def _validate_advanced_settings(self, value: Any) -> dict[str, Any]:
        required = {
            "model_id",
            "nodes",
            "temperature",
            "temp_decay_moves",
            "clock",
        }
        allowed = required | {"start_fen"}
        if (
            not isinstance(value, Mapping)
            or not required.issubset(value)
            or set(value) - allowed
        ):
            raise PublicPlayError(
                "Advanced settings must contain model, visits, temperature, "
                "temperature decay, and clock."
            )
        model_id = value["model_id"]
        if (
            not isinstance(model_id, str)
            or model_id not in self._selectable_models_by_id
        ):
            raise PublicPlayError("Advanced model selection is invalid.")
        nodes = value["nodes"]
        if (
            isinstance(nodes, bool)
            or not isinstance(nodes, int)
            or not 1 <= nodes <= MAX_PLAY_NODES
        ):
            raise PublicPlayError(
                f"Visits must be an integer from 1 to {MAX_PLAY_NODES}."
            )
        temperature = value["temperature"]
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not math.isfinite(float(temperature))
            or not 0 <= float(temperature) <= 10
        ):
            raise PublicPlayError(
                "Temperature must be a number from 0 to 10."
            )
        temp_decay_moves = value["temp_decay_moves"]
        if (
            isinstance(temp_decay_moves, bool)
            or not isinstance(temp_decay_moves, int)
            or not 0 <= temp_decay_moves <= MAX_TEMP_DECAY_MOVES
        ):
            raise PublicPlayError(
                "Temperature decay moves must be an integer from 0 to "
                f"{MAX_TEMP_DECAY_MOVES}."
            )
        clock = value["clock"]
        if not isinstance(clock, Mapping):
            raise PublicPlayError("Advanced clock selection is invalid.")
        if clock.get("mode") == "unlimited":
            if set(clock) != {"mode"}:
                raise PublicPlayError(
                    "Unlimited clock must not contain timed settings."
                )
            normalized_clock = {"mode": "unlimited"}
        elif clock.get("mode") == "timed":
            if set(clock) != {"mode", "initial_ms", "increment_ms"}:
                raise PublicPlayError(
                    "Timed clock must contain initial_ms and increment_ms."
                )
            initial_ms = clock["initial_ms"]
            increment_ms = clock["increment_ms"]
            if (
                isinstance(initial_ms, bool)
                or not isinstance(initial_ms, int)
                or not 1_000 <= initial_ms <= MAX_CLOCK_INITIAL_MS
            ):
                raise PublicPlayError(
                    "Initial clock time must be between one second and "
                    "24 hours."
                )
            if (
                isinstance(increment_ms, bool)
                or not isinstance(increment_ms, int)
                or not 0 <= increment_ms <= MAX_CLOCK_INCREMENT_MS
            ):
                raise PublicPlayError(
                    "Clock increment must be between zero and one hour."
                )
            normalized_clock = {
                "mode": "timed",
                "initial_ms": initial_ms,
                "increment_ms": increment_ms,
            }
        else:
            raise PublicPlayError(
                "Advanced clock mode must be unlimited or timed."
            )
        start_fen = value.get("start_fen", START_FEN)
        if start_fen == "startpos":
            start_fen = START_FEN
        if (
            not isinstance(start_fen, str)
            or not start_fen.strip()
            or len(start_fen) > 256
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in start_fen
            )
        ):
            raise PublicPlayError("Start FEN is invalid.")
        start_fen = start_fen.strip()
        fields = start_fen.split()
        if len(fields) not in (6, 7):
            raise PublicPlayError(
                "Start FEN must have six fields and may include the "
                "Double Move ability field."
            )
        if len(fields) == 7 and not re.fullmatch(
            r"[WBwbmM-]+", fields[6]
        ):
            raise PublicPlayError(
                "Double Move ability field may contain only W, B, m, and -."
            )
        return {
            "model_id": model_id,
            "nodes": nodes,
            "temperature": float(temperature),
            "temp_decay_moves": temp_decay_moves,
            "clock": normalized_clock,
            "start_fen": start_fen,
        }

    @staticmethod
    def _validate_move_body(body: Any) -> dict[str, Any]:
        if not isinstance(body, Mapping) or set(body) != {
            "move",
            "expected_revision",
        }:
            raise PublicPlayError(
                "Move request must contain move and expected_revision."
            )
        move = body["move"]
        if not isinstance(move, str) or not _MOVE_RE.fullmatch(move):
            raise PublicPlayError("Move must be a valid UCI move.")
        revision = PublicPlayService._validate_revision(
            body["expected_revision"]
        )
        return {"move": move, "expected_revision": revision}

    @staticmethod
    def _validate_revision_body(body: Any) -> dict[str, int]:
        if not isinstance(body, Mapping) or set(body) != {"expected_revision"}:
            raise PublicPlayError(
                "Request must contain expected_revision."
            )
        return {
            "expected_revision": PublicPlayService._validate_revision(
                body["expected_revision"]
            )
        }

    @staticmethod
    def _validate_revision(value: Any) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 9_007_199_254_740_991
        ):
            raise PublicPlayError(
                "expected_revision must be a non-negative safe integer."
            )
        return value

    @staticmethod
    def _validate_idempotency_key(value: Optional[str]) -> str:
        if not isinstance(value, str) or not _IDEMPOTENCY_RE.fullmatch(value):
            raise PublicPlayError(
                "Idempotency-Key must contain 8-128 URL-safe characters."
            )
        return value

    @staticmethod
    def _validate_game_id(value: str) -> str:
        if not isinstance(value, str) or not _GAME_ID_RE.fullmatch(value):
            raise PublicPlayError("Game does not exist.", 404, "not_found")
        return value

    @staticmethod
    def _incomplete_human_turn(
        game: Mapping[str, Any]
    ) -> Optional[Mapping[str, Any]]:
        turns = game.get("turns")
        if not isinstance(turns, list):
            return None
        matches = [
            turn
            for turn in turns
            if turn.get("active")
            and not turn.get("completed")
            and turn.get("actor") == "human"
        ]
        if len(matches) > 1:
            raise RuntimeError("storage contains multiple unfinished human turns")
        return matches[0] if matches else None

    @staticmethod
    def _engine_context_id(public_id: str) -> str:
        # The colon is deliberately outside ENGINE_CONTEXT_RE in server.py.
        # A browser-controlled analysis context can therefore never name a
        # public game's retained search tree.
        return "public:" + public_id

    def _random_color(self, token: str, idempotency_key: str) -> str:
        """Choose a private but retry-stable color for an idempotent create."""

        digest = hmac.new(
            self._csrf_secret,
            (
                "double-move-random-color-v1\0"
                + token
                + "\0"
                + idempotency_key
            ).encode("ascii"),
            hashlib.sha256,
        ).digest()
        return ("white", "black")[digest[0] & 1]

    def _game_engine_config(
        self, game: Mapping[str, Any]
    ) -> tuple[str, str]:
        model = game.get("model")
        model_id = model.get("id") if isinstance(model, Mapping) else None
        model_sha256 = (
            model.get("sha256") if isinstance(model, Mapping) else None
        )
        configured = (
            self._models_by_id.get(model_id)
            if isinstance(model_id, str)
            else None
        )
        if configured is None or configured.sha256 != model_sha256:
            configured = next(
                (
                    candidate
                    for candidate in self._models_by_id.values()
                    if candidate.sha256 == model_sha256
                ),
                None,
            )
        backend = game.get("backend")
        allowed_backends = (
            configured.compatibility_backends
            if configured is not None
            and configured.id in self._compatibility_model_ids
            else (self.backend,)
        )
        if (
            not isinstance(model, Mapping)
            or configured is None
            or not isinstance(backend, str)
            or backend not in allowed_backends
        ):
            raise PublicPlayError(
                "This game's original engine configuration is unavailable.",
                503,
                "game_engine_unavailable",
            )
        return configured.id, backend

    @staticmethod
    def _float_option(value: float) -> str:
        if not math.isfinite(value):
            raise RuntimeError("preset temperature is not finite")
        return format(value, ".12g")

    @staticmethod
    def _other_color(color: str) -> str:
        return "black" if color == "white" else "white"

    @staticmethod
    def _color_code(color: str) -> str:
        return "w" if color == "white" else "b"

    @staticmethod
    def _color_name(code: str) -> str:
        return "white" if code == "w" else "black"

    @staticmethod
    def _color_from_fen(fen: str) -> str:
        fields = str(fen).split()
        return "black" if len(fields) > 1 and fields[1] == "b" else "white"

    @staticmethod
    def _map_error(
        exc: Exception, *, hide_ownership: bool = False
    ) -> PublicPlayError:
        if isinstance(exc, PublicPlayError):
            return exc
        if isinstance(exc, RevisionConflict):
            return PublicPlayError(
                "Game state changed; reload and try again.",
                409,
                "stale_revision",
            )
        if isinstance(exc, IdempotencyConflict):
            return PublicPlayError(
                "Idempotency-Key was already used for another request.",
                409,
                "idempotency_conflict",
            )
        if isinstance(exc, (NotFoundError, OwnershipError)):
            if hide_ownership or isinstance(exc, OwnershipError):
                return PublicPlayError(
                    "Game does not exist.", 404, "not_found"
                )
            return PublicPlayError(str(exc), 404, "not_found")
        if isinstance(exc, InvalidTransition):
            return PublicPlayError(str(exc), 409, "invalid_transition")
        if isinstance(exc, ValidationError):
            return PublicPlayError(str(exc), 400, "invalid_request")
        if isinstance(exc, (TimeoutError, RuntimeError)):
            return PublicPlayError(
                "Engine request failed; try again shortly.",
                503,
                "engine_unavailable",
            )
        if isinstance(exc, GameStoreError):
            return PublicPlayError(
                "Game storage request failed.", 500, "storage_error"
            )
        return PublicPlayError(
            "Game request failed.", 500, "internal_error"
        )


@contextmanager
def direct_engine_session(
    engine: EngineLike,
    _context_id: Optional[str] = None,
    _model_id: Optional[str] = None,
    _backend: Optional[str] = None,
) -> Any:
    """Adapter for tests and embeddings that intentionally share one engine."""

    yield engine


__all__ = [
    "CLOCKS",
    "PRESETS",
    "SESSION_COOKIE",
    "START_FEN",
    "ClockChoice",
    "ModelChoice",
    "Preset",
    "PublicPlayError",
    "PublicPlayService",
    "SessionConfiguration",
    "TokenBucketLimiter",
    "direct_engine_session",
]
