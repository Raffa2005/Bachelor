#!/usr/bin/env python3
"""Interactive CLI for the double-move variant lc0.

Maintains the move history so you only type one move per turn. Sends a
single lc0 subprocess via UCI. Ability moves use the `+` suffix on the
half-1 move; the underlying lc0 supports this directly (see
src/chess/board.cc::ParseMove and src/chess/types.h::Move::ToString).

Run:
    python3 tools/dm_cli.py --weights <path> [--nodes 800]
optionally:
    --fen "<FEN with optional 7th-field ability state>"

Then type one of:
    e2e4                  -> you play e2-e4 (normal)
    e2e4+ g1f3            -> you play an ability burn (half-1 + half-2)
    go [N]                -> engine's turn; if N omitted, use the default
                            If engine plays an ability move, half-2 is
                            automatically searched and appended too.
    pos startpos          -> reset to start position
    pos fen <FEN>         -> reset to FEN (FEN may include the 7th
                            ability field: W / B / m / - / combos like -B Wm)
    show                  -> print the current move history
    eval [N]              -> dump verbose stats (priors + visits) at the
                            current position with N nodes (default = current)
    nodes N               -> change the default node count
    smartprune X          -> set SmartPruningFactor (0 disables early stop)
    undo                  -> drop the last move from history
    history               -> alias for `show`
    forget / newgame      -> clear MCTS tree + NN cache (ucinewgame).
                             Useful after switching positions so verbose
                             dumps don't show stale cached V values.
    selfplay [N [T [NOISE]]]
                          -> spawn `lc0 selfplay` for ONE game from the
                             current position. Uses the same selfplay code
                             path as training runs (handles 3-fold rep,
                             50-move rule, mid-double state natively).
                             N = nodes/move (default=default_nodes).
                             T = temperature (default 0 = deterministic).
                             NOISE = dirichlet epsilon (default 0).
                             soft-adjudicate-rate is forced to 0 so the
                             game plays to natural termination.
                             Does NOT mutate the CLI's move history.
    quit / exit / q

Engine output is printed inline. Engine responses are appended to the
history exactly as the engine returned them, so the next `go` resumes
cleanly.
"""

import argparse
from collections import deque
import glob
import os
import re
import shlex
import subprocess
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LC0 = os.path.join(REPO_ROOT, "build", "lc0", "blas", "release", "lc0")
RUNS_ROOT = os.environ.get("DM_RUNS_ROOT", os.path.join(REPO_ROOT, "runs"))
MAX_ENGINE_OUTPUT_LINES = 16384
UCI_INFO_NODES_RE = re.compile(r"(?:^|\s)nodes\s+(\d+)(?=\s|$)")
VERBOSE_ROOT_RE = re.compile(r"^info string node(?:\s|$)")


class Lc0PositionError(ValueError):
    """The engine rejected a FEN or move history."""


class UciInfoSnapshot(str):
    """Last UCI info line plus every MultiPV line from the same update."""

    def __new__(cls, value, lines=()):
        instance = super().__new__(cls, value)
        instance.lines = tuple(lines) or (str(value),)
        return instance


class _OutputBuffer:
    """Thread-safe, bounded sequence of lc0 output lines."""

    def __init__(self, max_lines=MAX_ENGINE_OUTPUT_LINES):
        if max_lines < 1:
            raise ValueError("max_lines must be positive")
        self.max_lines = max_lines
        self._lines = deque(maxlen=max_lines)
        self._next_sequence = 0
        self._closed = False
        self._condition = threading.Condition()

    def append(self, line):
        with self._condition:
            sequence = self._next_sequence
            self._next_sequence += 1
            self._lines.append((sequence, line))
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def mark(self):
        with self._condition:
            return self._next_sequence

    def tail(self, count):
        with self._condition:
            return [line for _sequence, line in list(self._lines)[-count:]]

    def read(self, start, end=None, require_full=False):
        """Return retained lines in ``[start, end]``.

        The buffer is intentionally bounded. Callers that need to observe a
        terminator use :meth:`wait_for`, which advances its scan cursor while
        output arrives and therefore cannot miss the terminator merely because
        older diagnostic lines were evicted.
        """
        with self._condition:
            if (
                require_full
                and self._lines
                and start < self._lines[0][0]
            ):
                raise RuntimeError(
                    "lc0 output buffer overrun before reply was consumed"
                )
            return [
                line
                for sequence, line in self._lines
                if sequence >= start and (end is None or sequence <= end)
            ]

    def wait_for(self, predicate, start, timeout):
        deadline = time.monotonic() + timeout
        cursor = start
        with self._condition:
            while True:
                if self._lines and cursor < self._lines[0][0]:
                    raise RuntimeError(
                        "lc0 output buffer overrun while waiting for a reply"
                    )
                for sequence, line in self._lines:
                    if sequence < cursor:
                        continue
                    cursor = sequence + 1
                    if predicate(line):
                        return sequence, line
                if self._closed:
                    raise RuntimeError("lc0 closed its output stream")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self._condition.wait(remaining)

    def __len__(self):
        with self._condition:
            return len(self._lines)


def discover_latest_weights():
    """Find the most-recently-modified net-NNNN.pb.gz under any run dir.

    Returns the absolute path or None if no exports exist. Useful as a
    default for --weights so the CLI Just Works on the latest checkpoint.
    """
    pattern = os.path.join(RUNS_ROOT, "*", "exported_models", "net-*.pb.gz")
    candidates = glob.glob(pattern)
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def load_module_hint():
    print(
        "Check that the selected engine's shared libraries are installed "
        "and available in the dynamic linker's search path. "
        "See docs/self-hosting.md for backend setup.",
        file=sys.stderr,
    )


def _lc0_uci_options(smart_pruning_factor=0.0, opts=None):
    """Return the UCI options shared by analysis and public play."""
    defaults = {
        "HistoryFill": "repeat_current",
        "VerboseMoveStats": "false",
        "CPuct": "1.2",
        "PolicyTemperature": "1.0",
        "FpuValue": "0.0",
        "StickyEndgames": "true",
        "TwoFoldDraws": "false",
        "MaxCollisionVisits": "1",
        "MaxCollisionEvents": "1",
        "OutOfOrderEval": "false",
        "DisableAbilityEdges": "false",
        "SmartPruningFactor": str(smart_pruning_factor),
    }
    if opts:
        defaults.update(opts)
    # Match stock lc0: `go nodes N` targets N cumulative visits at the
    # retained root. The website exposes N as a visit budget, not as extra
    # work to add on every request.
    defaults["NodesAsPlayouts"] = "false"
    return defaults


class Lc0:
    """Thin synchronous wrapper around a single lc0 UCI process."""

    def __init__(self, lc0_path, weights, backend="cuda-fp16",
                 minibatch_size=32, smart_pruning_factor=0.0,
                 opts=None, launch_with_modules=False, fallback_weights=None):
        threads = int(os.environ.get("DM_ENGINE_THREADS", "1"))
        if threads < 1:
            raise ValueError("DM_ENGINE_THREADS must be a positive integer")
        cmd = [
            lc0_path,
            f"--weights={weights}",
            f"--backend={backend}",
            f"--threads={threads}",
            f"--minibatch-size={minibatch_size}",
            "--noise-epsilon=0",
        ]
        if os.environ.get("DM_BACKEND_OPTIONS"):
            cmd.append(f"--backend-opts={os.environ['DM_BACKEND_OPTIONS']}")
        popen_cmd = cmd
        if launch_with_modules:
            modules = shlex.split(os.environ.get("DM_ENGINE_MODULES", ""))
            if not modules:
                raise ValueError("Set DM_ENGINE_MODULES when enabling module loading")
            module_bootstrap = (
                "if [ -n \"${LMOD_PKG:-}\" ] && "
                "[ -f \"$LMOD_PKG/init/bash\" ]; then "
                "source \"$LMOD_PKG/init/bash\"; "
                "elif [ -f /etc/profile.d/modules.sh ]; then "
                "source /etc/profile.d/modules.sh; "
                "fi; "
                "if ! module load " + shlex.join(modules) + " >/dev/null 2>&1; then "
                "echo 'ERROR: failed to load the requested engine modules' >&2; "
                "exit 1; "
                "fi; "
                "exec \"$@\""
            )
            popen_cmd = ["bash", "-lc", module_bootstrap, "lc0-module-launch", *cmd]
        self.proc = subprocess.Popen(
            popen_cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        self._output = _OutputBuffer()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._lock = threading.RLock()
        mark = self._output.mark()
        self._send("uci")
        uci_sequence, _ = self._wait_for("uciok", timeout=30, after=mark)
        effective_fallback = (fallback_weights if fallback_weights is not None
                              else (opts or {}).get("FallbackWeights"))
        if effective_fallback:
            advertised = self._output.read(mark, uci_sequence, require_full=True)
            if not any(line.startswith("option name FallbackWeights type string")
                       for line in advertised):
                self.quit()
                raise RuntimeError("lc0 does not support UCI FallbackWeights; "
                                   "refusing an unrouted opening search")
        if fallback_weights is not None:
            opts = dict(opts or {}, FallbackWeights=str(fallback_weights))
        for k, v in _lc0_uci_options(
            smart_pruning_factor=smart_pruning_factor,
            opts=opts,
        ).items():
            self._send(f"setoption name {k} value {v}")
        mark = self._output.mark()
        self._send("isready")
        self._wait_for("readyok", timeout=30, after=mark)
        self._send("ucinewgame")

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                self._output.append(line.rstrip("\r\n"))
        finally:
            self._output.close()

    def _send(self, cmd):
        if not self.proc.stdin or self.proc.poll() is not None:
            tail = "\n".join(self._output.tail(8))
            detail = f"; output tail:\n{tail}" if tail else ""
            raise RuntimeError("lc0 died" + detail)
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _wait_for(self, marker, timeout=60, after=None):
        start = self._output.mark() if after is None else after
        try:
            return self._output.wait_for(
                lambda line: line == marker or line.startswith(marker + " "),
                start,
                timeout,
            )
        except TimeoutError as exc:
            tail = "\n".join(self._output.tail(12))
            detail = f"; output tail:\n{tail}" if tail else ""
            raise TimeoutError(
                f"timed out waiting for '{marker}'" + detail
            ) from exc

    def set_position(self, fen, moves):
        with self._lock:
            cmd = "position "
            if fen.lower() == "startpos":
                cmd += "startpos"
            else:
                cmd += "fen " + fen
            if moves:
                cmd += " moves " + " ".join(moves)
            mark = self._output.mark()
            self._send(cmd)
            self._send("isready")
            ready_sequence, _line = self._wait_for(
                "readyok", after=mark
            )
            output = self._output.read(
                mark, ready_sequence, require_full=True
            )
            error = next(
                (line[6:].strip() for line in output
                 if line == "error" or line.startswith("error ")),
                None,
            )
            if error is not None:
                raise Lc0PositionError(error or "lc0 rejected the position")
            diagnostic_error = next(
                (line for line in output
                 if line.startswith("info string")
                 and "ERROR" in line.upper()),
                None,
            )
            if diagnostic_error is not None:
                raise Lc0PositionError(diagnostic_error)

    def position_info(self):
        """Return authoritative variant state for the current position.

        The double-move lc0 extension emits one ``dminfo`` line for each
        field, followed by ``dminfo ready``. A partial or malformed response is
        rejected instead of being treated as a legal position.
        """
        with self._lock:
            mark = self._output.mark()
            self._send("dminfo")
            ready_sequence, _line = self._wait_for(
                "dminfo ready", after=mark
            )
            lines = self._output.read(
                mark, ready_sequence, require_full=True
            )

        values = {}
        for line in lines:
            if line == "dminfo ready":
                continue
            if line.startswith("error"):
                message = line[5:].strip()
                raise Lc0PositionError(
                    message or "lc0 rejected the current position"
                )
            if not line.startswith("dminfo "):
                continue
            payload = line[len("dminfo "):]
            field, separator, value = payload.partition(" ")
            if field not in {"fen", "turn", "ability", "result", "legal"}:
                raise RuntimeError(f"unexpected dminfo field: {field!r}")
            if field in values:
                raise RuntimeError(f"duplicate dminfo field: {field}")
            values[field] = value if separator else ""

        missing = {"fen", "turn", "ability", "result", "legal"} - set(values)
        if missing:
            raise RuntimeError(
                "incomplete dminfo response; missing " + ", ".join(sorted(missing))
            )
        if not values["fen"]:
            raise RuntimeError("dminfo returned an empty FEN")
        if values["turn"] not in ("w", "b"):
            raise RuntimeError("dminfo returned an invalid turn")
        ability_parts = values["ability"].split()
        if (
            len(ability_parts) != 3
            or any(part not in ("0", "1") for part in ability_parts)
        ):
            raise RuntimeError("dminfo returned invalid ability state")
        if values["result"] not in ("ongoing", "white", "black", "draw"):
            raise RuntimeError("dminfo returned an invalid result")
        legal = values["legal"].split()
        invalid_move = next(
            (move for move in legal if not MOVE_RE.fullmatch(move)),
            None,
        )
        if invalid_move is not None:
            raise RuntimeError(
                f"dminfo returned an invalid legal move: {invalid_move!r}"
            )
        return {
            "fen": values["fen"],
            "turn": values["turn"],
            "ability": {
                "white": ability_parts[0] == "1",
                "black": ability_parts[1] == "1",
                "mid": ability_parts[2] == "1",
            },
            "result": values["result"],
            "legal": legal,
        }

    def set_option(self, name, value):
        with self._lock:
            self._send(f"setoption name {name} value {value}")

    def press_option(self, name):
        """Press a UCI button option such as ClearTree."""
        with self._lock:
            self._send(f"setoption name {name}")

    def go_nodes(self, n):
        """Run search with N nodes; return (bestmove, ponder, pv, score)."""
        with self._lock:
            mark = self._output.mark()
            self._send(f"go nodes {n}")
            sequence, bestmove_line = self._wait_for(
                "bestmove", timeout=600, after=mark
            )
            captured = self._output.read(
                mark, sequence, require_full=True
            )
        parts = bestmove_line.split()
        bestmove = parts[1] if len(parts) >= 2 else None
        ponder = (
            parts[3]
            if len(parts) >= 4 and parts[2] == "ponder"
            else None
        )
        last_info = next(
            (line for line in reversed(captured)
             if line.startswith("info") and " pv " in line),
            None,
        )
        return bestmove, ponder, last_info

    def verbose_stats(self, n):
        """Run a search with VerboseMoveStats=on and return the final dump."""
        with self._lock:
            self._send("setoption name VerboseMoveStats value true")
            try:
                mark = self._output.mark()
                self._send(f"go nodes {n}")
                sequence, bestmove_line = self._wait_for(
                    "bestmove", timeout=600, after=mark
                )
                captured = self._output.read(
                    mark, sequence, require_full=True
                )
            except Exception:
                try:
                    self._send(
                        "setoption name VerboseMoveStats value false"
                    )
                except Exception:
                    pass
                raise
            else:
                self._send("setoption name VerboseMoveStats value false")
        # Collect the final block of "info string" lines just before bestmove.
        idx = next(
            i for i, line in enumerate(captured)
            if line.startswith("bestmove")
        )
        block = []
        i = idx - 1
        while i >= 0:
            if captured[i].startswith("info string "):
                block.append(captured[i])
                i -= 1
            elif captured[i].startswith("info "):
                i -= 1
            else:
                if block:
                    break
                i -= 1
        block.reverse()
        return block, bestmove_line

    def _verbose_stats_search_locked(
        self,
        n,
        on_snapshot,
        cancel_event=None,
    ):
        """Run one verbose search while the caller owns ``self._lock``."""
        if cancel_event is not None and cancel_event.is_set():
            return [], None, None, True

        mark = self._output.mark()
        cursor = mark
        deadline = time.monotonic() + 600
        bestmove_line = None
        latest_block = []
        current_block = []
        current_info = None
        current_infos = []
        current_nodes = None
        actual_nodes = None
        stop_sent = False
        cancelled = False
        callback_error = None
        search_started = False

        try:
            self._send(f"go nodes {n}")
            search_started = True
            while bestmove_line is None:
                if (
                    cancel_event is not None
                    and cancel_event.is_set()
                    and not stop_sent
                ):
                    cancelled = True
                    self._send("stop")
                    stop_sent = True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "timed out waiting for 'bestmove'"
                    )
                wait_time = (
                    min(remaining, 0.05)
                    if cancel_event is not None and not stop_sent
                    else remaining
                )
                try:
                    sequence, line = self._output.wait_for(
                        lambda _line: True,
                        cursor,
                        wait_time,
                    )
                except TimeoutError:
                    # Poll a supersession event without waiting for lc0's
                    # next periodic UCI information update.
                    continue
                cursor = sequence + 1

                if line == "bestmove" or line.startswith("bestmove "):
                    bestmove_line = line
                    break

                if line.startswith("info ") and not line.startswith(
                    "info string"
                ):
                    nodes_match = UCI_INFO_NODES_RE.search(line)
                    if nodes_match:
                        current_info = line
                        current_infos.append(line)
                        current_nodes = int(nodes_match.group(1))
                        current_block = []
                    continue

                if (
                    current_info is None
                    or not line.startswith("info string ")
                ):
                    continue

                current_block.append(line)
                if not VERBOSE_ROOT_RE.match(line):
                    continue

                latest_block = list(current_block)
                actual_nodes = current_nodes
                if (
                    on_snapshot is not None
                    and callback_error is None
                    and not cancelled
                ):
                    try:
                        keep_searching = on_snapshot(
                            current_nodes,
                            list(latest_block),
                            UciInfoSnapshot(
                                current_info,
                                tuple(current_infos),
                            ),
                        )
                    except Exception as exc:
                        callback_error = exc
                        keep_searching = False
                    if keep_searching is False:
                        cancelled = True
                        if not stop_sent:
                            self._send("stop")
                            stop_sent = True
                current_info = None
                current_infos = []
                current_nodes = None
                current_block = []
        except Exception:
            if search_started and bestmove_line is None:
                try:
                    if not stop_sent:
                        self._send("stop")
                        stop_sent = True
                    _sequence, bestmove_line = self._output.wait_for(
                        lambda line: (
                            line == "bestmove"
                            or line.startswith("bestmove ")
                        ),
                        cursor,
                        10,
                    )
                except Exception:
                    process = getattr(self, "proc", None)
                    if process is not None:
                        try:
                            process.kill()
                        except Exception:
                            pass
            raise

        if callback_error is not None:
            raise callback_error
        return latest_block, bestmove_line, actual_nodes, cancelled

    def verbose_stats_stream(
        self,
        n,
        on_snapshot,
        multipv=1,
        cancel_event=None,
        initial_snapshot=False,
    ):
        """Stream live root/candidate visits from one absolute-node search.

        ``LogLiveStats`` makes lc0 emit a complete VerboseMoveStats table after
        each ordinary UCI info update.  A snapshot callback receives
        ``(root_nodes, verbose_lines, info_line)`` for every complete table.
        ``info_line`` remains string-compatible and exposes all lines from the
        same MultiPV update through its ``lines`` attribute.
        Returning ``False`` or setting ``cancel_event`` requests cancellation:
        lc0 is sent ``stop`` once and its reply is still drained through
        ``bestmove`` so the next UCI command cannot consume stale output.

        With ``initial_snapshot``, a cumulative one-node search first forces
        lc0 to dump the already-retained root. If that root is below the target,
        a second cumulative search continues it to ``n``. This does not add an
        extra visit budget or clear the inherited tree.

        Returns ``(final_verbose, bestmove_line, actual_nodes, cancelled)``.
        The search tree is deliberately not cleared when a stream is
        cancelled, allowing the next position in the same context to inherit
        the work completed before ``stop``.
        """
        n = int(n)
        multipv = max(1, min(int(multipv), 64))
        with self._lock:
            if multipv > 1:
                self._send(f"setoption name MultiPV value {multipv}")
                # Keep regular analysis on lc0's native WDL logistic mean.
                # The resulting UCI centipawn score is 100 * WDL_mu and
                # already includes lc0's draw-probability and edge handling.
                self._send("setoption name ScoreType value WDL_mu")
            self._send("setoption name VerboseMoveStats value true")
            self._send("setoption name LogLiveStats value true")

            try:
                targets = [1, n] if initial_snapshot and n > 1 else [n]
                result = ([], None, None, False)
                for target in targets:
                    result = self._verbose_stats_search_locked(
                        target,
                        on_snapshot,
                        cancel_event=cancel_event,
                    )
                    _block, _bestmove, actual_nodes, cancelled = result
                    if cancelled:
                        break
                    if (
                        target == 1
                        and actual_nodes is not None
                        and actual_nodes >= n
                    ):
                        break
            finally:
                try:
                    self._send("setoption name LogLiveStats value false")
                except Exception:
                    pass
                try:
                    self._send(
                        "setoption name VerboseMoveStats value false"
                    )
                except Exception:
                    pass
                if multipv > 1:
                    try:
                        self._send("setoption name MultiPV value 1")
                    except Exception:
                        pass
                    try:
                        self._send("setoption name ScoreType value WDL_mu")
                    except Exception:
                        pass

            return result

    def new_game(self):
        """Send 'ucinewgame' — clears tree and (in stock lc0) the NN cache."""
        with self._lock:
            mark = self._output.mark()
            self._send("ucinewgame")
            self._send("isready")
            self._wait_for("readyok", timeout=10, after=mark)

    def quit(self):
        try:
            with self._lock:
                self._send("quit")
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?[+]?$")


def run_native_selfplay(lc0_path, weights, backend, minibatch_size,
                        start_fen, history, visits, temperature, noise_epsilon,
                        history_fill="fen_only"):
    """Run `lc0 selfplay` for one game starting from start_fen+history.

    Writes a temporary PGN that encodes the starting position (via [FEN] tag
    if non-startpos) and the dm-variant move list as the opening, then spawns
    a fresh lc0 in selfplay mode. The same selfplay machinery used in
    training runs handles 3-fold repetition, 50-move rule, mid-double-move
    state, and result reporting natively.

    Returns (full_move_list, result_string). The move list is in UCI form,
    starting from start_fen.

    Notes:
      - soft-adjudicate-rate is forced to 0 so the game plays to natural
        termination — what you want when probing in the CLI.
      - The dm_cli's interactive lc0 process is left untouched. We spawn a
        separate selfplay subprocess and shut it down on exit.
    """
    import tempfile
    # Build PGN. lc0 accepts UCI moves inside the body, including the
    # dm-variant `+` suffix for ability-burns (see encoder_test.cc and the
    # PGN reader in src/utils/pgn.cc). Wrap as a single game with a SetUp/FEN
    # header when we have a non-startpos starting FEN.
    pgn_lines = [
        '[Event "dm_cli-selfplay"]',
        '[White "?"]',
        '[Black "?"]',
        '[Result "*"]',
    ]
    if start_fen and start_fen != "startpos":
        pgn_lines.append('[SetUp "1"]')
        pgn_lines.append(f'[FEN "{start_fen}"]')
    pgn_lines.append("")
    body = " ".join(history) + " *" if history else "*"
    pgn_lines.append(body)
    pgn_text = "\n".join(pgn_lines) + "\n"

    with tempfile.NamedTemporaryFile(
        "w", suffix=".pgn", prefix="dm_cli_selfplay_", delete=False,
    ) as fh:
        fh.write(pgn_text)
        pgn_path = fh.name

    try:
        cmd = [
            lc0_path, "selfplay",
            f"--weights={weights}",
            f"--backend={backend}",
            f"--minibatch-size={minibatch_size}",
            "--threads=1",
            "--parallelism=1",
            "--games=1",
            "--training=false",
            "--ability-mode=normal",
            f"--history-fill={history_fill}",
            f"--visits={visits}",
            f"--temperature={temperature}",
            f"--noise-epsilon={noise_epsilon}",
            "--tempdecay-moves=0",
            "--soft-adjudicate-rate=0",
            "--smart-pruning-factor=0",
            "--resign-percentage=0",
            f"--openings-pgn={pgn_path}",
            "--openings-mode=sequential",
            "--verbose-thinking=false",
        ]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
    finally:
        try: os.unlink(pgn_path)
        except OSError: pass

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-5:]
        raise RuntimeError(
            f"lc0 selfplay exited {proc.returncode}; last stderr:\n  "
            + "\n  ".join(err)
        )

    # Parse the `gameready ... moves <list>` line — emitted once per game.
    moves: list[str] = []
    result = "(unknown)"
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line.startswith("gameready "):
            continue
        # Extract result up to "moves" (everything after gameready ... result ...)
        m = re.search(r"\bmoves\s+(.*?)\s*$", line)
        if m:
            moves = m.group(1).split()
        # Pull a few useful fields for the summary string.
        result_match = re.search(r"\bresult\s+(\S+)", line)
        q_match = re.search(r"\bq\s+([-0-9.eE+]+)", line)
        d_match = re.search(r"\bd\s+([-0-9.eE+]+)", line)
        bits = []
        if result_match: bits.append(f"result={result_match.group(1)}")
        if q_match:      bits.append(f"q={q_match.group(1)}")
        if d_match:      bits.append(f"d={d_match.group(1)}")
        if bits: result = " ".join(bits)
    if not moves:
        raise RuntimeError(
            "no `gameready` line in lc0 output; stdout tail:\n  "
            + "\n  ".join(proc.stdout.strip().splitlines()[-10:])
        )
    return moves, result


def is_uci_move(token):
    return bool(MOVE_RE.match(token))


def main():
    auto_weights = discover_latest_weights()
    p = argparse.ArgumentParser(description="Interactive Double Strike Chess CLI")
    p.add_argument("--weights", default=auto_weights,
                   help="Path to net-NNNN.pb.gz. Default: most-recently-"
                        f"modified export under {RUNS_ROOT}/*/exported_models/."
                        f" Auto-discovered: {auto_weights or '(none found)'}")
    p.add_argument("--lc0", default=DEFAULT_LC0)
    p.add_argument("--nodes", type=int, default=800, help="Default node count")
    p.add_argument("--fen", default="startpos",
                   help="Starting position (FEN, or 'startpos')")
    p.add_argument("--backend", default="cuda-fp16")
    p.add_argument("--minibatch-size", type=int, default=32,
                   help="lc0 minibatch size for NN evaluation (default 32)")
    p.add_argument("--smart-pruning-factor", type=float, default=0.0,
                   help="SmartPruningFactor for UCI searches; 0 disables "
                        "early stopping (default 0)")
    args = p.parse_args()

    if not os.path.exists(args.lc0):
        print(f"ERROR: lc0 binary not found at {args.lc0}", file=sys.stderr)
        load_module_hint()
        sys.exit(1)
    if not args.weights:
        print(f"ERROR: no --weights given and none found under {RUNS_ROOT}",
              file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(args.weights):
        print(f"ERROR: weights not found at {args.weights}", file=sys.stderr)
        sys.exit(1)

    try:
        lc0 = Lc0(args.lc0, args.weights, backend=args.backend,
                  minibatch_size=args.minibatch_size,
                  smart_pruning_factor=args.smart_pruning_factor)
    except Exception as e:
        print(f"ERROR starting lc0: {e}", file=sys.stderr)
        load_module_hint()
        sys.exit(1)

    history = []           # list of UCI move strings (with optional + suffix)
    start_fen = args.fen   # 'startpos' or a FEN
    default_nodes = args.nodes

    print(f"# lc0 ready. weights={args.weights}")
    print(f"# default nodes = {default_nodes}.  Type 'help' for usage.")
    print(f"# SmartPruningFactor = {args.smart_pruning_factor}")

    def push_pos():
        lc0.set_position(start_fen, history)

    push_pos()

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        if line in ("quit", "exit", "q"):
            break
        if line in ("help", "?"):
            print(__doc__.strip())
            continue
        if line in ("show", "history"):
            mv = " ".join(history) if history else "(empty)"
            print(f"start: {start_fen}")
            print(f"moves: {mv}")
            continue
        if line == "fen":
            # Ask lc0 to dump the current FEN. lc0 doesn't have a "what FEN"
            # UCI command, so use python-chess to reconstruct it from the move
            # history. Falls back to startpos / the explicit FEN we set.
            try:
                import chess
                board = chess.Board() if start_fen == "startpos" \
                    else chess.Board(start_fen.split(" -")[0] if " -" in start_fen
                                     else start_fen.split()[0] + " " + " ".join(start_fen.split()[1:6]))
                for tok in history:
                    base = tok.rstrip("+")
                    try:
                        board.push_uci(base)
                    except Exception:
                        # Mid-double or other states python-chess can't model;
                        # bail out and tell the user.
                        raise RuntimeError(
                            f"Cannot reconstruct FEN past move {tok}: python-chess "
                            "doesn't model the variant's mid-double state."
                        )
                print(board.fen())
            except RuntimeError as e:
                print(f"# {e}")
            except ImportError:
                print("# python-chess not installed; install with `pip install --user chess`")
            continue
        if line == "undo":
            if not history:
                print("nothing to undo")
            else:
                popped = history.pop()
                push_pos()
                print(f"undone: {popped}")
            continue
        if line in ("forget", "newgame", "clear"):
            # Drop the analysis tree. ucinewgame also flushes lc0's NN cache,
            # which is what you want when you've moved to a new position and
            # don't want stale V values lingering in verbose-stats dumps.
            lc0.new_game()
            push_pos()
            print("# tree + cache cleared (ucinewgame)")
            continue
        if line.startswith("nodes "):
            try:
                default_nodes = int(line.split()[1])
                print(f"# default nodes = {default_nodes}")
            except Exception:
                print("usage: nodes N")
            continue
        if line.startswith("smartprune ") or line.startswith("smart "):
            try:
                smart_pruning_factor = float(line.split()[1])
                lc0.set_option("SmartPruningFactor", smart_pruning_factor)
                print(f"# SmartPruningFactor = {smart_pruning_factor}")
            except Exception:
                print("usage: smartprune X   (0 disables early stopping)")
            continue
        if line.startswith("pos "):
            rest = line[4:].strip()
            if rest == "startpos":
                start_fen = "startpos"; history = []
            elif rest.startswith("fen "):
                start_fen = rest[4:].strip(); history = []
            else:
                # Allow "pos <FEN-without-fen-keyword>" too.
                start_fen = rest; history = []
            push_pos()
            print(f"# now at: {start_fen}")
            continue
        if line.startswith("eval"):
            parts = line.split()
            n = int(parts[1]) if len(parts) >= 2 else default_nodes
            push_pos()
            block, best = lc0.verbose_stats(n)
            print(f"# verbose dump at {n} nodes (best={best})")
            for l in block:
                print("  " + l)
            continue
        if line.startswith("go"):
            parts = line.split()
            n = int(parts[1]) if len(parts) >= 2 else default_nodes
            push_pos()
            best, ponder, info = lc0.go_nodes(n)
            if best is None:
                print("no bestmove returned")
                continue
            history.append(best)
            extra = ""
            # If half-1 of an ability, automatically search for half-2.
            if best.endswith("+"):
                push_pos()
                best2, ponder2, info2 = lc0.go_nodes(n)
                if best2 is not None:
                    history.append(best2)
                    extra = f"  +half2= {best2}"
            print(f"engine: {best}{extra}    (ponder={ponder})")
            if info:
                # Only show the meat: score / pv tail.
                short = re.sub(r"^info ", "", info)
                print(f"  {short[:120]}{'...' if len(short) > 120 else ''}")
            continue
        if line.startswith("selfplay"):
            # Native lc0 selfplay from the CURRENT position. Spawns a fresh
            # lc0-selfplay subprocess and lets it run a single game with the
            # exact same machinery training selfplay uses (PositionHistory,
            # 3-fold-repetition, 50-move rule, soft adjudication, etc.).
            #
            #   selfplay              -> N = default_nodes/move, T=0, no noise
            #   selfplay N            -> N nodes/move
            #   selfplay N TEMP       -> N nodes/move, temperature=TEMP
            #   selfplay N TEMP NOISE -> + dirichlet noise epsilon
            parts = line.split()
            n = int(parts[1]) if len(parts) >= 2 else default_nodes
            temp = float(parts[2]) if len(parts) >= 3 else 0.0
            noise = float(parts[3]) if len(parts) >= 4 else 0.0
            try:
                moves, result = run_native_selfplay(
                    args.lc0, args.weights, args.backend, args.minibatch_size,
                    start_fen, history, visits=n, temperature=temp,
                    noise_epsilon=noise,
                )
            except RuntimeError as e:
                print(f"# selfplay failed: {e}")
                continue
            print(f"# selfplay: {result}")
            new_moves = moves[len(history):] if len(moves) >= len(history) \
                else moves
            print(f"# {len(new_moves)} new move(s): {' '.join(new_moves)}")
            print(f"# full game from start: {' '.join(moves)}")
            continue

        # Otherwise treat whitespace-separated tokens as moves to append.
        tokens = shlex.split(line)
        if all(is_uci_move(t) for t in tokens):
            for t in tokens:
                history.append(t)
            push_pos()
            mv = " ".join(history[-len(tokens):])
            print(f"# played: {mv}")
            continue

        print(f"unknown command: {line!r}.  Type 'help'.")

    lc0.quit()


if __name__ == "__main__":
    main()
