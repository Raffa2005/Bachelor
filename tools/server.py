#!/usr/bin/env python3
"""HTTP server for the double-move board visualizer.

The default profile uses repository-local paths and the CPU BLAS backend.
Use the restricted hosted profile behind a reverse proxy for public access.

Usage:
  python3 tools/server.py
  python3 tools/server.py --profile local
  python3 tools/server.py --profile hosted --port 8082
"""

import argparse
from contextlib import contextmanager
import glob
import hashlib
import json
import math
import os
import re
import secrets
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, quote, urlsplit

try:
    from .access_control import (
        AccessStore,
        AccessStoreError,
        InvitationLimitError,
        InvalidInvitationError,
        InvalidTransferError,
        MODE_INVITE_ONLY,
        MODE_OPEN,
        default_access_database_path,
    )
    from .dm_cli import DEFAULT_LC0, Lc0, Lc0PositionError
    from .game_store import GameStore, GameStoreError
    from .play_service import (
        ModelChoice,
        PublicPlayError,
        PublicPlayService,
        TokenBucketLimiter,
    )
except ImportError:
    from access_control import (
        AccessStore,
        AccessStoreError,
        InvitationLimitError,
        InvalidInvitationError,
        InvalidTransferError,
        MODE_INVITE_ONLY,
        MODE_OPEN,
        default_access_database_path,
    )
    from dm_cli import DEFAULT_LC0, Lc0, Lc0PositionError
    from game_store import GameStore, GameStoreError
    from play_service import (
        ModelChoice,
        PublicPlayError,
        PublicPlayService,
        TokenBucketLimiter,
    )

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOLS_DIR)
WEB_DIR = os.path.join(REPO_ROOT, "web")
MANIFEST_PATH = os.path.join(REPO_ROOT, "runtime", "manifest.json")
MODELS_DIR = os.path.join(REPO_ROOT, "models")
UCI_TOOL = os.path.join(TOOLS_DIR, "uci2lichess.py")
DEFAULT_RUNS_ROOT = os.path.join(REPO_ROOT, "runs")
DEFAULT_PORT = 8082
LOCAL_MODEL_ID = "v43-latest"
LOCAL_LC0 = os.path.join(
    REPO_ROOT, "build", "lc0", "blas", "release", "lc0"
)
LOCAL_RUNS_ROOT = os.path.join(REPO_ROOT, "runs")
LOCAL_WEIGHTS = os.path.join(REPO_ROOT, "models", "v43-latest.pb.gz")
DEFAULT_DATABASE_PATH = os.path.join(
    REPO_ROOT, ".runtime", "public-play", "games.sqlite3"
)
ENGINE = None
ENGINE_KEY = None
ENGINE_CONTEXT_ID = None
ENGINE_LOCK = threading.Lock()
PLAY_SERVICE = None
ACCESS_STORE = None
HOSTED_MAX_BODY = 64 * 1024
DEFAULT_MAX_BODY = 1024 * 1024
HOSTED_MAX_MOVES = 2048
HOSTED_MAX_SEARCH_NODES = 1_000_000
MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?\+?$")
HOST_RE = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")
BACKEND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
ENGINE_CONTEXT_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
ANALYSIS_CONTEXT_PREFIX = "analysis:"
MAX_POSITION_REVISION = 9_007_199_254_740_991
# lc0 parses ``go nodes`` with std::stoi into a signed 32-bit int.
MAX_SEARCH_NODES = 2_147_483_647
MAX_PUBLIC_PLAY_MODELS = 8
ANALYSIS_MULTIPV = 8
ENGINE_LOCK_WAIT_SECONDS = 5
ENGINE_STREAM_WRITE_TIMEOUT_SECONDS = 10
MAX_RECENT_ANALYSIS_CONTEXTS = 1024
ACCESS_REDEEM_MAX_BODY = 2048
GAME_MAINTENANCE_INTERVAL_SECONDS = 2 * 24 * 60 * 60


def _protected_page_path(path):
    return bool(
        path
        in (
            "/analysis",
            "/analysis/",
            "/double_move.html",
            "/doublemove_board.html",
            "/index.html",
            "/play",
            "/play/",
            "/play.html",
            "/games",
            "/games/",
            "/games.html",
        )
        or re.fullmatch(r"/play/[0-9a-f]{32}", path)
        or re.fullmatch(r"/games/[0-9a-f]{32}", path)
    )


def _site_page_path(path):
    return bool(
        path
        in (
            "/",
            "/landing.html",
            "/invite",
            "/invite/",
            "/invite.html",
            "/rules",
            "/rules/",
            "/rules.html",
        )
        or _protected_page_path(path)
    )


def _protected_api_path(path):
    return bool(
        path == "/api/models"
        or path in (
            "/api/training/games",
            "/api/latest-game",
            "/api/runs",
        )
        or path.startswith("/api/engine/")
        or path.startswith("/api/play/")
        or path == "/api/games"
        or path.startswith("/api/games/")
    )


def _parse_uci_analysis_lines(info_lines, root_turn):
    """Convert one lc0 MultiPV update into a compact browser payload."""

    parsed = {}
    for raw_line in info_lines or ():
        line = str(raw_line)
        if not line.startswith("info ") or line.startswith("info string"):
            continue
        pv_match = re.search(r"(?:^|\s)pv\s+(.+)$", line)
        if pv_match is None:
            continue
        pv = [
            token
            for token in pv_match.group(1).split()
            if MOVE_RE.fullmatch(token)
        ]
        if not pv:
            continue
        multipv_match = re.search(
            r"(?:^|\s)multipv\s+(\d+)(?=\s|$)", line
        )
        multipv = int(multipv_match.group(1)) if multipv_match else 1
        score_match = re.search(
            r"(?:^|\s)score\s+(cp|mate)\s+(-?\d+)(?=\s|$)", line
        )
        score_cp = None
        score_mate = None
        if score_match:
            value = int(score_match.group(2))
            if score_match.group(1) == "cp":
                score_cp = value
            else:
                score_mate = value
        if root_turn == "b":
            if score_cp is not None:
                score_cp = -score_cp
            if score_mate is not None:
                score_mate = -score_mate

        def integer_field(name):
            match = re.search(
                rf"(?:^|\s){name}\s+(\d+)(?=\s|$)", line
            )
            return int(match.group(1)) if match else None

        parsed[multipv] = {
            "multipv": multipv,
            "white_cp": score_cp,
            "white_mate": score_mate,
            "depth": integer_field("depth"),
            "seldepth": integer_field("seldepth"),
            "nodes": integer_field("nodes"),
            "nps": integer_field("nps"),
            "pv": pv,
        }
    return [parsed[key] for key in sorted(parsed)]


def _engine_process_dead(engine):
    """Return whether a previously launched lc0 child has exited."""

    if engine is None:
        return False
    process = getattr(engine, "proc", None)
    if process is None:
        # Test doubles and alternate in-process engines have no child process.
        return False
    try:
        return process.poll() is not None
    except Exception:
        return True


def _discard_dead_engine():
    """Forget a dead shared process so the next request launches a new one."""

    global ENGINE, ENGINE_KEY, ENGINE_CONTEXT_ID
    if not _engine_process_dead(ENGINE):
        return False
    ENGINE = None
    ENGINE_KEY = None
    ENGINE_CONTEXT_ID = None
    return True


def _quarantine_analysis_engine(engine, context_id):
    """Force the next request to reset an engine after an internal failure."""

    global ENGINE_CONTEXT_ID
    if engine is None or not context_id:
        return False
    expected_context = ANALYSIS_CONTEXT_PREFIX + context_id
    with ENGINE_LOCK:
        if ENGINE is not engine:
            return False
        if _discard_dead_engine():
            return True
        if ENGINE_CONTEXT_ID != expected_context:
            return False
        ENGINE_CONTEXT_ID = None
        return True


@dataclass(frozen=True)
class RuntimeConfig:
    profile: str
    host: str
    port: int
    runs_root: str
    lc0_path: str
    weights_path: Optional[str]
    backend: str
    available_backends: tuple[str, ...]
    nodes: int
    minibatch_size: int
    use_modules: bool
    restricted: bool


@dataclass(frozen=True)
class _FallbackWeights:
    path: str
    sha256: str
    identity: tuple[int, ...]


@dataclass(frozen=True)
class _PublicPlayModelRuntime:
    choice: ModelChoice
    path: str
    size: int
    mtime_ns: int
    fallback: Optional[_FallbackWeights] = None
    identity: Optional[tuple[int, ...]] = None


class RequestError(Exception):
    """A safe error caused by an invalid or currently unserviceable request."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class _AnalysisRequestTicket:
    context_id: str
    request_id: Optional[int]
    token: object
    cancel_event: threading.Event
    accepted: bool


class _AnalysisRequestCoordinator:
    """Promptly supersede stale streams within one browser analysis context."""

    def __init__(self, maximum_contexts=MAX_RECENT_ANALYSIS_CONTEXTS):
        self.maximum_contexts = maximum_contexts
        self._lock = threading.Lock()
        self._active = {}
        self._latest_request_ids = {}

    def begin(self, context_id, request_id=None):
        cancel_event = threading.Event()
        ticket = _AnalysisRequestTicket(
            context_id=context_id,
            request_id=request_id,
            token=object(),
            cancel_event=cancel_event,
            accepted=True,
        )
        with self._lock:
            latest = self._latest_request_ids.get(context_id)
            if (
                request_id is not None
                and latest is not None
                and request_id <= latest
            ):
                cancel_event.set()
                return _AnalysisRequestTicket(
                    context_id=context_id,
                    request_id=request_id,
                    token=ticket.token,
                    cancel_event=cancel_event,
                    accepted=False,
                )
            if request_id is not None:
                self._latest_request_ids.pop(context_id, None)
                self._latest_request_ids[context_id] = request_id

            previous = self._active.get(context_id)
            if previous is not None:
                previous.cancel_event.set()
            self._active[context_id] = ticket
            self._prune_locked()
        return ticket

    def finish(self, ticket):
        with self._lock:
            current = self._active.get(ticket.context_id)
            if current is not None and current.token is ticket.token:
                self._active.pop(ticket.context_id, None)

    def cancel(self, context_id, request_id):
        with self._lock:
            latest = self._latest_request_ids.get(context_id)
            if latest is None or request_id > latest:
                self._latest_request_ids.pop(context_id, None)
                self._latest_request_ids[context_id] = request_id
            current = self._active.get(context_id)
            cancelled = (
                current is not None and current.request_id == request_id
            )
            if cancelled:
                current.cancel_event.set()
            self._prune_locked()
            return cancelled

    def _prune_locked(self):
        while len(self._latest_request_ids) > self.maximum_contexts:
            removable = next(
                (
                    context_id
                    for context_id in self._latest_request_ids
                    if context_id not in self._active
                ),
                None,
            )
            if removable is None:
                return
            self._latest_request_ids.pop(removable, None)


ANALYSIS_REQUESTS = _AnalysisRequestCoordinator()


class _PublicPlayEngineSessions:
    """Share the server's one lc0 process with a stable per-game context."""

    def __init__(self, config, models):
        self.config = config
        self.models = {model.choice.id: model for model in models}

    @contextmanager
    def __call__(self, context_id, model_id, backend):
        global ENGINE, ENGINE_KEY, ENGINE_CONTEXT_ID
        if not ENGINE_LOCK.acquire(timeout=5):
            raise RuntimeError("engine is busy")
        try:
            _discard_dead_engine()
            model = self.models.get(model_id)
            if model is None:
                raise RuntimeError("public-play model is unavailable")
            allowed_backends = (self.config.backend,) + tuple(
                model.choice.compatibility_backends
            )
            if backend not in allowed_backends:
                raise RuntimeError("public-play backend is unavailable")
            try:
                _check_model_files(model)
            except OSError as exc:
                raise RuntimeError(
                    "public-play model is unavailable"
                ) from exc
            options = {
                "HistoryFill": "repeat_current",
                "CPuct": "1.2",
                "PolicyTemperature": "1.0",
                "DisableAbilityEdges": "false",
                "SmartPruningFactor": "0",
            }
            if model.fallback is not None:
                options["FallbackWeights"] = model.fallback.path
            key = (
                self.config.lc0_path,
                model.path,
                backend,
                self.config.minibatch_size,
                self.config.use_modules,
                tuple(sorted(options.items())),
            )
            launched = False
            if ENGINE is None or ENGINE_KEY != key:
                if ENGINE is not None:
                    try:
                        ENGINE.quit()
                    except Exception:
                        pass
                ENGINE = Lc0(
                    self.config.lc0_path,
                    model.path,
                    backend=backend,
                    minibatch_size=self.config.minibatch_size,
                    opts=options,
                    launch_with_modules=self.config.use_modules,
                    fallback_weights=options.get("FallbackWeights"),
                )
                ENGINE_KEY = key
                ENGINE_CONTEXT_ID = None
                launched = True
            if not launched and ENGINE_CONTEXT_ID != context_id:
                ENGINE.new_game()
            ENGINE_CONTEXT_ID = context_id
            yield ENGINE
        finally:
            ENGINE_LOCK.release()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _file_identity(metadata):
    return (metadata.st_dev, metadata.st_ino, metadata.st_size,
            metadata.st_mtime_ns, metadata.st_ctime_ns)


def _check_model_files(model):
    metadata = os.stat(model.path)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size != model.size
            or metadata.st_mtime_ns != model.mtime_ns):
        raise OSError("model changed after server startup")
    if model.identity is not None and _file_identity(metadata) != model.identity:
        raise OSError("model changed after server startup")
    if model.fallback is not None:
        metadata = os.stat(model.fallback.path)
        if (not stat.S_ISREG(metadata.st_mode)
                or _file_identity(metadata) != model.fallback.identity):
            raise OSError("fallback changed after server startup")


def _manifest_model_specs():
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as handle:
            models = json.load(handle).get("models", {})
    except (OSError, ValueError, TypeError):
        return ()
    if not isinstance(models, dict):
        return ()
    return tuple(spec for spec in models.values() if isinstance(spec, dict))


def _required_fallback(path, sha256=None):
    """Bind a known expert to the trusted manifest pair, including renamed files."""
    specifications = _manifest_model_specs()
    normalized = os.path.abspath(path)
    specification = next((spec for spec in specifications
                          if isinstance(spec.get("installed_filename"), str)
                          and normalized == os.path.abspath(os.path.join(
                              MODELS_DIR, spec["installed_filename"]))), None)
    if sha256 is None and os.path.isfile(path):
        sha256 = _sha256_file(path)
    if specification is None and sha256 is not None:
        specification = next((spec for spec in specifications
                              if spec.get("sha256") == sha256), None)
    if specification is None:
        return None
    if specification.get("sha256") is not None and sha256 != specification["sha256"]:
        raise OSError("model does not match its manifest declaration")
    fallback = specification.get("fallback_weights")
    if fallback is None:
        return None
    if not isinstance(fallback, dict):
        raise OSError("invalid fallback declaration")
    filename = fallback.get("installed_filename")
    if (not isinstance(filename, str) or not filename
            or filename != os.path.basename(filename) or filename in (".", "..")
            or type(fallback.get("size_bytes")) is not int
            or fallback["size_bytes"] < 1
            or not isinstance(fallback.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", fallback["sha256"])):
        raise OSError("invalid fallback declaration")
    fallback_path = os.path.abspath(os.path.join(MODELS_DIR, filename))
    before = os.stat(fallback_path)
    if (not stat.S_ISREG(before.st_mode) or before.st_size != fallback["size_bytes"]
            or _sha256_file(fallback_path) != fallback["sha256"]):
        raise OSError("fallback does not match its manifest declaration")
    if _file_identity(before) != _file_identity(os.stat(fallback_path)):
        raise OSError("fallback changed while being verified")
    return _FallbackWeights(fallback_path, fallback["sha256"], _file_identity(before))


def _resolve_public_weights(config):
    configured = config.weights_path
    if configured and os.path.isfile(configured):
        return os.path.abspath(configured)
    pattern = os.path.join(
        config.runs_root, "*", "exported_models", "net-*.pb.gz"
    )
    candidates = [
        path for path in glob.glob(pattern) if os.path.isfile(path)
    ]
    return (
        os.path.abspath(max(candidates, key=os.path.getmtime))
        if candidates
        else None
    )


def _model_stem(path):
    name = os.path.basename(path)
    for suffix in (".pb.gz", ".pb", ".gz"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return os.path.splitext(name)[0]


def _public_model_id(path, default_path):
    if path == default_path:
        # Public play historically persisted the selected basename. Preserve
        # that identifier so active games survive a server upgrade.
        candidate = os.path.basename(path)
    else:
        run = os.path.basename(
            os.path.dirname(os.path.dirname(path))
        )
        candidate = f"{run}:{_model_stem(path)}"
    candidate = re.sub(r"[^A-Za-z0-9_.:+@/-]", "-", candidate)
    candidate = candidate[:160].rstrip("-")
    if not candidate or not candidate[0].isalnum():
        candidate = "model-" + hashlib.sha256(
            path.encode("utf-8")
        ).hexdigest()[:16]
    return candidate


def _manifest_selectable_models(default_path):
    """Return optional, verified-by-declaration model specifications.

    A manifest catalog is only trusted when a declared model resolves to
    the configured default model. This prevents an unrelated checkout or a
    test runtime from silently inheriting this repository's hosted catalog.
    """

    try:
        with open(MANIFEST_PATH, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError, TypeError):
        return ()
    declared = manifest.get("models")
    default_model = manifest.get("default_model")
    if not isinstance(declared, dict) or not isinstance(default_model, str):
        return ()
    default_specification = next((spec for spec in declared.values()
                                 if isinstance(spec, dict)
                                 and isinstance(spec.get("installed_filename"), str)
                                 and os.path.abspath(os.path.join(MODELS_DIR, spec["installed_filename"]))
                                 == os.path.abspath(default_path)), None)
    if not isinstance(default_specification, dict):
        return ()
    default_filename = default_specification.get("installed_filename")
    if (
        not isinstance(default_filename, str)
        or default_filename != os.path.basename(default_filename)
        or os.path.abspath(os.path.join(MODELS_DIR, default_filename))
        != os.path.abspath(default_path)
    ):
        return ()

    selectable = []
    for model_id, specification in declared.items():
        if (
            specification is default_specification
            or not isinstance(model_id, str)
            or not isinstance(specification, dict)
            or specification.get("selectable") is not True
            or specification.get("resume_only") is True
        ):
            continue
        filename = specification.get("installed_filename")
        expected_size = specification.get("size_bytes")
        expected_hash = specification.get("sha256")
        if (
            not isinstance(filename, str)
            or filename != os.path.basename(filename)
            or not isinstance(expected_size, int)
            or expected_size < 1
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash or "")
        ):
            continue
        raw_label = specification.get("display_name", model_id)
        if not isinstance(raw_label, str):
            continue
        label = re.sub(r"[\x00-\x1f\x7f]+", " ", raw_label)
        label = " ".join(label.split())[:160].strip()
        if not label:
            continue
        selectable.append(
            (
                os.path.abspath(os.path.join(MODELS_DIR, filename)),
                _public_model_id(filename, filename),
                label,
                expected_size,
                expected_hash,
            )
        )
        if len(selectable) >= MAX_PUBLIC_PLAY_MODELS - 1:
            break
    return tuple(selectable)


def _resolve_public_models(config):
    default_path = _resolve_public_weights(config)
    if default_path is None:
        return (), None
    candidates = [(default_path, False, None, None, None, None)]
    candidates.extend(
        (path, True, model_id, label, expected_size, expected_hash)
        for path, model_id, label, expected_size, expected_hash
        in _manifest_selectable_models(default_path)
    )
    if not config.restricted:
        pattern = os.path.join(
            config.runs_root, "*", "exported_models", "net-*.pb.gz"
        )
        discovered = []
        for path in glob.glob(pattern):
            normalized = os.path.abspath(path)
            try:
                metadata = os.stat(normalized)
            except OSError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                discovered.append((metadata.st_mtime_ns, normalized))
        discovered.sort(reverse=True)
        remaining = max(0, MAX_PUBLIC_PLAY_MODELS - len(candidates))
        candidates.extend(
            (path, True, None, None, None, None)
            for _mtime_ns, path in discovered[:remaining]
        )

    models = []
    seen_paths = set()
    seen_hashes = set()
    seen_ids = set()
    for (
        path,
        optional,
        declared_id,
        declared_label,
        expected_size,
        expected_hash,
    ) in candidates:
        normalized = os.path.abspath(path)
        if normalized in seen_paths:
            continue
        seen_paths.add(normalized)
        try:
            before = os.stat(normalized)
            if not stat.S_ISREG(before.st_mode):
                raise OSError("model path is not a regular file")
            sha256 = _sha256_file(normalized)
            metadata = os.stat(normalized)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            )
            identity_after = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
            if identity_before != identity_after:
                raise OSError("model changed while its digest was computed")
            if (
                expected_size is not None
                and (
                    metadata.st_size != expected_size
                    or sha256 != expected_hash
                )
            ):
                raise OSError("model does not match its manifest declaration")
            fallback = _required_fallback(normalized, sha256)
        except OSError:
            if optional:
                continue
            raise
        if sha256 in seen_hashes:
            continue
        seen_hashes.add(sha256)
        model_id = declared_id or _public_model_id(normalized, default_path)
        if model_id in seen_ids:
            suffix = "-" + sha256[:12]
            model_id = model_id[: 160 - len(suffix)] + suffix
        seen_ids.add(model_id)
        if declared_label is not None:
            label = declared_label
        elif normalized == default_path:
            label = _model_stem(normalized)
        else:
            run = os.path.basename(
                os.path.dirname(os.path.dirname(normalized))
            )
            label = f"{run} / {os.path.basename(normalized)}"
        label = re.sub(r"[\x00-\x1f\x7f]+", " ", label)
        label = " ".join(label.split())[:160].strip() or model_id[:160]
        models.append(
            _PublicPlayModelRuntime(
                choice=ModelChoice(
                    id=model_id,
                    label=label,
                    sha256=sha256,
                ),
                path=normalized,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
                fallback=fallback,
                identity=_file_identity(metadata),
            )
        )
    default_id = models[0].choice.id if models else None
    return tuple(models), default_id


def _resolve_compatibility_models(models):
    """Load verified hidden models needed only to resume existing games."""

    try:
        with open(MANIFEST_PATH, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, ValueError, TypeError):
        return ()

    declared = manifest.get("models")
    if not isinstance(declared, dict):
        return ()
    engine_manifest = manifest.get("engine")
    supported_values = (
        engine_manifest.get("supported_backends")
        if isinstance(engine_manifest, dict)
        else None
    )
    supported_backends = {
        candidate
        for candidate in supported_values or ()
        if isinstance(candidate, str) and BACKEND_RE.fullmatch(candidate)
    }
    seen_ids = {model.choice.id for model in models}
    seen_hashes = {model.choice.sha256 for model in models}
    compatibility = []
    for model_id, specification in declared.items():
        if (
            not isinstance(model_id, str)
            or not isinstance(specification, dict)
            or specification.get("resume_only") is not True
        ):
            continue
        filename = specification.get("installed_filename")
        expected_size = specification.get("size_bytes")
        expected_hash = specification.get("sha256")
        resume_values = specification.get("resume_backends")
        if (
            not isinstance(filename, str)
            or filename != os.path.basename(filename)
            or not isinstance(expected_size, int)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_hash or "")
            or not isinstance(resume_values, list)
            or not resume_values
            or any(
                not isinstance(candidate, str)
                or candidate not in supported_backends
                for candidate in resume_values
            )
        ):
            continue
        resume_backends = tuple(dict.fromkeys(resume_values))
        public_id = _public_model_id(filename, filename)
        path = os.path.join(MODELS_DIR, filename)
        try:
            metadata = os.stat(path)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size != expected_size
                or _sha256_file(path) != expected_hash
            ):
                continue
            fallback = _required_fallback(path, expected_hash)
        except OSError:
            continue
        if public_id in seen_ids or expected_hash in seen_hashes:
            continue
        seen_ids.add(public_id)
        seen_hashes.add(expected_hash)
        compatibility.append(
            _PublicPlayModelRuntime(
                choice=ModelChoice(
                    id=public_id,
                    label=f"{model_id} (existing games only)",
                    sha256=expected_hash,
                    compatibility_backends=resume_backends,
                ),
                path=path,
                size=metadata.st_size,
                mtime_ns=metadata.st_mtime_ns,
                fallback=fallback,
                identity=_file_identity(metadata),
            )
        )
    return tuple(compatibility)


def _load_or_create_csrf_secret(database_path, environ):
    configured = environ.get("DM_CSRF_SECRET")
    if configured:
        encoded = configured.encode("utf-8")
        if len(encoded) < 32:
            raise ValueError("DM_CSRF_SECRET must contain at least 32 bytes")
        return hashlib.sha256(encoded).digest()

    secret_path = database_path + ".csrf-secret"
    parent = os.path.dirname(secret_path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    with open(secret_path, "rb") as handle:
        secret = handle.read(64)
    if len(secret) < 32:
        raise ValueError("public-play CSRF secret file is invalid")
    return secret[:32]


def _initialize_public_play(config, environ=None):
    environ = os.environ if environ is None else environ
    models, default_model_id = _resolve_public_models(config)
    if not models or default_model_id is None:
        raise ValueError("public play requires configured model weights")
    compatibility_models = _resolve_compatibility_models(models)
    database_path = os.path.abspath(
        environ.get("DM_DATABASE_PATH") or DEFAULT_DATABASE_PATH
    )
    secret = _load_or_create_csrf_secret(database_path, environ)
    origins = tuple(
        value.strip().rstrip("/")
        for value in (environ.get("DM_PUBLIC_ORIGIN") or "").split(",")
        if value.strip()
    )
    secure_cookie = any(
        origin.lower().startswith("https://") for origin in origins
    )
    store = GameStore(database_path)
    service = PublicPlayService(
        store,
        _PublicPlayEngineSessions(config, models + compatibility_models),
        models=tuple(model.choice for model in models),
        compatibility_models=tuple(
            model.choice for model in compatibility_models
        ),
        default_model_id=default_model_id,
        backend=config.backend,
        csrf_secret=secret,
        secure_cookie=secure_cookie,
    )
    return service, origins, database_path, models


def _initialize_access_control(database_path, origins, environ=None):
    environ = os.environ if environ is None else environ
    access_database_path = default_access_database_path(environ)
    if not environ.get("DM_ACCESS_DATABASE_PATH"):
        access_database_path = os.path.join(
            os.path.dirname(os.path.abspath(database_path)),
            "access.sqlite3",
        )
    configured_origins = tuple(origins) or tuple(
        value.strip().rstrip("/")
        for value in (environ.get("DM_PUBLIC_ORIGIN") or "").split(",")
        if value.strip()
    )
    secure_cookie = any(
        origin.lower().startswith("https://") for origin in configured_origins
    )
    return (
        AccessStore(
            access_database_path,
            secure_cookie=secure_cookie,
        ),
        access_database_path,
    )


def _detect_profile(environ, path_exists=os.path.exists):
    """Keep automatic startup local, independent of the host environment."""
    return "local"


def _profile_defaults(profile, environ):
    if profile == "local":
        return RuntimeConfig(
            profile="local",
            host="127.0.0.1",
            port=DEFAULT_PORT,
            runs_root=LOCAL_RUNS_ROOT,
            lc0_path=LOCAL_LC0,
            weights_path=LOCAL_WEIGHTS,
            backend="blas",
            available_backends=("blas",),
            nodes=800,
            minibatch_size=32,
            use_modules=False,
            restricted=False,
        )
    if profile == "hosted":
        return RuntimeConfig(
            profile="hosted",
            host="127.0.0.1",
            port=DEFAULT_PORT,
            runs_root=LOCAL_RUNS_ROOT,
            lc0_path=LOCAL_LC0,
            weights_path=LOCAL_WEIGHTS,
            backend="blas",
            available_backends=("blas",),
            nodes=800,
            minibatch_size=32,
            use_modules=False,
            restricted=True,
        )
    raise ValueError(f"Unknown runtime profile: {profile}")


def _parse_bool(value, name):
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true/false, yes/no, on/off, or 1/0")


def _first_override(cli_value, environ, env_name, default, converter=None):
    if cli_value is not None:
        return cli_value
    env_value = environ.get(env_name)
    if env_value is None or env_value == "":
        return default
    if converter is None:
        return env_value
    try:
        return converter(env_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid {env_name}: {env_value!r}") from exc


def _parse_available_backends(value, name="available backends"):
    if isinstance(value, (tuple, list)):
        raw_choices = value
    else:
        raw_choices = str(value).split(",")
    choices = []
    for raw_choice in raw_choices:
        choice = str(raw_choice).strip()
        if not choice or not BACKEND_RE.fullmatch(choice):
            raise ValueError(
                f"{name} must be a comma-separated list of backend names"
            )
        if choice not in choices:
            choices.append(choice)
    if not choices:
        raise ValueError(f"{name} must include at least one backend")
    return tuple(choices)


def _resolve_runtime_config(args, environ=None, path_exists=os.path.exists):
    environ = os.environ if environ is None else environ
    requested_profile = args.profile or environ.get("DM_PROFILE") or "auto"
    if requested_profile not in ("auto", "local", "hosted"):
        raise ValueError("DM_PROFILE must be auto, local, or hosted")
    profile = (
        _detect_profile(environ, path_exists=path_exists)
        if requested_profile == "auto" else requested_profile
    )
    defaults = _profile_defaults(profile, environ)

    use_modules = args.use_modules
    if use_modules is None:
        raw_use_modules = environ.get("DM_USE_MODULES")
        use_modules = (
            defaults.use_modules
            if raw_use_modules in (None, "")
            else _parse_bool(raw_use_modules, "DM_USE_MODULES")
        )

    backend = _first_override(
        args.backend, environ, "DM_BACKEND", defaults.backend
    )
    if not isinstance(backend, str) or not BACKEND_RE.fullmatch(backend):
        raise ValueError("Backend must be a valid lc0 backend name")
    raw_available_backends = (
        args.available_backends
        if args.available_backends is not None
        else environ.get("DM_AVAILABLE_BACKENDS")
    )
    if raw_available_backends in (None, ""):
        available_backends = defaults.available_backends
        if backend not in available_backends:
            available_backends = (backend, *available_backends)
    else:
        try:
            available_backends = _parse_available_backends(
                raw_available_backends,
                "DM_AVAILABLE_BACKENDS",
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if backend not in available_backends:
            raise ValueError(
                f"Configured backend {backend!r} is not in "
                "DM_AVAILABLE_BACKENDS"
            )

    config = RuntimeConfig(
        profile=profile,
        host=_first_override(args.host, environ, "DM_HOST", defaults.host),
        port=_first_override(args.port, environ, "DM_PORT", defaults.port, int),
        runs_root=_first_override(
            args.runs_root, environ, "DM_RUNS_ROOT", defaults.runs_root
        ),
        lc0_path=_first_override(
            args.lc0_path, environ, "DM_LC0_PATH", defaults.lc0_path
        ),
        weights_path=_first_override(
            args.weights_path, environ, "DM_WEIGHTS_PATH", defaults.weights_path
        ),
        backend=backend,
        available_backends=available_backends,
        nodes=_first_override(args.nodes, environ, "DM_NODES", defaults.nodes, int),
        minibatch_size=_first_override(
            args.minibatch_size,
            environ,
            "DM_MINIBATCH_SIZE",
            defaults.minibatch_size,
            int,
        ),
        use_modules=use_modules,
        restricted=defaults.restricted,
    )
    if not 1 <= config.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if not 1 <= config.nodes <= MAX_SEARCH_NODES:
        raise ValueError(
            f"Nodes must be between 1 and {MAX_SEARCH_NODES}"
        )
    if config.minibatch_size < 1:
        raise ValueError("Minibatch size must be at least 1")
    return config


def _build_arg_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile",
        choices=("auto", "local", "hosted"),
        default=None,
        help=(
            "Runtime defaults to use (default: local; "
            "hosted locks engine settings and run browsing)"
        ),
    )
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument(
        "--runs-root",
        default=None,
        help="Root directory containing all run subdirectories",
    )
    parser.add_argument("--lc0", dest="lc0_path", default=None)
    parser.add_argument("--weights", dest="weights_path", default=None)
    parser.add_argument("--backend", default=None)
    parser.add_argument(
        "--available-backends",
        default=None,
        help=(
            "Comma-separated backends supported by the configured lc0 binary "
            "(environment: DM_AVAILABLE_BACKENDS)"
        ),
    )
    parser.add_argument("--nodes", type=int, default=None)
    parser.add_argument("--minibatch-size", type=int, default=None)
    module_group = parser.add_mutually_exclusive_group()
    module_group.add_argument(
        "--use-modules", dest="use_modules", action="store_true"
    )
    module_group.add_argument(
        "--no-use-modules", dest="use_modules", action="store_false"
    )
    parser.set_defaults(use_modules=None)
    return parser


def _find_latest_run_dir(runs_root):
    """Find the most recently modified run directory that actually has
    completed self-play data.

    Slurm pre-creates the run's logs/ directory at submit time (because the
    sbatch --output path lives inside it). That means a queued-but-not-yet-
    started job has the freshest mtime but zero slurm-*/ subdirs. We need
    to skip such empty shells and return the most recent run with real data.
    """
    if not os.path.isdir(runs_root):
        return None
    candidates = []
    for name in os.listdir(runs_root):
        path = os.path.join(runs_root, name)
        if os.path.isdir(path) and os.path.isdir(os.path.join(path, "logs")):
            candidates.append(path)
    if not candidates:
        return None
    # Sort by modification time (most recent first)
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    # Walk in order; pick the first one that has at least one completed
    # selfplay log (i.e. one slurm-*/ subdir with a 'tournamentstatus final').
    for cand in candidates:
        if _find_latest_completed_selfplay_logs(cand):
            return cand
    # No run has completed data yet — fall back to the freshest dir so the
    # caller can still report a sensible "no data" message tied to a run.
    return candidates[0]


def _find_latest_completed_selfplay_logs(run_dir):
    """Return list of completed selfplay logs for the most-recent finished iter.

    Two log layouts are supported:
      - Single-proc (v3–v6): selfplay-iter{N}.stdout.log
      - Multi-proc  (v8+):   selfplay-iter{N}.p{X}.stdout.log  (one per proc)

    For multi-proc, all per-proc logs from the same iter are returned together.
    "Completed" = the file contains 'tournamentstatus final'. Returns [] if no
    completed iter found.
    """
    log_dir = os.path.join(run_dir, "logs")
    if not os.path.isdir(log_dir):
        return []
    dirs = sorted(
        [d for d in os.listdir(log_dir)
         if d.startswith("slurm-") and os.path.isdir(os.path.join(log_dir, d))],
        key=lambda x: os.path.getmtime(os.path.join(log_dir, x)), reverse=True,
    )
    if not dirs:
        return []
    latest = os.path.join(log_dir, dirs[0])

    # Group selfplay stdout logs by iter number. We deliberately only consider
    # *.stdout.log files (so .stderr.log etc. don't match) but fall back to
    # plain .log for the v3–v6 naming where stdout was written to <name>.log.
    iter_re = re.compile(r"selfplay-iter(\d+)(?:\.p(\d+))?(?:\.stdout)?\.log$")
    by_iter = {}  # iter_number -> [path, ...]
    for f in os.listdir(latest):
        m = iter_re.match(f)
        if not m:
            continue
        # Skip .stderr.log explicitly
        if f.endswith(".stderr.log"):
            continue
        iter_n = int(m.group(1))
        by_iter.setdefault(iter_n, []).append(os.path.join(latest, f))

    if not by_iter:
        return []

    # Walk iters from highest to lowest, return the first iter where every
    # log in the group contains 'tournamentstatus final'.
    for iter_n in sorted(by_iter.keys(), reverse=True):
        paths = sorted(by_iter[iter_n])
        try:
            all_complete = all(
                "tournamentstatus final" in open(p).read()
                for p in paths
            )
        except Exception:
            all_complete = False
        if all_complete:
            return paths
    return []


def _parse_game_line(line):
    """Parse a gameready line into a dict."""
    # Try adjudicated format first (has q and d values)
    m = re.search(
        r'gameid\s+(\d+).*?player1\s+(\w+)\s+result\s+(\w+)\s+q\s+([-\d.]+)\s+d\s+([-\d.]+)\s+moves\s+(.+)$',
        line,
    )
    if m:
        moves = m.group(6).strip()
        return {
            "game_id": int(m.group(1)),
            "player1": m.group(2),
            "result": m.group(3),
            "q": float(m.group(4)),
            "d": float(m.group(5)),
            "uci": moves,
            "move_count": len(moves.split()),
        }
    # Try non-adjudicated format (no q/d)
    m = re.search(
        r'gameid\s+(\d+).*?player1\s+(\w+)\s+result\s+(\w+)\s+moves\s+(.+)$',
        line,
    )
    if m:
        moves = m.group(4).strip()
        return {
            "game_id": int(m.group(1)),
            "player1": m.group(2),
            "result": m.group(3),
            "q": 0.0,
            "d": 0.0,
            "uci": moves,
            "move_count": len(moves.split()),
        }
    return None


class BoardHTTPServer(ThreadingHTTPServer):
    """Keep static requests responsive without retaining worker threads."""

    daemon_threads = True
    request_queue_size = 16


class BoardServer(SimpleHTTPRequestHandler):
    runs_root = DEFAULT_RUNS_ROOT
    runtime_config = _profile_defaults("local", {})
    play_service = None
    model_runtimes = ()
    access_store = None
    analysis_requests = ANALYSIS_REQUESTS
    public_origins = ()
    canonical_origin = ""
    legacy_origins = ()
    engine_rate_limiter = TokenBucketLimiter(
        capacity=30,
        refill_per_second=0.5,
        maximum_buckets=1,
    )
    archive_rate_limiter = TokenBucketLimiter(
        capacity=240,
        refill_per_second=4,
        maximum_buckets=1,
    )
    access_redeem_rate_limiter = TokenBucketLimiter(
        capacity=20,
        refill_per_second=0.25,
        maximum_buckets=1,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if self._redirect_legacy_page(path):
            return
        if (_protected_page_path(path) or _protected_api_path(path)) and not (
            self._require_site_access(page_request=_protected_page_path(path))
        ):
            return
        if path == "/api/access/status":
            self._handle_access_status()
        elif path == "/api/play/config":
            self._handle_play_config()
        elif re.fullmatch(r"/api/play/games/[0-9a-f]{32}", path):
            self._handle_owner_game(path.rsplit("/", 1)[1])
        elif path == "/api/games":
            self._handle_public_games()
        elif re.fullmatch(r"/api/games/[0-9a-f]{32}", path):
            self._handle_public_game(path.rsplit("/", 1)[1])
        elif path in ("/", "/landing.html"):
            self.path = "/landing.html"
            super().do_GET()
        elif path in ("/invite", "/invite/", "/invite.html"):
            self.path = "/invite.html"
            super().do_GET()
        elif path in (
            "/analysis",
            "/analysis/",
            "/double_move.html",
            "/doublemove_board.html",
            "/index.html",
        ):
            self.path = "/index.html"
            super().do_GET()
        elif (
            path in ("/play", "/play/", "/play.html")
            or re.fullmatch(r"/play/[0-9a-f]{32}", path)
        ):
            self.path = "/play.html"
            super().do_GET()
        elif (
            path in ("/games", "/games/", "/games.html")
            or re.fullmatch(r"/games/[0-9a-f]{32}", path)
        ):
            self.path = "/games.html"
            super().do_GET()
        elif path in ("/rules", "/rules/", "/rules.html"):
            self.path = "/rules.html"
            super().do_GET()
        elif path == "/api/health":
            self._handle_health()
        elif path in (
            "/api/training/games",
            "/api/latest-game",
            "/api/runs",
        ):
            if self.runtime_config.restricted:
                self._send_json({"error": "Not found"}, 404)
            elif path == "/api/training/games":
                self._handle_all_games()
            elif path == "/api/latest-game":
                self._handle_latest_game()
            else:
                self._handle_runs()
        elif path == "/api/models":
            self._handle_models()
        elif path.startswith("/api/"):
            self._send_json({"error": "Not found"}, 404)
        else:
            super().do_GET()

    def do_OPTIONS(self):
        if urlsplit(self.path).path.startswith("/api/play/"):
            self._send_json({"error": "Method not allowed"}, 405)
            return
        if self.runtime_config.restricted:
            self._send_json({"error": "Method not allowed"}, 405)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            if path.startswith(("/api/play/", "/api/access/")):
                self._check_public_origin()
            elif self.runtime_config.restricted:
                self._check_hosted_origin()
        except RequestError as exc:
            self._send_json({"error": str(exc)}, exc.status)
            return

        if path == "/api/access/redeem":
            self._handle_access_redeem()
            return
        if path == "/api/access/transfer":
            self._handle_access_transfer()
            return
        if _protected_api_path(path) and not self._require_site_access(
            page_request=False
        ):
            return

        if path == "/api/engine/eval":
            self._handle_engine_eval()
        elif path == "/api/engine/eval/stream":
            self._handle_engine_eval_stream()
        elif path == "/api/engine/eval/cancel":
            self._handle_engine_eval_cancel()
        elif path == "/api/engine/move":
            self._handle_engine_move()
        elif path == "/api/engine/newgame":
            self._handle_engine_newgame()
        elif path == "/api/play/games":
            self._handle_create_game()
        else:
            match = re.fullmatch(
                r"/api/play/games/([0-9a-f]{32})/"
                r"(moves|takeback|resign)",
                path,
            )
            if match:
                self._handle_play_mutation(match.group(1), match.group(2))
            else:
                self._send_json({"error": "Not found"}, 404)

    def end_headers(self):
        path = urlsplit(self.path).path
        if (
            not path.startswith(("/api/", "/vendor/"))
            and path.endswith((".html", ".js", ".css"))
        ):
            # First-party files change together and must be fetched as one
            # release. In particular, an older invitation helper cannot
            # understand newer access-transfer credentials. ``no-cache`` is
            # insufficient when a proxy enforces a minimum browser TTL, while
            # ``no-store`` remains unambiguous for browser and edge caches.
            self.send_header(
                "Cache-Control",
                "no-store",
            )
            self.send_header("Cloudflare-CDN-Cache-Control", "no-store")
        if self.runtime_config.restricted:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "object-src 'none'; "
                "base-uri 'none'; "
                "frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Permissions-Policy",
                "camera=(), microphone=(), geolocation=()",
            )
        super().end_headers()

    def list_directory(self, _path):
        self.send_error(404, "Not found")
        return None

    def _access_session_token(self):
        store = self.access_store
        if store is None:
            return None
        return store.parse_session_cookie(self.headers.get("Cookie"))

    def _redirect_legacy_page(self, path):
        """Move legacy-host page visits to the canonical public origin."""

        if not _site_page_path(path) or path in (
            "/invite",
            "/invite/",
            "/invite.html",
        ):
            return False
        canonical = str(self.canonical_origin or "").rstrip("/")
        if not canonical or not self.legacy_origins:
            return False
        try:
            canonical_url = urlsplit(canonical)
            legacy_hosts = {
                urlsplit(origin).netloc.lower()
                for origin in self.legacy_origins
                if urlsplit(origin).scheme in ("http", "https")
                and urlsplit(origin).netloc
            }
        except ValueError:
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        if (
            canonical_url.scheme not in ("http", "https")
            or not canonical_url.netloc
            or host not in legacy_hosts
        ):
            return False

        target = self.path if len(self.path) <= 2048 else "/"
        location = canonical + "/"
        store = self.access_store
        try:
            authorization = (
                store.authorize(self._access_session_token())
                if store is not None
                else None
            )
            if (
                authorization is not None
                and authorization.mode == MODE_INVITE_ONLY
                and authorization.authorized
                and authorization.invitation_id
            ):
                grant = store.create_transfer(self._access_session_token())
                location = (
                    canonical
                    + "/invite?next="
                    + quote(target, safe="")
                    + "#transfer="
                    + grant.token
                )
            elif authorization is not None and authorization.mode == MODE_OPEN:
                location = canonical + target
        except (AccessStoreError, OSError, sqlite3.Error) as exc:
            self.log_message("legacy access transfer failed: %s", exc)
            return False

        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        return True

    def _require_site_access(self, *, page_request):
        store = self.access_store
        if store is None:
            if page_request:
                self.send_error(
                    503, "Access control is temporarily unavailable"
                )
            else:
                self._send_json(
                    {
                        "error": "Access control is temporarily unavailable.",
                        "code": "access_unavailable",
                    },
                    503,
                )
            return False
        try:
            authorization = store.authorize(self._access_session_token())
        except (AccessStoreError, OSError, sqlite3.Error) as exc:
            self.log_message("access authorization failed: %s", exc)
            if page_request:
                self.send_error(
                    503, "Access control is temporarily unavailable"
                )
            else:
                self._send_json(
                    {
                        "error": "Access control is temporarily unavailable.",
                        "code": "access_unavailable",
                    },
                    503,
                )
            return False
        if authorization.authorized:
            return True
        if not page_request:
            self._send_json(
                {
                    "error": "An invitation is required.",
                    "code": "access_required",
                },
                401,
            )
            return False
        target = self.path if len(self.path) <= 2048 else "/play"
        location = "/invite?next=" + quote(target, safe="")
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def _handle_access_status(self):
        store = self.access_store
        if store is None:
            self._send_json(
                {
                    "error": "Access control is temporarily unavailable.",
                    "code": "access_unavailable",
                },
                503,
            )
            return
        try:
            authorization = store.authorize(self._access_session_token())
            self._send_json(
                {
                    "mode": authorization.mode,
                    "authorized": authorization.authorized,
                }
            )
        except (AccessStoreError, OSError, sqlite3.Error) as exc:
            self.log_message("access status failed: %s", exc)
            self._send_json(
                {
                    "error": "Access control is temporarily unavailable.",
                    "code": "access_unavailable",
                },
                503,
            )

    def _handle_access_redeem(self):
        store = self.access_store
        if store is None:
            self._send_json(
                {
                    "error": "Access control is temporarily unavailable.",
                    "code": "access_unavailable",
                },
                503,
            )
            return
        try:
            self.access_redeem_rate_limiter.consume("global")
            body = self._read_json(
                limit=ACCESS_REDEEM_MAX_BODY,
                require_json=True,
            )
            if set(body) != {"token"}:
                raise RequestError("Invitation request is invalid")
            redemption = store.redeem(
                body["token"],
                self._access_session_token(),
            )
            self._send_json(
                {
                    "authorized": True,
                    "remaining_uses": redemption.remaining_redemptions,
                },
                headers={
                    "Set-Cookie": store.session_cookie_header(
                        redemption.session_token
                    )
                },
            )
        except RequestError as exc:
            self._send_json(
                {"error": str(exc), "code": "invalid_request"},
                exc.status,
            )
        except InvalidInvitationError:
            self._send_json(
                {
                    "error": "This invitation is invalid or has been revoked.",
                    "code": "invitation_invalid",
                },
                403,
            )
        except InvitationLimitError:
            self._send_json(
                {
                    "error": (
                        "This invitation has used all of its available "
                        "browser sessions."
                    ),
                    "code": "invitation_exhausted",
                },
                409,
            )
        except PublicPlayError as exc:
            self._send_json(exc.payload(), exc.status)
        except (AccessStoreError, OSError, sqlite3.Error) as exc:
            self.log_message("invitation redemption failed: %s", exc)
            self._send_json(
                {
                    "error": "Access control is temporarily unavailable.",
                    "code": "access_unavailable",
                },
                503,
            )

    def _handle_access_transfer(self):
        store = self.access_store
        if store is None:
            self._send_json(
                {
                    "error": "Access control is temporarily unavailable.",
                    "code": "access_unavailable",
                },
                503,
            )
            return
        try:
            self.access_redeem_rate_limiter.consume("global")
            body = self._read_json(
                limit=ACCESS_REDEEM_MAX_BODY,
                require_json=True,
            )
            if set(body) != {"token"}:
                raise RequestError("Access transfer request is invalid")
            redemption = store.redeem_transfer(
                body["token"],
                self._access_session_token(),
            )
            self._send_json(
                {"authorized": True},
                headers={
                    "Set-Cookie": store.session_cookie_header(
                        redemption.session_token
                    )
                },
            )
        except RequestError as exc:
            self._send_json(
                {"error": str(exc), "code": "invalid_request"},
                exc.status,
            )
        except InvalidTransferError:
            self._send_json(
                {
                    "error": "This access transfer is invalid or has expired.",
                    "code": "transfer_invalid",
                },
                403,
            )
        except PublicPlayError as exc:
            self._send_json(exc.payload(), exc.status)
        except (AccessStoreError, OSError, sqlite3.Error) as exc:
            self.log_message("access transfer failed: %s", exc)
            self._send_json(
                {
                    "error": "Access control is temporarily unavailable.",
                    "code": "access_unavailable",
                },
                503,
            )

    def _check_hosted_origin(self):
        host = (self.headers.get("Host") or "").strip()
        if not host or len(host) > 255 or not HOST_RE.fullmatch(host):
            raise RequestError("Invalid Host header", 400)
        try:
            parsed_host = urlsplit("//" + host)
            if (
                not parsed_host.hostname
                or parsed_host.username is not None
                or parsed_host.password is not None
            ):
                raise ValueError
            parsed_host.port
        except ValueError as exc:
            raise RequestError("Invalid Host header", 400) from exc

        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            raise RequestError("Cross-origin requests are not allowed", 403)

        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return
        parsed_origin = urlsplit(origin)
        if (
            parsed_origin.scheme not in ("http", "https")
            or not parsed_origin.netloc
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path not in ("", "/")
            or parsed_origin.query
            or parsed_origin.fragment
            or parsed_origin.netloc.lower() != host.lower()
        ):
            raise RequestError("Cross-origin requests are not allowed", 403)

    def _check_public_origin(self):
        try:
            PublicPlayService.validate_write_origin(
                host=self.headers.get("Host"),
                origin=self.headers.get("Origin"),
                sec_fetch_site=self.headers.get("Sec-Fetch-Site"),
                allowed_origins=tuple(self.public_origins),
            )
        except PublicPlayError as exc:
            raise RequestError(str(exc), exc.status) from exc

    def _require_play_service(self):
        service = self.play_service
        if service is None:
            raise PublicPlayError(
                "Public play is unavailable.", 503, "play_unavailable"
            )
        return service

    def _session_token(self):
        service = self._require_play_service()
        return service.parse_session_cookie(self.headers.get("Cookie"))

    def _handle_public_play_error(self, exc):
        if isinstance(exc, PublicPlayError):
            self._send_json(exc.payload(), exc.status)
            return
        self.log_message("public play request failed: %s", exc)
        self._send_json(
            {
                "error": "Public play request failed.",
                "code": "internal_error",
            },
            500,
        )

    def _handle_play_config(self):
        try:
            service = self._require_play_service()
            config = service.get_config(self._session_token())
            self._send_json(
                config.payload,
                headers={
                    "Set-Cookie": service.session_cookie_header(
                        config.session_token
                    )
                },
            )
        except Exception as exc:
            self._handle_public_play_error(exc)

    def _handle_owner_game(self, public_id):
        try:
            service = self._require_play_service()
            result = service.get_owner_game(
                self._session_token(), public_id
            )
            self._send_json(result)
        except Exception as exc:
            self._handle_public_play_error(exc)

    def _handle_create_game(self):
        try:
            service = self._require_play_service()
            result = service.create_game(
                self._session_token(),
                self.headers.get("X-CSRF-Token"),
                self.headers.get("Idempotency-Key"),
                self._read_json(limit=HOSTED_MAX_BODY, require_json=True),
            )
            self._send_json(result, 201)
        except RequestError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self._handle_public_play_error(exc)

    def _handle_play_mutation(self, public_id, operation):
        try:
            service = self._require_play_service()
            arguments = (
                self._session_token(),
                self.headers.get("X-CSRF-Token"),
                public_id,
                self.headers.get("Idempotency-Key"),
                self._read_json(limit=HOSTED_MAX_BODY, require_json=True),
            )
            method = {
                "moves": service.move,
                "takeback": service.takeback,
                "resign": service.resign,
            }[operation]
            self._send_json(method(*arguments))
        except RequestError as exc:
            self._send_json({"error": str(exc)}, exc.status)
        except Exception as exc:
            self._handle_public_play_error(exc)

    def _handle_public_games(self):
        try:
            service = self._require_play_service()
            self.archive_rate_limiter.consume("global")
            query = parse_qs(
                urlsplit(self.path).query,
                keep_blank_values=True,
                strict_parsing=True,
            )
            if set(query) - {"limit", "cursor"} or any(
                len(values) != 1 for values in query.values()
            ):
                raise PublicPlayError("Archive query is invalid.")
            raw_limit = query.get("limit", ["50"])[0]
            if not raw_limit.isdigit():
                raise PublicPlayError("Archive limit is invalid.")
            cursor = query.get("cursor", [None])[0]
            if cursor == "":
                raise PublicPlayError("Archive cursor is invalid.")
            self._send_json(
                service.list_public_games(
                    limit=int(raw_limit), cursor=cursor
                )
            )
        except (ValueError, PublicPlayError) as exc:
            self._handle_public_play_error(
                exc
                if isinstance(exc, PublicPlayError)
                else PublicPlayError("Archive query is invalid.")
            )
        except Exception as exc:
            self._handle_public_play_error(exc)

    def _handle_public_game(self, public_id):
        try:
            service = self._require_play_service()
            self.archive_rate_limiter.consume("global")
            self._send_json(service.get_public_game(public_id))
        except Exception as exc:
            self._handle_public_play_error(exc)

    def _handle_health(self):
        config = self.runtime_config
        engine_artifact_ready = os.path.isfile(config.lc0_path) and os.access(
            config.lc0_path, os.X_OK
        )
        engine_dead = _engine_process_dead(ENGINE)
        engine_ready = engine_artifact_ready and not engine_dead
        if config.weights_path:
            model_ready = os.path.isfile(config.weights_path)
        elif config.restricted:
            model_ready = False
        else:
            model_ready = bool(self._discover_weights())
        if model_ready and self.model_runtimes:
            try:
                _check_model_files(self.model_runtimes[0])
            except OSError:
                model_ready = False
        service_ready = self.play_service is not None
        storage_ready = False
        if service_ready:
            try:
                storage_ready = (
                    self.play_service.store.health_check() is True
                )
            except Exception:
                storage_ready = False
        play_ready = service_ready and storage_ready
        access_ready = False
        if self.access_store is not None:
            try:
                access_ready = self.access_store.health_check() is True
            except Exception:
                access_ready = False
        ready = engine_ready and model_ready and play_ready and access_ready
        engine_status = (
            "ready"
            if engine_ready
            else ("unavailable" if engine_dead else "missing")
        )
        self._send_json(
            {
                "status": "ok" if ready else "unavailable",
                "profile": config.profile,
                "engine": engine_status,
                "model": "ready" if model_ready else "missing",
                "public_play": "ready" if play_ready else "unavailable",
                "storage": "ready" if storage_ready else "unavailable",
                "access": "ready" if access_ready else "unavailable",
            },
            200 if ready else 503,
        )

    def _resolve_run_dir(self):
        """Auto-discover the latest run directory."""
        run_dir = _find_latest_run_dir(self.runs_root)
        if not run_dir:
            return None
        return run_dir

    def _handle_runs(self):
        """List all available runs."""
        try:
            runs = []
            for name in sorted(os.listdir(self.runs_root)):
                path = os.path.join(self.runs_root, name)
                if os.path.isdir(path) and os.path.isdir(os.path.join(path, "logs")):
                    runs.append({"name": name, "path": path})
            self._send_json({"runs": runs})
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _discover_weights(self):
        configured = self.runtime_config.weights_path
        pattern = os.path.join(self.runs_root, "*", "exported_models", "net-*.pb.gz")
        candidates = glob.glob(pattern)
        candidates.sort(key=os.path.getmtime, reverse=True)
        candidates[:0] = [model.path for model in self.model_runtimes]
        if configured and os.path.isfile(configured):
            candidates.insert(0, configured)
        models = []
        seen = set()
        catalog = {model.path: model.choice.label for model in self.model_runtimes}
        for path in candidates:
            normalized = os.path.abspath(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            if configured and normalized == os.path.abspath(configured):
                run_dir = self.runtime_config.profile
                name = os.path.basename(path)
            else:
                run_dir = os.path.basename(os.path.dirname(os.path.dirname(path)))
                name = f"{run_dir} / {os.path.basename(path)}"
            name = catalog.get(normalized, name)
            models.append({
                "path": path,
                "name": name,
                "run": run_dir,
                "mtime": os.path.getmtime(path),
            })
        return models

    def _handle_models(self):
        config = self.runtime_config
        if config.restricted:
            runtimes = tuple(self.model_runtimes)
            if runtimes:
                models = [
                    {
                        "id": runtime.choice.id,
                        "name": runtime.choice.label,
                    }
                    for runtime in runtimes
                ]
                default_model = runtimes[0].choice.id
            elif config.weights_path and os.path.isfile(config.weights_path):
                default_model = _public_model_id(
                    config.weights_path, config.weights_path
                )
                models = [{
                    "id": default_model,
                    "name": _model_stem(config.weights_path),
                }]
            else:
                models = []
                default_model = ""
            self._send_json({
                "models": models,
                "default_model": default_model,
                "default_backend": config.backend,
                "available_backends": list(config.available_backends),
                "default_nodes": config.nodes,
                "max_nodes": HOSTED_MAX_SEARCH_NODES,
                "default_smart_pruning_factor": 0,
                "profile": config.profile,
                "locked": False,
                "managed": True,
            })
            return

        models = self._discover_weights()
        default_weights = (
            config.weights_path
            if config.weights_path and os.path.isfile(config.weights_path)
            else (models[0]["path"] if models else "")
        )
        self._send_json({
            "models": models,
            "default_weights": default_weights,
            "default_lc0": config.lc0_path,
            "default_backend": config.backend,
            "available_backends": list(config.available_backends),
            "default_nodes": config.nodes,
            "default_minibatch_size": config.minibatch_size,
            "default_smart_pruning_factor": 0,
            "default_use_modules": config.use_modules,
            "profile": config.profile,
            "locked": False,
            "managed": False,
        })

    def _read_json(self, *, limit=None, require_json=False):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise RequestError("Invalid Content-Length header") from exc
        if length < 0:
            raise RequestError("Invalid Content-Length header")
        body_limit = (
            limit
            if limit is not None
            else (
                HOSTED_MAX_BODY
                if self.runtime_config.restricted
                else DEFAULT_MAX_BODY
            )
        )
        if length > body_limit:
            raise RequestError("Request body is too large", 413)
        if self.runtime_config.restricted or require_json:
            content_type = self.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                raise RequestError("Content-Type must be application/json", 415)
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("Malformed JSON request") from exc
        if not isinstance(body, dict):
            raise RequestError("JSON request must be an object")
        return body

    def _engine_config_from_body(self, body):
        config = self.runtime_config
        if config.restricted:
            forbidden = sorted(
                set(body)
                - {
                    "fen",
                    "moves",
                    "engine_context_id",
                    "position_revision",
                    "analysis_request_id",
                    "model_id",
                    "backend",
                    "nodes",
                    "smart_pruning_factor",
                    "disable_ability_edges",
                    "options",
                }
            )
            if forbidden:
                raise RequestError(
                    "Hosted runtime does not accept local engine paths or "
                    "unsupported settings"
                )
            runtimes = tuple(self.model_runtimes)
            selected_model_id = body.get("model_id")
            if runtimes:
                models_by_id = {
                    runtime.choice.id: runtime for runtime in runtimes
                }
                if selected_model_id is None:
                    selected_runtime = runtimes[0]
                elif not isinstance(selected_model_id, str):
                    raise RequestError("Model is not available")
                else:
                    selected_runtime = models_by_id.get(selected_model_id)
                    if selected_runtime is None:
                        raise RequestError("Model is not available")
                try:
                    metadata = os.stat(selected_runtime.path)
                except OSError as exc:
                    raise RequestError(
                        "Hosted model is unavailable", 503
                    ) from exc
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != selected_runtime.size
                    or metadata.st_mtime_ns != selected_runtime.mtime_ns
                ):
                    raise RequestError("Hosted model is unavailable", 503)
                weights_path = selected_runtime.path
            else:
                default_id = _public_model_id(
                    config.weights_path or "", config.weights_path or ""
                )
                if selected_model_id not in (None, default_id):
                    raise RequestError("Model is not available")
                if not os.path.isfile(config.weights_path or ""):
                    raise RequestError("Hosted model is unavailable", 503)
                weights_path = config.weights_path
            if not (
                os.path.isfile(config.lc0_path)
                and os.access(config.lc0_path, os.X_OK)
            ):
                raise RequestError("Hosted engine is unavailable", 503)
            backend = body.get("backend", config.backend)
            if (
                not isinstance(backend, str)
                or backend not in config.available_backends
            ):
                raise RequestError("Backend is not available")

            raw_disable = body.get("disable_ability_edges", False)
            if not isinstance(raw_disable, bool):
                raise RequestError("Ability edges setting must be true or false")

            raw_smart_pruning = body.get("smart_pruning_factor", 0)
            if isinstance(raw_smart_pruning, bool):
                raise RequestError("Smart pruning must be between 0 and 10")
            try:
                smart_pruning = float(raw_smart_pruning)
            except (TypeError, ValueError) as exc:
                raise RequestError(
                    "Smart pruning must be between 0 and 10"
                ) from exc
            if not math.isfinite(smart_pruning) or not 0 <= smart_pruning <= 10:
                raise RequestError("Smart pruning must be between 0 and 10")

            options = body.get("options", {})
            if not isinstance(options, dict):
                raise RequestError("Engine options must be an object")
            unsupported_options = sorted(
                set(options) - {"HistoryFill", "CPuct", "PolicyTemperature"}
            )
            if unsupported_options:
                raise RequestError("Engine option is not available")
            history_fill = options.get("HistoryFill", "repeat_current")
            if history_fill not in ("repeat_current", "fen_only"):
                raise RequestError("History fill is not available")

            def hosted_float_option(name, default, minimum, maximum):
                raw_value = options.get(name, default)
                if isinstance(raw_value, bool):
                    raise RequestError(f"{name} is outside the allowed range")
                try:
                    value = float(raw_value)
                except (TypeError, ValueError) as exc:
                    raise RequestError(
                        f"{name} is outside the allowed range"
                    ) from exc
                if (
                    not math.isfinite(value)
                    or value < minimum
                    or value > maximum
                ):
                    raise RequestError(f"{name} is outside the allowed range")
                return str(raw_value).strip()

            opts = {
                "HistoryFill": history_fill,
                "CPuct": hosted_float_option("CPuct", "1.2", 0, 100),
                "PolicyTemperature": hosted_float_option(
                    "PolicyTemperature", "1.0", 0.000001, 100
                ),
                "DisableAbilityEdges": "true" if raw_disable else "false",
                "SmartPruningFactor": format(smart_pruning, ".15g"),
            }
            return (
                config.lc0_path,
                weights_path,
                backend,
                config.minibatch_size,
                config.use_modules,
                opts,
            )

        weights = body.get("weights") or ""
        if "model_id" in body:
            selected = next((model for model in self.model_runtimes
                             if model.choice.id == body["model_id"]), None)
            if selected is None:
                raise RequestError("Model is not available")
            weights = selected.path
        if not weights and config.weights_path and os.path.isfile(config.weights_path):
            weights = config.weights_path
        if not weights:
            models = self._discover_weights()
            weights = models[0]["path"] if models else ""
        if not weights:
            raise ValueError("No weights configured and no exported models found")
        lc0_path = body.get("lc0") or config.lc0_path
        if "backend" in body:
            backend = body["backend"]
            if (
                not isinstance(backend, str)
                or backend not in config.available_backends
            ):
                raise RequestError("Backend is not available")
        else:
            backend = config.backend
        minibatch_size = int(
            body.get("minibatch_size") or config.minibatch_size
        )
        raw_use_modules = body.get("use_modules", config.use_modules)
        if isinstance(raw_use_modules, str):
            use_modules = raw_use_modules.strip().lower() not in ("0", "false", "no", "off")
        else:
            use_modules = bool(raw_use_modules)
        options = body.get("options") or {}
        disable_ability_edges = bool(body.get("disable_ability_edges", False))
        smart_pruning_factor = body.get("smart_pruning_factor", 0)
        opts = {
            "DisableAbilityEdges": "true" if disable_ability_edges else "false",
            "SmartPruningFactor": str(smart_pruning_factor),
        }
        for key in (
            "HistoryFill",
            "CPuct",
            "PolicyTemperature",
            "FpuValue",
            "StickyEndgames",
            "TwoFoldDraws",
            "MaxCollisionVisits",
            "MaxCollisionEvents",
            "OutOfOrderEval",
            "SmartPruningFactor",
        ):
            if key in options and str(options[key]).strip() != "":
                opts[key] = str(options[key]).strip()
        return lc0_path, weights, backend, minibatch_size, use_modules, opts

    def _get_engine(self, body):
        global ENGINE, ENGINE_KEY, ENGINE_CONTEXT_ID
        _discard_dead_engine()
        lc0_path, weights, backend, minibatch_size, use_modules, opts = self._engine_config_from_body(body)
        model = next((runtime for runtime in self.model_runtimes
                      if runtime.path == os.path.abspath(weights)), None)
        try:
            if model is not None:
                _check_model_files(model)
                fallback = model.fallback
            else:
                fallback = _required_fallback(weights)
        except OSError as exc:
            raise RequestError("Model or required fallback is unavailable", 503) from exc
        if fallback is not None:
            opts["FallbackWeights"] = fallback.path
        key = (lc0_path, weights, backend, minibatch_size, use_modules, tuple(sorted(opts.items())))
        if ENGINE is not None and ENGINE_KEY == key:
            return ENGINE
        if ENGINE is not None:
            try:
                ENGINE.quit()
            except Exception:
                pass
        ENGINE = Lc0(
            lc0_path, weights, backend=backend, minibatch_size=minibatch_size,
            opts=opts, launch_with_modules=use_modules,
            fallback_weights=opts.get("FallbackWeights"),
        )
        ENGINE_KEY = key
        ENGINE_CONTEXT_ID = None
        return ENGINE

    def _engine_request_metadata(self, body):
        """Validate the tree-ownership token and stale-response revision."""
        if "engine_context_id" in body:
            context_id = body["engine_context_id"]
            if (
                not isinstance(context_id, str)
                or not ENGINE_CONTEXT_RE.fullmatch(context_id)
            ):
                raise RequestError(
                    "engine_context_id must be a 16-128 character opaque ID"
                )
        else:
            # Retain compatibility for CLI/API clients predating context-aware
            # tree reuse without letting unrelated legacy clients inherit one
            # another's tree. Browser clients always send a stable random ID.
            context_id = "legacy_" + secrets.token_hex(16)

        revision = body.get("position_revision", 0)
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or revision > MAX_POSITION_REVISION
        ):
            raise RequestError(
                "position_revision must be a non-negative safe integer"
            )
        return context_id, revision

    @staticmethod
    def _analysis_request_id(body):
        request_id = body.get("analysis_request_id")
        if request_id is None:
            return None
        if (
            isinstance(request_id, bool)
            or not isinstance(request_id, int)
            or request_id < 0
            or request_id > MAX_POSITION_REVISION
        ):
            raise RequestError(
                "analysis_request_id must be a non-negative safe integer"
            )
        return request_id

    def _engine_for_context(self, body, context_id, force_clear=False):
        """Return the configured engine with one analysis context owning it."""
        global ENGINE, ENGINE_CONTEXT_ID
        internal_context_id = ANALYSIS_CONTEXT_PREFIX + context_id
        previous_engine = ENGINE
        lc0 = self._get_engine(body)
        launched = lc0 is not previous_engine
        if force_clear or (
            not launched and ENGINE_CONTEXT_ID != internal_context_id
        ):
            lc0.new_game()
        ENGINE_CONTEXT_ID = internal_context_id
        return lc0

    @staticmethod
    def _validate_search_position(info):
        if info["result"] != "ongoing":
            raise RequestError("Position is already terminal")
        if not info["legal"]:
            raise RequestError("Position has no legal moves")

    @staticmethod
    def _terminal_analysis_payload(position, target_nodes):
        """Describe a terminal root without asking lc0 to search past it."""

        return {
            "terminal": True,
            "game_result": position["result"],
            "nodes": target_nodes,
            "target_nodes": target_nodes,
            "actual_nodes": 0,
            "bestmove": None,
            "lines": [],
            "verbose": [],
            "position": position,
        }

    @staticmethod
    def _validate_engine_move(move, info):
        if not move or move in ("(none)", "0000"):
            raise RuntimeError("lc0 returned no move for a non-terminal position")
        if move not in info["legal"]:
            raise RuntimeError(
                f"lc0 returned move {move!r}, which is not legal in its "
                "authoritative position"
            )

    @staticmethod
    def _set_and_inspect(lc0, fen, moves):
        try:
            lc0.set_position(fen, moves)
            return lc0.position_info()
        except Lc0PositionError as exc:
            raise RequestError(f"Invalid position: {exc}") from exc

    def _position_from_body(self, body):
        raw_fen = body.get("fen") or "startpos"
        if not isinstance(raw_fen, str):
            raise RequestError("FEN must be a string")
        fen = raw_fen.strip()
        moves = body.get("moves") or []
        if isinstance(moves, str):
            moves = moves.split()
        elif not isinstance(moves, list):
            raise RequestError("Moves must be a string or list")
        if self.runtime_config.restricted:
            try:
                fen.encode("utf-8", "strict")
            except UnicodeEncodeError as exc:
                raise RequestError(
                    "FEN is too long or contains invalid characters"
                ) from exc
            if len(fen) > 256 or any(
                ord(char) < 32 or ord(char) == 127 for char in fen
            ):
                raise RequestError("FEN is too long or contains invalid characters")
            if len(moves) > HOSTED_MAX_MOVES:
                raise RequestError(
                    f"At most {HOSTED_MAX_MOVES} moves may be supplied"
                )
            cleaned_moves = []
            for move in moves:
                if not isinstance(move, str):
                    raise RequestError("Every move must be a UCI string")
                move = move.strip()
                if not MOVE_RE.fullmatch(move):
                    raise RequestError(f"Invalid UCI move: {move!r}")
                cleaned_moves.append(move)
            moves = cleaned_moves
        else:
            moves = [str(move).strip() for move in moves if str(move).strip()]
        return fen, moves

    def _search_nodes_from_body(self, body):
        raw_nodes = body.get("nodes")
        if raw_nodes in (None, ""):
            return self.runtime_config.nodes
        if isinstance(raw_nodes, bool):
            raise RequestError("Visits must be a positive integer")
        if isinstance(raw_nodes, int):
            nodes = raw_nodes
        elif (
            isinstance(raw_nodes, str)
            and re.fullmatch(r"[0-9]+", raw_nodes)
        ):
            nodes = int(raw_nodes)
        else:
            raise RequestError("Visits must be a positive integer")
        maximum = (
            HOSTED_MAX_SEARCH_NODES
            if self.runtime_config.restricted
            else MAX_SEARCH_NODES
        )
        if not 1 <= nodes <= maximum:
            raise RequestError(
                f"Visits must be between 1 and {maximum}"
            )
        return nodes

    @contextmanager
    def _engine_session(self):
        if self.runtime_config.restricted:
            acquired = ENGINE_LOCK.acquire(
                timeout=ENGINE_LOCK_WAIT_SECONDS
            )
        else:
            ENGINE_LOCK.acquire()
            acquired = True
        if not acquired:
            raise RequestError("Engine is busy; try again shortly", 503)
        try:
            yield
        finally:
            ENGINE_LOCK.release()

    def _handle_engine_error(self, exc):
        if isinstance(exc, RequestError):
            self._send_json({"error": str(exc)}, exc.status)
        elif isinstance(exc, PublicPlayError):
            self._send_json(exc.payload(), exc.status)
        elif self.runtime_config.restricted:
            self.log_message("hosted engine request failed: %s", exc)
            self._send_json({"error": "Engine request failed"}, 500)
        else:
            self._send_json({"error": str(exc)}, 500)

    def _handle_engine_eval(self):
        try:
            self.engine_rate_limiter.consume("global")
            body = self._read_json()
            context_id, revision = self._engine_request_metadata(body)
            nodes = self._search_nodes_from_body(body)
            with self._engine_session():
                lc0 = self._engine_for_context(body, context_id)
                fen, moves = self._position_from_body(body)
                position = self._set_and_inspect(lc0, fen, moves)
                if position["result"] == "ongoing":
                    self._validate_search_position(position)
                    block, best = lc0.verbose_stats(nodes)
                    response = {
                        "nodes": nodes,
                        "actual_nodes": self._verbose_root_nodes(block),
                        "bestmove": best,
                        "verbose": block,
                        "position": position,
                    }
                else:
                    response = self._terminal_analysis_payload(
                        position, nodes
                    )
            self._send_json({
                **response,
                "engine_context_id": context_id,
                "position_revision": revision,
            })
        except Exception as e:
            self._handle_engine_error(e)

    def _begin_engine_stream(self):
        connection = getattr(self, "connection", None)
        if connection is not None and hasattr(connection, "settimeout"):
            connection.settimeout(ENGINE_STREAM_WRITE_TIMEOUT_SECONDS)
        self.send_response(200)
        self.send_header(
            "Content-Type", "application/x-ndjson; charset=utf-8"
        )
        self.send_header("Cache-Control", "no-store, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        if not self.runtime_config.restricted:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type"
            )
        self.end_headers()
        self.close_connection = True

    def _send_engine_stream_record(self, record):
        encoded = (
            json.dumps(record, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self.wfile.write(encoded)
        self.wfile.flush()

    @staticmethod
    def _verbose_root_nodes(verbose):
        for line in reversed(verbose or []):
            if not re.match(r"^info string node(?:\s|$)", str(line)):
                continue
            match = re.search(r"(?:^|\s)N:\s*(\d+)(?=\s|$)", line)
            if match:
                return int(match.group(1))
        return None

    def _handle_engine_eval_stream(self):
        stream_started = False
        disconnected = False
        latest_lines = []
        request_ticket = None
        context_id = None
        revision = 0
        request_id = None
        lc0 = None
        try:
            body = self._read_json()
            context_id, revision = self._engine_request_metadata(body)
            request_id = self._analysis_request_id(body)
            nodes = self._search_nodes_from_body(body)
            request_ticket = self.analysis_requests.begin(
                context_id,
                request_id,
            )
            if not request_ticket.accepted:
                raise RequestError(
                    "Analysis request was superseded by a newer position",
                    409,
                )
            with self._engine_session():
                if request_ticket.cancel_event.is_set():
                    raise RequestError(
                        "Analysis request was superseded by a newer position",
                        409,
                    )
                self.engine_rate_limiter.consume("global")
                lc0 = self._engine_for_context(body, context_id)
                fen, moves = self._position_from_body(body)
                position = self._set_and_inspect(lc0, fen, moves)
                if position["result"] != "ongoing":
                    self._begin_engine_stream()
                    stream_started = True
                    self._send_engine_stream_record({
                        "type": "complete",
                        **self._terminal_analysis_payload(position, nodes),
                        "engine_context_id": context_id,
                        "position_revision": revision,
                        **(
                            {"analysis_request_id": request_id}
                            if request_id is not None
                            else {}
                        ),
                    })
                    return
                self._validate_search_position(position)
                self._begin_engine_stream()
                stream_started = True

                def send_snapshot(actual_nodes, verbose, info):
                    nonlocal disconnected, latest_lines
                    latest_lines = _parse_uci_analysis_lines(
                        getattr(info, "lines", (info,)),
                        position["turn"],
                    )
                    pv_match = re.search(r"(?:^|\s)pv\s+(\S+)", info)
                    principal_move = (
                        latest_lines[0]["pv"][0]
                        if latest_lines and latest_lines[0].get("pv")
                        else (pv_match.group(1) if pv_match else None)
                    )
                    record = {
                        "type": "progress",
                        "nodes": actual_nodes,
                        "target_nodes": nodes,
                        "bestmove": principal_move,
                        "info": info,
                        "lines": latest_lines,
                        "verbose": verbose,
                        "engine_context_id": context_id,
                        "position_revision": revision,
                    }
                    if request_id is not None:
                        record["analysis_request_id"] = request_id
                    try:
                        self._send_engine_stream_record(record)
                    except OSError:
                        disconnected = True
                        return False
                    return True

                block, best, actual_nodes, cancelled = (
                    lc0.verbose_stats_stream(
                        nodes,
                        send_snapshot,
                        multipv=ANALYSIS_MULTIPV,
                        cancel_event=request_ticket.cancel_event,
                        initial_snapshot=True,
                    )
                )
            if cancelled or disconnected:
                return
            self._send_engine_stream_record({
                "type": "complete",
                "nodes": nodes,
                "target_nodes": nodes,
                "actual_nodes": actual_nodes,
                "bestmove": best,
                "lines": latest_lines,
                "verbose": block,
                "position": position,
                "engine_context_id": context_id,
                "position_revision": revision,
                **(
                    {"analysis_request_id": request_id}
                    if request_id is not None
                    else {}
                ),
            })
        except Exception as exc:
            retriable = (
                lc0 is not None
                and not isinstance(exc, (RequestError, OSError))
            )
            if retriable:
                _quarantine_analysis_engine(lc0, context_id)
            if not stream_started:
                self._handle_engine_error(exc)
                return
            if self.runtime_config.restricted:
                self.log_message(
                    "hosted engine stream failed: %s", exc
                )
                message = "Engine request failed"
            else:
                message = str(exc)
            try:
                self._send_engine_stream_record({
                    "type": "error",
                    "error": message,
                    "retriable": retriable,
                    "engine_context_id": context_id,
                    "position_revision": revision,
                    **(
                        {"analysis_request_id": request_id}
                        if request_id is not None
                        else {}
                    ),
                })
            except OSError:
                pass
        finally:
            if request_ticket is not None:
                self.analysis_requests.finish(request_ticket)

    def _handle_engine_eval_cancel(self):
        try:
            body = self._read_json()
            context_id, revision = self._engine_request_metadata(body)
            request_id = self._analysis_request_id(body)
            if request_id is None:
                raise RequestError(
                    "analysis_request_id is required for cancellation"
                )
            cancelled = self.analysis_requests.cancel(
                context_id,
                request_id,
            )
            self._send_json(
                {
                    "cancelled": cancelled,
                    "engine_context_id": context_id,
                    "position_revision": revision,
                    "analysis_request_id": request_id,
                },
                202,
            )
        except Exception as exc:
            self._handle_engine_error(exc)

    def _handle_engine_move(self):
        try:
            self.engine_rate_limiter.consume("global")
            body = self._read_json()
            context_id, revision = self._engine_request_metadata(body)
            nodes = self._search_nodes_from_body(body)
            with self._engine_session():
                lc0 = self._engine_for_context(body, context_id)
                fen, moves = self._position_from_body(body)
                position = self._set_and_inspect(lc0, fen, moves)
                self._validate_search_position(position)
                best, ponder, info = lc0.go_nodes(nodes)
                self._validate_engine_move(best, position)
                engine_moves = []
                infos = []
                engine_moves.append(best)
                if info:
                    infos.append(info)
                position = self._set_and_inspect(
                    lc0, fen, moves + engine_moves
                )
                if best.endswith("+"):
                    if (
                        position["result"] != "ongoing"
                        or not position["ability"]["mid"]
                    ):
                        raise RuntimeError(
                            "lc0 did not enter a live mid-double position "
                            "after an ability move"
                        )
                    if not position["legal"]:
                        raise RuntimeError(
                            "lc0 reported no legal second half of a double move"
                        )
                    best2, ponder2, info2 = lc0.go_nodes(nodes)
                    self._validate_engine_move(best2, position)
                    engine_moves.append(best2)
                    if info2:
                        infos.append(info2)
                    position = self._set_and_inspect(
                        lc0, fen, moves + engine_moves
                    )
                    if position["ability"]["mid"]:
                        raise RuntimeError(
                            "lc0 remained mid-double after the second move"
                        )
                elif position["ability"]["mid"]:
                    raise RuntimeError(
                        "lc0 entered a mid-double position after an ordinary "
                        "move"
                    )
            self._send_json({
                "nodes": nodes,
                "moves": engine_moves,
                "bestmove": best,
                "ponder": ponder,
                "info": infos,
                "position": position,
                "engine_context_id": context_id,
                "position_revision": revision,
            })
        except Exception as e:
            self._handle_engine_error(e)

    def _handle_engine_newgame(self):
        try:
            self.engine_rate_limiter.consume("global")
            body = self._read_json()
            context_id, revision = self._engine_request_metadata(body)
            with self._engine_session():
                self._engine_for_context(
                    body, context_id, force_clear=True
                )
            self._send_json({
                "ok": True,
                "engine_context_id": context_id,
                "position_revision": revision,
            })
        except Exception as e:
            self._handle_engine_error(e)

    def _handle_all_games(self):
        try:
            run_dir = self._resolve_run_dir()
            if not run_dir:
                self._send_json({"error": "No run directories found"}, 404)
                return

            sp_logs = _find_latest_completed_selfplay_logs(run_dir)
            if not sp_logs:
                self._send_json({"error": "No completed selfplay log found"}, 404)
                return

            # Concatenate games from all logs of this iter (multi-proc returns
            # one log per proc; single-proc returns a 1-element list).
            games = []
            for sp_log in sp_logs:
                with open(sp_log) as f:
                    for line in f:
                        if line.startswith("gameready"):
                            g = _parse_game_line(line)
                            if g:
                                games.append(g)

            # Extract iteration from any of the filenames (all share the same N).
            iter_match = re.search(r'selfplay-iter(\d+)', os.path.basename(sp_logs[0]))
            iteration = int(iter_match.group(1)) if iter_match else None

            self._send_json({
                "source": sp_logs[0] if len(sp_logs) == 1 else sp_logs,
                "log_count": len(sp_logs),
                "run_name": os.path.basename(run_dir),
                "iteration": iteration,
                "game_count": len(games),
                "games": games,
            })
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_latest_game(self):
        try:
            run_dir = self._resolve_run_dir()
            if not run_dir:
                self._send_json({"error": "No run directories found"}, 404)
                return

            result = subprocess.run(
                [
                    sys.executable, UCI_TOOL,
                    "--auto", "--json",
                    "--run-dir", run_dir,
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                self._send_json({"error": result.stderr.strip()}, 500)
                return

            # Parse full stdout as JSON
            data = json.loads(result.stdout.strip())
            self._send_json(data)
        except subprocess.TimeoutExpired:
            self._send_json({"error": "Timed out fetching game"}, 504)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _send_json(self, data, status=200, headers=None):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        path = urlsplit(getattr(self, "path", "")).path
        if (
            not self.runtime_config.restricted
            and not path.startswith("/api/play/")
            and not path.startswith("/api/games")
        ):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[server] {fmt % args}", file=sys.stderr)


def _shutdown_engine():
    global ENGINE, ENGINE_KEY, ENGINE_CONTEXT_ID
    with ENGINE_LOCK:
        if ENGINE is not None:
            try:
                ENGINE.quit()
            except Exception as exc:
                print(f"[server] Error while stopping lc0: {exc}", file=sys.stderr)
        ENGINE = None
        ENGINE_KEY = None
        ENGINE_CONTEXT_ID = None


def _run_game_maintenance(store):
    try:
        result = store.cleanup()
    except Exception as exc:
        print(f"[server] Game maintenance failed: {exc}", file=sys.stderr)
        return None
    if any(result.values()):
        print(
            "[server] Game maintenance: "
            + ", ".join(
                f"{name}={count}" for name, count in result.items()
            ),
            file=sys.stderr,
        )
    return result


def _game_maintenance_loop(store, stopping):
    while not stopping.wait(GAME_MAINTENANCE_INTERVAL_SECONDS):
        _run_game_maintenance(store)


def main():
    global ACCESS_STORE, PLAY_SERVICE
    parser = _build_arg_parser()
    args = parser.parse_args()
    try:
        config = _resolve_runtime_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    BoardServer.runtime_config = config
    BoardServer.runs_root = config.runs_root
    try:
        (
            PLAY_SERVICE,
            public_origins,
            database_path,
            model_runtimes,
        ) = _initialize_public_play(config)
    except (OSError, ValueError, sqlite3.Error, GameStoreError) as exc:
        PLAY_SERVICE = None
        public_origins = ()
        model_runtimes = ()
        database_path = os.environ.get(
            "DM_DATABASE_PATH", DEFAULT_DATABASE_PATH
        )
        print(f"Public play unavailable: {exc}", file=sys.stderr)
    BoardServer.play_service = PLAY_SERVICE
    BoardServer.model_runtimes = model_runtimes
    BoardServer.public_origins = public_origins
    BoardServer.canonical_origin = public_origins[0] if public_origins else ""
    BoardServer.legacy_origins = tuple(public_origins[1:])
    try:
        ACCESS_STORE, access_database_path = _initialize_access_control(
            database_path,
            public_origins,
        )
    except (OSError, ValueError, sqlite3.Error, AccessStoreError) as exc:
        ACCESS_STORE = None
        access_database_path = default_access_database_path()
        print(f"Access control unavailable: {exc}", file=sys.stderr)
    BoardServer.access_store = ACCESS_STORE

    if PLAY_SERVICE is not None:
        _run_game_maintenance(PLAY_SERVICE.store)

    server = BoardHTTPServer((config.host, config.port), BoardServer)
    print(f"Serving on http://{config.host}:{config.port}")
    print(f"Profile: {config.profile}")
    print(f"lc0: {config.lc0_path}")
    print(f"Weights: {config.weights_path or 'auto-discover from runs'}")
    print(
        f"Engine: backend={config.backend}, nodes={config.nodes}, "
        f"minibatch={config.minibatch_size}, modules={config.use_modules}"
    )
    print(
        "Public play: "
        + (
            f"ready · database={database_path}"
            if PLAY_SERVICE is not None
            else "unavailable"
        )
    )
    print(
        "Access control: "
        + (
            f"{ACCESS_STORE.get_mode()} · database={access_database_path}"
            if ACCESS_STORE is not None
            else "unavailable (protected routes fail closed)"
        )
    )
    if config.restricted:
        print(
            "Hosted restrictions: server-managed engine/model paths; "
            "run browsing disabled"
        )
    else:
        print(f"Runs root: {config.runs_root}")
        print("Auto-discovering latest run...")
        latest = _find_latest_run_dir(config.runs_root)
        if latest:
            print(f"  → {os.path.basename(latest)}")
        else:
            print("  → No runs found yet")

    stopping = threading.Event()
    maintenance_thread = None
    if PLAY_SERVICE is not None:
        maintenance_thread = threading.Thread(
            target=_game_maintenance_loop,
            args=(PLAY_SERVICE.store, stopping),
            name="game-maintenance",
            daemon=True,
        )
        maintenance_thread.start()

    def request_shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        print(f"\nReceived signal {signum}; shutting down.")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        stopping.set()
        server.server_close()
        if maintenance_thread is not None:
            maintenance_thread.join(timeout=2)
        _shutdown_engine()


if __name__ == "__main__":
    main()
