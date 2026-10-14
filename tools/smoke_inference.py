#!/usr/bin/env python3
"""Perform a structural UCI smoke test without judging chess quality."""

import argparse
import os
import re
import sys

try:
    from .dm_cli import Lc0
except ImportError:
    from dm_cli import Lc0


MOVE_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?\+?$")
ROOT_VISITS_RE = re.compile(r"\bN:\s*(\d+)\b")
VERBOSE_MOVE_RE = re.compile(
    r"^([a-h][1-8][a-h][1-8][qrbn]?\+?)(?:\[AB\])?(?:\s|$)"
)


def _bestmove_from_line(line):
    parts = str(line or "").split()
    if len(parts) >= 2 and parts[0] == "bestmove":
        return parts[1]
    return None


def _root_visits(block):
    for line in reversed(block):
        payload = str(line).removeprefix("info string ").strip()
        if not payload.startswith("node "):
            continue
        match = ROOT_VISITS_RE.search(payload)
        if match:
            return int(match.group(1))
    raise RuntimeError("lc0 verbose statistics did not report root visits")


def _move_visits(block, move):
    for line in block:
        payload = str(line).removeprefix("info string ").strip()
        move_match = VERBOSE_MOVE_RE.match(payload)
        if not move_match or move_match.group(1) != move:
            continue
        visits_match = ROOT_VISITS_RE.search(payload)
        if visits_match:
            return int(visits_match.group(1))
    raise RuntimeError(
        f"lc0 verbose statistics did not report visits for {move!r}"
    )


def _policy_value_signature(block):
    priors = {}
    value = None
    for line in block:
        payload = str(line).removeprefix("info string ").strip()
        move = VERBOSE_MOVE_RE.match(payload)
        if move:
            prior = re.search(r"\bP:\s*([0-9.]+)%", payload)
            if prior:
                priors[move.group(1)] = float(prior.group(1))
        elif payload.startswith("node "):
            root = re.search(r"\bQ:\s*([-+0-9.]+)", payload)
            if root:
                value = float(root.group(1))
    if not priors or value is None:
        raise RuntimeError("lc0 did not report root policy and value")
    return value, priors


def _check_fallback_signature(paired, standalone):
    paired_value, paired_policy = paired
    value, policy = standalone
    # Allow the rounding of verbose statistics and small backend differences.
    if (paired_policy.keys() != policy.keys()
            or abs(paired_value - value) > 0.0001
            or any(abs(paired_policy[move] - prior) > 0.02
                   for move, prior in policy.items())):
        raise RuntimeError("MoE fallback policy/value differ from standalone BT3")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lc0", required=True)
    parser.add_argument("--weights", required=True)
    parser.add_argument("--fallback-weights")
    parser.add_argument("--backend", default="blas")
    parser.add_argument("--minibatch-size", type=int, default=1)
    parser.add_argument("--nodes", type=int, default=1)
    parser.add_argument(
        "--verify-tree-reuse",
        action="store_true",
        help=(
            "repeat and advance a fixed-node search, verifying that retained "
            "visits count toward the requested root total"
        ),
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(args.lc0) or not os.access(args.lc0, os.X_OK):
        parser.error(f"lc0 executable not found: {args.lc0}")
    if not os.path.isfile(args.weights):
        parser.error(f"weights not found: {args.weights}")
    if args.fallback_weights and not os.path.isfile(args.fallback_weights):
        parser.error(f"fallback weights not found: {args.fallback_weights}")
    if args.nodes < 1:
        parser.error("--nodes must be at least 1")

    fallback_checks = []
    engine = Lc0(
        args.lc0,
        args.weights,
        backend=args.backend,
        minibatch_size=args.minibatch_size,
        launch_with_modules=False,
        fallback_weights=args.fallback_weights,
    )
    try:
        engine.set_position("startpos", [])
        if args.verify_tree_reuse:
            # lc0 stops search workers asynchronously. A small requested
            # target can therefore overshoot by part of a minibatch even when
            # NodesAsPlayouts=false. Use a guard target large enough to
            # distinguish that bounded overshoot from adding a complete new
            # visit budget, which is the regression this check is meant to
            # catch.
            check_nodes = max(args.nodes, 4 * args.minibatch_size)
            first_block, first_line = engine.verbose_stats(check_nodes)
            first_visits = _root_visits(first_block)
            bestmove = _bestmove_from_line(first_line)

            engine.set_position("startpos", [])
            repeated_block, repeated_line = engine.verbose_stats(check_nodes)
            repeated_visits = _root_visits(repeated_block)
            repeated_bestmove = _bestmove_from_line(repeated_line)
            if (
                not repeated_bestmove
                or not MOVE_RE.fullmatch(repeated_bestmove)
            ):
                raise RuntimeError(
                    "lc0 returned an invalid repeated-search bestmove token: "
                    f"{repeated_bestmove!r}"
                )
            repeated_added = repeated_visits - first_visits
            if repeated_added >= check_nodes:
                raise RuntimeError(
                    "repeated search added a complete fresh visit budget "
                    f"({first_visits} -> {repeated_visits}; "
                    f"budget {check_nodes})"
                )

            inherited_visits = _move_visits(
                repeated_block, repeated_bestmove
            )
            if inherited_visits < 1:
                raise RuntimeError(
                    "selected child has zero retained visits; rerun the "
                    "tree-reuse check with a larger --nodes value"
                )
            engine.set_position("startpos", [repeated_bestmove])
            # A target of one cannot manufacture the selected child's retained
            # visit count from an empty tree. Promotion can legitimately add a
            # few visits while lc0 reopens a converted terminal child.
            advanced_block, _advanced_line = engine.verbose_stats(1)
            advanced_visits = _root_visits(advanced_block)
            if advanced_visits < inherited_visits:
                raise RuntimeError(
                    "advancing discarded visits from the selected "
                    f"child visits ({inherited_visits} -> {advanced_visits})"
                )
            engine.set_position("startpos", [repeated_bestmove])
            stable_block, _stable_line = engine.verbose_stats(advanced_visits)
            stable_visits = _root_visits(stable_block)
            advanced_added = stable_visits - advanced_visits
            if advanced_added >= check_nodes:
                raise RuntimeError(
                    "repeated advanced-root search added a complete fresh "
                    f"visit budget ({advanced_visits} -> {stable_visits}; "
                    f"budget {check_nodes})"
                )
            print(
                "Cumulative tree-reuse check passed "
                f"(guard budget {check_nodes}; root "
                f"{first_visits} -> {repeated_visits}; child/root "
                f"{inherited_visits} -> {advanced_visits} -> "
                f"{stable_visits})."
            )
        else:
            bestmove, _ponder, _info = engine.go_nodes(args.nodes)
        if not bestmove or not MOVE_RE.fullmatch(bestmove):
            raise RuntimeError(
                f"lc0 returned an invalid bestmove token: {bestmove!r}"
            )
        if args.fallback_weights:
            for moves in (["e2e4+", "d2d4", "e7e5+"],
                          ["e2e4+", "d2d4", "e7e5+", "d7d5"]):
                engine.new_game()
                engine.set_position("startpos", moves)
                position = engine.position_info()
                block, best_line = engine.verbose_stats(1)
                move = _bestmove_from_line(best_line)
                if move not in position["legal"]:
                    raise RuntimeError(f"Fallback returned illegal move: {move!r}")
                fallback_checks.append((moves, _policy_value_signature(block)))
    finally:
        engine.quit()

    if fallback_checks:
        standard = Lc0(
            args.lc0, args.fallback_weights, backend=args.backend,
            minibatch_size=args.minibatch_size, launch_with_modules=False,
        )
        try:
            for moves, signature in fallback_checks:
                standard.new_game()
                standard.set_position("startpos", moves)
                block, _best = standard.verbose_stats(1)
                _check_fallback_signature(signature, _policy_value_signature(block))
        finally:
            standard.quit()
        print("MoE pair searched all phases; mid-double and completed-turn outputs match standalone BT3.")

    print(
        f"Inference smoke test passed ({args.backend}, "
        f"{args.nodes} node{'s' if args.nodes != 1 else ''})."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
