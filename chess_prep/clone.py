"""A bot that plays like the scouted player.

How it picks a move, in order:
  1. Opening book: the player's own move sequences, sampled by how often they played them.
  2. Known position (any move order): the player's moves from that position.
     With Maia available, 70% the player's move, 30% Maia's.
  3. Unknown position: Maia (a neural net trained to play like humans of a given
     rating) if Lc0 + Maia weights are installed, otherwise Stockfish at reduced
     strength near the player's rating, otherwise a random legal move.
Recent games (last 6 months of the data) count three times as much.
"""

from __future__ import annotations

import contextlib
import random
import sqlite3
import statistics
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.engine

from chess_prep import db
from chess_prep.engines import EngineSet, pick_maia
from chess_prep.pgnparse import position_key

BOOK_PLIES = 24
HISTORY_WEIGHT = 0.70
RECENT_SECONDS = 183 * 24 * 3600
FALLBACK_DEPTH = 8


@dataclass
class MoveInfo:
    source: str  # book / history / maia / stockfish / random
    confidence: float = 0.0
    think_time: float = 0.0
    detail: str = ""


@dataclass
class OpponentBook:
    trees: dict[str, dict] = field(default_factory=lambda: {"white": {}, "black": {}})
    positions: dict[str, Counter] = field(default_factory=dict)
    think: dict[str, float] = field(default_factory=dict)
    rating: int | None = None
    games: int = 0

    @classmethod
    def from_db(cls, db_path: Path) -> OpponentBook:
        book = cls()
        conn = db.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            games = conn.execute("SELECT id, color, end_time, player_rating, increment FROM games").fetchall()
            if not games:
                raise ValueError("No games stored for this player yet.")
            book.games = len(games)
            newest = max((g["end_time"] or 0) for g in games)
            ratings = [
                g["player_rating"] for g in sorted(games, key=lambda g: g["end_time"] or 0) if g["player_rating"]
            ]
            book.rating = round(statistics.median(ratings[-30:])) if ratings else None
            info = {g["id"]: g for g in games}
            phase_times: dict[str, list[float]] = {"opening": [], "middlegame": [], "endgame": []}
            current, sans, prev_clock = None, [], None
            for m in conn.execute(
                "SELECT game_id, ply, san, epd_before, clock_seconds FROM moves ORDER BY game_id, ply"
            ):
                g = info[m["game_id"]]
                if m["game_id"] != current:
                    current, sans, prev_clock = m["game_id"], [], None
                weight = 3 if newest and (g["end_time"] or 0) >= newest - RECENT_SECONDS else 1
                mine = db.is_player_move(g["color"], m["ply"])
                if m["ply"] < BOOK_PLIES:
                    node = book.trees[g["color"]]
                    for s in sans:
                        node = node.setdefault(s, {"w": 0, "children": {}})["children"]
                    entry = node.setdefault(m["san"], {"w": 0, "children": {}})
                    entry["w"] += weight
                sans.append(m["san"])
                if mine:
                    book.positions.setdefault(m["epd_before"], Counter())[m["san"]] += weight
                    clock = m["clock_seconds"]
                    if clock is not None and prev_clock is not None:
                        think = max(0.0, prev_clock - clock + (g["increment"] or 0))
                        phase = "opening" if m["ply"] < 24 else "middlegame" if m["ply"] < 70 else "endgame"
                        phase_times[phase].append(think)
                    prev_clock = clock if clock is not None else prev_clock
            for phase, values in phase_times.items():
                book.think[phase] = statistics.median(values) if values else 3.0
        finally:
            conn.close()
        return book

    def book_move(self, color: str, history: list[str], rng: random.Random) -> tuple[str, float] | None:
        node = self.trees[color]
        for san in history:
            if san not in node:
                return None
            node = node[san]["children"]
        if not node:
            return None
        total = sum(v["w"] for v in node.values())
        moves = list(node)
        pick = rng.choices(moves, weights=[node[m]["w"] for m in moves], k=1)[0]
        return pick, node[pick]["w"] / total

    def history_move(self, board: chess.Board, rng: random.Random) -> tuple[str, float] | None:
        counts = self.positions.get(position_key(board))
        if not counts:
            return None
        legal = {board.san(m) for m in board.legal_moves}
        valid = {m: w for m, w in counts.items() if m in legal}
        if not valid:
            return None
        total = sum(valid.values())
        pick = rng.choices(list(valid), weights=list(valid.values()), k=1)[0]
        return pick, valid[pick] / total

    def think_time(self, ply: int, rng: random.Random) -> float:
        phase = "opening" if ply < 24 else "middlegame" if ply < 70 else "endgame"
        base = self.think.get(phase, 3.0)
        return round(max(0.2, rng.gauss(base, base * 0.3)), 1)


class CloneBot:
    """Plays one colour. Use as a context manager so engine processes are closed."""

    def __init__(
        self,
        book: OpponentBook,
        color: str,
        engines: EngineSet,
        *,
        seed: int | None = None,
        use_maia: bool = True,
        fallback_depth: int = FALLBACK_DEPTH,
    ) -> None:
        self.book = book
        self.color = color
        self.rng = random.Random(seed)
        self.fallback_depth = fallback_depth
        self.maia: chess.engine.SimpleEngine | None = None
        self.stockfish: chess.engine.SimpleEngine | None = None
        self.maia_rating: int | None = None
        self.notes: list[str] = []
        if use_maia and engines.has_maia:
            choice = pick_maia(engines.maia, book.rating)
            try:
                self.maia = chess.engine.SimpleEngine.popen_uci(str(engines.lc0))
                self.maia.configure({"WeightsFile": str(choice[1])})
                self.maia_rating = choice[0]
                self.notes.append(f"Maia {choice[0]} via Lc0")
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"Maia could not start ({exc}); using Stockfish instead")
                self._quit_maia()
        if self.maia is None and engines.stockfish:
            try:
                self.stockfish = chess.engine.SimpleEngine.popen_uci(str(engines.stockfish))
                opts = {"Threads": 1, "Hash": 32}
                elo_opt = self.stockfish.options.get("UCI_Elo")
                if elo_opt is not None and "UCI_LimitStrength" in self.stockfish.options:
                    lo, hi = elo_opt.min or 1320, elo_opt.max or 3190
                    target = max(lo, min(hi, book.rating or 1500))
                    opts.update({"UCI_LimitStrength": True, "UCI_Elo": target})
                    self.notes.append(f"Stockfish limited to about {target} Elo, depth {fallback_depth}")
                else:
                    self.notes.append(f"Stockfish at depth {fallback_depth}")
                self.stockfish.configure(opts)
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"Stockfish could not start ({exc})")
                self.stockfish = None
        if self.maia is None and self.stockfish is None:
            self.notes.append("No engine found: unknown positions get a random legal move")

    def __enter__(self) -> CloneBot:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _quit_maia(self) -> None:
        if self.maia:
            with contextlib.suppress(Exception):
                self.maia.quit()
        self.maia = None

    def close(self) -> None:
        self._quit_maia()
        if self.stockfish:
            with contextlib.suppress(Exception):
                self.stockfish.quit()
            self.stockfish = None

    def _maia_move(self, board: chess.Board) -> chess.Move | None:
        if not self.maia:
            return None
        try:
            return self.maia.play(board, chess.engine.Limit(nodes=1)).move
        except chess.engine.EngineError:
            return None

    def pick(self, board: chess.Board, history: list[str]) -> tuple[chess.Move, MoveInfo]:
        ply = len(history)
        think = self.book.think_time(ply, self.rng)
        if len(history) < BOOK_PLIES:
            hit = self.book.book_move(self.color, history, self.rng)
            if hit:
                try:
                    return board.parse_san(hit[0]), MoveInfo("book", round(hit[1], 2), think)
                except ValueError:
                    pass
        known = self.book.history_move(board, self.rng)
        if known:
            if self.maia and self.rng.random() > HISTORY_WEIGHT:
                mv = self._maia_move(board)
                if mv:
                    return mv, MoveInfo("maia", 0.3, think, "blended with their history")
            return board.parse_san(known[0]), MoveInfo("history", round(known[1], 2), think)
        mv = self._maia_move(board)
        if mv:
            return mv, MoveInfo("maia", 0.5, think)
        if self.stockfish:
            try:
                res = self.stockfish.play(board, chess.engine.Limit(depth=self.fallback_depth, time=0.5))
                if res.move:
                    return res.move, MoveInfo("stockfish", 0.3, think)
            except chess.engine.EngineError:
                pass
        return self.rng.choice(list(board.legal_moves)), MoveInfo("random", 0.0, think)
