"""A tiny UCI engine for tests. Plays the first legal move (sorted by UCI) and scores
every position as +0.13 for White. Accepts any option, including Lc0's WeightsFile."""

import sys

import chess


def main() -> None:
    board = chess.Board()
    name = sys.argv[1] if len(sys.argv) > 1 else "FakeEngine"
    for raw in sys.stdin:
        line = raw.strip()
        if line == "uci":
            print(f"id name {name}")
            print("id author tests")
            print("option name Threads type spin default 1 min 1 max 64")
            print("option name Hash type spin default 16 min 1 max 1024")
            print("option name MultiPV type spin default 1 min 1 max 500")
            print("option name WeightsFile type string default <autodiscover>")
            print("uciok", flush=True)
        elif line == "isready":
            print("readyok", flush=True)
        elif line.startswith("position"):
            parts = line.split(" moves ")
            head = parts[0]
            if head.startswith("position startpos"):
                board = chess.Board()
            else:
                board = chess.Board(head[len("position fen ") :])
            if len(parts) > 1:
                for mv in parts[1].split():
                    board.push_uci(mv)
        elif line.startswith("go"):
            moves = sorted(m.uci() for m in board.legal_moves)
            if not moves:
                print("info depth 1 score mate 0")
                print("bestmove (none)", flush=True)
                continue
            pov = 13 if board.turn == chess.WHITE else -13
            print(f"info depth 1 multipv 1 score cp {pov} nodes 1 pv {moves[0]}")
            print(f"bestmove {moves[0]}", flush=True)
        elif line == "quit":
            return


if __name__ == "__main__":
    main()
