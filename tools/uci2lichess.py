#!/usr/bin/env python3
"""UCI move string → SAN, final FEN, Lichess import link, PGN, JSON.

Usage:
  python3 uci2lichess.py "e2e4 e7e5 g1f3 ..."
  python3 uci2lichess.py --file selfplay-iter99.stdout.log
  python3 uci2lichess.py --auto                    # picks latest game from latest log
  python3 uci2lichess.py --auto --json             # outputs JSON + writes latest_game.json
  python3 uci2lichess.py --auto --run-dir runs/selfplay
"""

import argparse, json, os, re, sys, urllib.parse, urllib.request

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_OUT = os.path.join(TOOLS_DIR, "latest_game.json")


def import_to_lichess(pgn):
    """Import PGN to Lichess via API (no auth needed, rate-limited).
    Returns the Lichess game URL, or None on failure."""
    try:
        data = urllib.parse.urlencode({"pgn": pgn}).encode()
        req = urllib.request.Request(
            "https://lichess.org/api/import",
            data=data,
            headers={"Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            return result.get("url")
    except Exception as e:
        return None


def parse_uci(uci_str):
    """Convert UCI moves to (san_list, final_fen, move_count, double_flags)."""
    import chess
    board = chess.Board()
    san_list = []
    double_flags = []
    count = 0
    last_color = None
    for uci in uci_str.strip().split():
        try:
            move = chess.Move.from_uci(uci)
        except ValueError:
            san_list.append(f"[bad:{uci}]")
            double_flags.append(False)
            continue
        if move in board.legal_moves:
            san_list.append(board.san(move))
            board.push(move)
            count += 1
            current_color = 'w' if board.turn == chess.BLACK else 'b'
            is_double = (current_color == last_color and count > 1)
            double_flags.append(is_double)
            # Retroactively mark the previous move too — the "ability
            # activation" belongs to the first move of the pair.
            if is_double and len(double_flags) >= 2:
                double_flags[-2] = True
            last_color = current_color
        else:
            try:
                # Determine the actual moving color and piece type from the
                # source square BEFORE pushing (board.push flips the turn
                # unconditionally and clears the source square).
                piece = board.piece_at(move.from_square)
                actual_color = 'w' if piece and piece.color == chess.WHITE else 'b'
                piece_type = piece.piece_type if piece else None
                target = board.piece_at(move.to_square)
                board.push(move)
                count += 1
                # Detect castling: king moves two squares or onto own rook
                san = _uci_to_san_castling(piece_type, actual_color, target, move, uci)
                san_list.append(san)
                # Use the pre-push color for double-move detection
                is_double = (actual_color == last_color and count > 1)
                double_flags.append(is_double)
                if is_double and len(double_flags) >= 2:
                    double_flags[-2] = True
                last_color = actual_color
            except Exception:
                san_list.append(f"[illegal:{uci}]")
                double_flags.append(False)
    return san_list, board.fen(), count, double_flags


def _uci_to_san_castling(piece_type, color, target, move, uci):
    """Return SAN for a move that wasn't in legal_moves, handling castling.
    
    All arguments reflect the state *before* the move was pushed."""
    if piece_type == chess.KING:
        from_f = chess.square_file(move.from_square)
        to_f = chess.square_file(move.to_square)
        from_r = chess.square_rank(move.from_square)
        to_r = chess.square_rank(move.to_square)
        # Standard UCI castling: king moves two files horizontally
        if from_r == to_r and abs(to_f - from_f) == 2:
            return 'O-O' if to_f > from_f else 'O-O-O'
        # LC0-style: king captures own rook (e1h1, e1a1, e8h8, e8a8)
        if target and target.color == (chess.WHITE if color == 'w' else chess.BLACK) \
                and target.piece_type == chess.ROOK:
            return 'O-O' if to_f > from_f else 'O-O-O'
    return uci  # fallback: raw UCI


def build_pgn(uci_str, san_list, double_flags, white="Player1", black="Player2", result="*"):
    """Build a clean PGN with {double-move} comments that Lichess can parse."""
    # Filter out invalid entries
    valid = [(s, d) for s, d in zip(san_list, double_flags) if not s.startswith('[')]

    tokens = []
    move_num = 1
    is_white_turn = True  # game starts with white

    for san, is_double in valid:
        if is_white_turn:
            token = f"{move_num}."
            is_white_turn = False
            if not is_double:
                move_num += 1  # advance move number after black's move
        else:
            token = ""
            is_white_turn = True

        if is_double:
            token += f" {{double-move}} {san}"
        else:
            token += f" {san}" if token else san

        tokens.append(token)

    # Wrap at 78 chars
    lines, line = [], ""
    for t in tokens:
        if line and len(line) + len(t) + 1 > 78:
            lines.append(line.rstrip())
            line = t + " "
        else:
            line += t + " "
    if line.strip():
        lines.append(line.rstrip())

    hdr = f'[Event "Selfplay"]\n[White "{white}"]\n[Black "{black}"]\n[Result "{result}"]\n'
    return hdr + "\n".join(lines) + f" {result}"


def main():
    p = argparse.ArgumentParser(description="UCI moves → SAN, FEN, Lichess, PGN, JSON")
    p.add_argument("moves", nargs="*", help="UCI moves string")
    p.add_argument("--file", "-f", help="Extract moves from selfplay log")
    p.add_argument("--auto", "-a", action="store_true", help="Auto-find latest completed game")
    p.add_argument("--run-dir", default="runs/selfplay")
    p.add_argument("--json", "-j", action="store_true", help="Output JSON + write latest_game.json")
    p.add_argument("--json-out", default=JSON_OUT, help="JSON output path")
    args = p.parse_args()

    uci_str = None
    source_file = None
    if args.auto:
        log_dir = os.path.join(args.run_dir, "logs")
        dirs = sorted([d for d in os.listdir(log_dir)
                       if d.startswith("slurm-") and os.path.isdir(os.path.join(log_dir, d))],
                      key=lambda x: os.path.getmtime(os.path.join(log_dir, x)), reverse=True)
        if not dirs:
            print("No log dirs", file=sys.stderr); sys.exit(1)
        latest = os.path.join(log_dir, dirs[0])
        sp_logs = sorted([f for f in os.listdir(latest) if f.startswith("selfplay-") and f.endswith(".log")],
                         key=lambda x: os.path.getmtime(os.path.join(latest, x)), reverse=True)
        # Prefer completed logs (contain "tournamentstatus final")
        completed = []
        for sp in sp_logs:
            sp_path = os.path.join(latest, sp)
            try:
                with open(sp_path) as f:
                    if 'tournamentstatus final' in f.read():
                        completed.append(sp)
            except Exception:
                pass
        if completed:
            args.file = os.path.join(latest, completed[0])
        elif sp_logs:
            args.file = os.path.join(latest, sp_logs[0])
        else:
            print("No selfplay logs", file=sys.stderr); sys.exit(1)
        source_file = args.file
        print(f"[auto] {args.file}", file=sys.stderr)

    if args.file:
        with open(args.file) as f:
            content = f.read()
        source_file = args.file
        matches = re.findall(r'moves\s+((?:[a-h][1-8][a-h][1-8][a-z]?\s*)+)', content)
        if matches:
            uci_str = matches[-1].strip()
            print(f"[info] {len(matches)} games, last: {len(uci_str.split())} moves", file=sys.stderr)
        else:
            print("No UCI moves found", file=sys.stderr); sys.exit(1)
    elif args.moves:
        uci_str = " ".join(args.moves)
    else:
        uci_str = sys.stdin.read().strip()

    uci_str = re.sub(r'^.*?moves\s+', '', uci_str).strip()
    if not uci_str:
        print("No moves", file=sys.stderr); sys.exit(1)

    san_list, fen, count, double_flags = parse_uci(uci_str)
    pgn = build_pgn(uci_str, san_list, double_flags)

    # Lichess: try API import first, fall back to encoded PGN URL
    lichess_api_url = import_to_lichess(pgn)
    if lichess_api_url:
        lichess_import_url = lichess_api_url
        lichess_method = "api"
    else:
        lichess_import_url = (
            "https://lichess.org/analysis/pgn/"
            + urllib.parse.quote(pgn, safe='')
        )
        lichess_method = "fallback"

    if args.json:
        data = {
            "source": source_file,
            "uci": uci_str,
            "san": san_list,
            "double_flags": double_flags,
            "fen": fen,
            "pgn": pgn,
            "lichess_import": lichess_import_url,
            "lichess_import_method": lichess_method,
            "lichess_fen": f"https://lichess.org/analysis/{urllib.parse.quote(fen, safe='')}",
            "move_count": count,
        }
        with open(args.json_out, "w") as f:
            json.dump(data, f, indent=2)
        print(json.dumps(data, indent=2))
    else:
        print(f"UCI moves: {count}, sample: {uci_str[:150]}{'...' if len(uci_str)>150 else ''}")
        print()
        print("=== SAN ===")
        for i, s in enumerate(san_list):
            dm = " [DOUBLE]" if double_flags[i] else ""
            print(f"  {i+1:3d}. {s}{dm}")
        print()
        print("=== Final FEN ===")
        print(fen)
        print()
        print("=== Lichess — Open this link to see the game immediately ===")
        print(f"{lichess_import_url}  (via {lichess_method})")
        print()
        print("=== PGN ===")
        print(pgn)


if __name__ == "__main__":
    main()
