#!/usr/bin/env python3
"""Compare double-move SYCL inference with BLAS over a fixed corpus."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LC0 = ROOT / "build/lc0/sycl/release/lc0"
DEFAULT_WEIGHTS = ROOT / "models/v37-latest.pb.gz"
POLICY_OUTPUTS = 3716

CORPUS = (
    (
        "start-white-both",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "w KQkq - 0 1 WB",
    ),
    (
        "start-white-white",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "w KQkq - 0 1 W-",
    ),
    (
        "start-white-black",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "w KQkq - 0 1 -B",
    ),
    (
        "start-white-neither",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "w KQkq - 0 1 --",
    ),
    (
        "start-black-both",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "b KQkq - 0 1 WB",
    ),
    (
        "start-black-white",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "b KQkq - 0 1 W-",
    ),
    (
        "start-black-black",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "b KQkq - 0 1 -B",
    ),
    (
        "start-black-neither",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR "
        "b KQkq - 0 1 --",
    ),
    (
        "white-midmove",
        "rnbqkbnr/pppppppp/8/8/8/4P3/PPPP1PPP/RNBQKBNR "
        "w KQkq - 0 1 -Bm",
    ),
    (
        "black-midmove",
        "rnbqkbnr/pppp1ppp/4p3/8/8/4P3/PPPP1PPP/RNBQKBNR "
        "b KQkq - 0 2 W-m",
    ),
    (
        "black-midmove-neither",
        "rnbqkbnr/pppp1ppp/4p3/8/8/4PN2/PPPP1PPP/RNBQKB1R "
        "b KQkq - 0 2 --m",
    ),
    (
        "complex-black",
        "rnbqk2r/pp1pbppp/2p2n2/4B3/2P5/1P2PP2/P2P2PP/RN1QKBNR "
        "b KQkq - 0 5 -B",
    ),
    (
        "en-passant",
        "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR "
        "w KQkq e6 0 2 WB",
    ),
    (
        "promotion",
        "7k/P7/8/8/8/8/7p/7K w - - 0 1 WB",
    ),
)
BATCHES = len(CORPUS)
EXPECTED_REPORTS = 2 + 2 * BATCHES

# These gates are intentionally backend-specific. FP32 should remain very
# close to BLAS; production sycl-auto uses FP16 and accumulates more rounding
# error through the transformer.
TOLERANCES = {
    "sycl": {
        "value_atol": 2e-5,
        "value_rtol": 2e-4,
        "policy_atol": 1e-4,
        "policy_rtol": 1e-4,
        "moves_left_atol": 1e-3,
        "moves_left_rtol": 1e-4,
    },
    "sycl-auto": {
        "value_atol": 5e-2,
        "value_rtol": 1.5e-1,
        "policy_atol": 2e-1,
        "policy_rtol": 3e-2,
        "moves_left_atol": 2e-1,
        "moves_left_rtol": 3e-2,
    },
}

FLOAT_PATTERN = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
VALUE_RE = re.compile(
    rf"value: absolute: ({FLOAT_PATTERN}), relative: ({FLOAT_PATTERN})"
)
POLICY_RE = re.compile(
    rf"raw policy: absolute: ({FLOAT_PATTERN}), relative: ({FLOAT_PATTERN})"
)
MOVES_LEFT_RE = re.compile(
    rf"moves left: absolute: ({FLOAT_PATTERN}), relative: ({FLOAT_PATTERN})"
)


def backend_options(backend, mode):
    options = [
        f"mode={mode}",
        "freq=1.0",
        f"raw_policy_outputs={POLICY_OUTPUTS}",
    ]
    if mode == "check":
        options.extend(
            f"{name}={value}" for name, value in TOLERANCES[backend].items()
        )
    options.extend((f"(backend={backend},max_batch=3)", "(backend=blas)"))
    return ",".join(options)


def make_command(lc0, weights, backend, mode, fen_file):
    return [
        str(lc0),
        "backendbench",
        f"--weights={weights}",
        "--backend=check",
        f"--backend-opts={backend_options(backend, mode)}",
        "--threads=1",
        f"--batches={BATCHES}",
        "--start-batch-size=1",
        "--max-batch-size=3",
        "--batch-step=2",
        f"--fen-file={fen_file}",
    ]


def run_backend(lc0, weights, backend, mode, fen_file):
    env = os.environ.copy()
    env["OPENBLAS_NUM_THREADS"] = "1"
    result = subprocess.run(
        make_command(lc0, weights, backend, mode, fen_file),
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=300,
        check=False,
    )
    output = result.stdout
    required = (
        f"Working backend set to {backend}.",
        "Reference backend set to blas.",
        f"Comparing {POLICY_OUTPUTS} raw policy outputs per sample.",
        f"Loaded {len(CORPUS)} benchmark positions",
    )
    problems = []
    if result.returncode != 0:
        problems.append(f"process exited {result.returncode}")
    for marker in required:
        if marker not in output:
            problems.append(f"missing output marker: {marker}")
    if "*** ERROR" in output:
        problems.append("numerical comparison failed")

    report_marker = (
        "Check passed for a batch of"
        if mode == "check"
        else "maximum error for a batch of"
    )
    reports = output.count(report_marker)
    if reports != EXPECTED_REPORTS:
        problems.append(
            f"expected {EXPECTED_REPORTS} comparison reports, got {reports}"
        )
    if problems:
        detail = "\n".join(f"- {problem}" for problem in problems)
        raise RuntimeError(f"{backend} failed:\n{detail}\n\n{output}")

    if mode == "display":
        value_errors = [
            (float(absolute), float(relative))
            for absolute, relative in VALUE_RE.findall(output)
        ]
        policy_errors = [
            (float(absolute), float(relative))
            for absolute, relative in POLICY_RE.findall(output)
        ]
        moves_left_errors = [
            (float(absolute), float(relative))
            for absolute, relative in MOVES_LEFT_RE.findall(output)
        ]
        if len(value_errors) != EXPECTED_REPORTS:
            raise RuntimeError(f"{backend}: could not parse every value report")
        if len(policy_errors) != EXPECTED_REPORTS:
            raise RuntimeError(f"{backend}: could not parse every policy report")
        if len(moves_left_errors) != EXPECTED_REPORTS:
            raise RuntimeError(
                f"{backend}: could not parse every moves-left report"
            )
        return (
            max(error[0] for error in value_errors),
            max(error[1] for error in value_errors),
            max(error[0] for error in policy_errors),
            max(error[1] for error in policy_errors),
            max(error[0] for error in moves_left_errors),
            max(error[1] for error in moves_left_errors),
        )
    return None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lc0", type=Path, default=DEFAULT_LC0)
    parser.add_argument("--weights", type=Path, default=DEFAULT_WEIGHTS)
    parser.add_argument(
        "--backend",
        choices=("sycl", "sycl-auto", "all"),
        default="all",
        help="SYCL precision path to validate (default: both)",
    )
    parser.add_argument(
        "--mode",
        choices=("check", "display"),
        default="check",
        help="enforce committed tolerances or print observed maxima",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not args.lc0.is_file() or not os.access(args.lc0, os.X_OK):
        raise RuntimeError(f"SYCL lc0 executable not found: {args.lc0}")
    if not args.weights.is_file():
        raise RuntimeError(f"weights file not found: {args.weights}")

    backends = ("sycl", "sycl-auto") if args.backend == "all" else (args.backend,)
    overall = {backend: [0.0] * 6 for backend in backends}
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="double-move-sycl-", suffix=".fens"
    ) as fen_file:
        for _, fen in CORPUS:
            fen_file.write(f"{fen}\n")
        fen_file.flush()

        for backend in backends:
            metrics = run_backend(
                args.lc0, args.weights, backend, args.mode, fen_file.name
            )
            if metrics is not None:
                overall[backend] = [
                    max(previous, current)
                    for previous, current in zip(overall[backend], metrics)
                ]
            print(f"passed: {backend}", flush=True)

    if args.mode == "display":
        for backend, metrics in overall.items():
            print(
                f"{backend}: value abs={metrics[0]:.3g}, "
                f"value rel={metrics[1]:.3g}, policy abs={metrics[2]:.3g}, "
                f"policy rel={metrics[3]:.3g}, moves-left abs={metrics[4]:.3g}, "
                f"moves-left rel={metrics[5]:.3g}"
            )
    else:
        print(
            f"SYCL numerical validation passed for {len(CORPUS)} positions "
            f"and {len(backends)} precision path(s)."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
