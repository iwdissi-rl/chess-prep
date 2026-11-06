"""Engine pass: evaluate every position in the stored games with Stockfish.

Each unique position is evaluated once and cached in the ``evals`` table, so
re-running after fetching more games only analyses the new positions.
"""

from __future__ import annotations

import contextlib
import os
import queue
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import chess
import chess.engine

from chess_prep.pgnparse import MATE_SCORE

DEFAULT_DEPTH = 12
RECHECK_EXTRA_DEPTH = 8
# A move the engine called best should not cost this much (win %, mover's view).
RECHECK_DROP = 15.0


@dataclass
class AnalysisStats:
    positions_total: int = 0
    analysed: int = 0
    cached: int = 0
    errors: int = 0
    rechecked: int = 0


def default_workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) // 2))


def terminal_eval(board: chess.Board) -> int | None:
    """Score finished positions without an engine (White's point of view)."""
    if board.is_checkmate():
        return -MATE_SCORE if board.turn == chess.WHITE else MATE_SCORE
    if board.is_stalemate() or board.is_insufficient_material():
        return 0
    return None


def pending_positions(conn: sqlite3.Connection, max_ply: int | None = None) -> tuple[list[str], int]:
    """Positions that still need an engine eval (no cached Stockfish result)."""
    where = "WHERE ply < ?" if max_ply else ""
    args = (max_ply,) if max_ply else ()
    rows = conn.execute(
        f"""SELECT epd FROM (
                SELECT epd_before AS epd FROM moves {where}
                UNION SELECT epd_after FROM moves {where}
            )""",
        args + args,
    ).fetchall()
    all_epds = [r[0] for r in rows]
    done = {r[0] for r in conn.execute("SELECT epd FROM evals WHERE source IN ('stockfish', 'terminal')")}
    todo = [e for e in all_epds if e not in done]
    return todo, len(all_epds)


def _worker(
    stockfish: Path,
    depth: int,
    jobs: queue.Queue,
    results: queue.Queue,
    stop: threading.Event,
) -> None:
    try:
        engine = chess.engine.SimpleEngine.popen_uci(str(stockfish))
        engine.configure({"Threads": 1, "Hash": 64})
    except Exception as exc:  # noqa: BLE001 - report any start-up failure
        results.put(("__error__", str(exc)))
        return
    try:
        while not stop.is_set():
            try:
                epd = jobs.get_nowait()
            except queue.Empty:
                return
            board = chess.Board()
            board.set_epd(epd)
            try:
                info = engine.analyse(board, chess.engine.Limit(depth=depth))
                score = info.get("score")
                cp = score.white().score(mate_score=MATE_SCORE) if score else None
                pv = info.get("pv") or []
                results.put((epd, cp, pv[0].uci() if pv else None))
            except chess.engine.EngineError:
                results.put((epd, None, None))
    finally:
        with contextlib.suppress(Exception):
            engine.quit()


def run_analysis(
    db_path: Path,
    stockfish: Path,
    *,
    depth: int = DEFAULT_DEPTH,
    workers: int | None = None,
    on_start: Callable[[int], None] | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> AnalysisStats:
    """Evaluate every not-yet-analysed position. Safe to stop and resume."""
    from chess_prep import db

    conn = db.connect(db_path)
    stats = AnalysisStats()
    try:
        todo, stats.positions_total = pending_positions(conn)
        stats.cached = stats.positions_total - len(todo)

        engine_jobs: list[str] = []
        for epd in todo:
            board = chess.Board()
            board.set_epd(epd)
            cp = terminal_eval(board)
            if cp is not None and board.is_game_over():
                conn.execute(
                    "INSERT OR REPLACE INTO evals (epd, cp, best_uci, depth, source) "
                    "VALUES (?, ?, NULL, 0, 'terminal')",
                    (epd, cp),
                )
                stats.analysed += 1
            else:
                engine_jobs.append(epd)
        conn.commit()
        if on_start:
            on_start(len(engine_jobs))
        if not engine_jobs:
            stats.rechecked = recheck_contradictions(conn, stockfish, depth)
            return stats

        jobs: queue.Queue = queue.Queue()
        for epd in engine_jobs:
            jobs.put(epd)
        results: queue.Queue = queue.Queue()
        stop = threading.Event()
        n = max(1, min(workers or default_workers(), len(engine_jobs)))
        threads = [
            threading.Thread(target=_worker, args=(stockfish, depth, jobs, results, stop), daemon=True)
            for _ in range(n)
        ]
        for t in threads:
            t.start()

        received = 0
        batch: list[tuple] = []
        try:
            while received < len(engine_jobs):
                try:
                    item = results.get(timeout=1.0)
                except queue.Empty:
                    if not any(t.is_alive() for t in threads):
                        break
                    continue
                if item[0] == "__error__":
                    stop.set()
                    raise RuntimeError(f"Stockfish failed to start: {item[1]}")
                received += 1
                epd, cp, best = item
                if cp is None:
                    stats.errors += 1
                else:
                    stats.analysed += 1
                    batch.append((epd, cp, best, depth))
                if len(batch) >= 200:
                    _flush(conn, batch)
                if on_progress:
                    on_progress(received)
        finally:
            stop.set()
            _flush(conn, batch)
            for t in threads:
                t.join(timeout=10)
        stats.rechecked = recheck_contradictions(conn, stockfish, depth)
    finally:
        conn.close()
    return stats


def _win_pct(cp: int) -> float:
    from chess_prep.report import win_pct

    return win_pct(cp)


def find_contradictions(conn: sqlite3.Connection, max_depth: int) -> list[str]:
    """Positions where the engine's own best move loses a lot by the engine's own eval.

    A shallow search sometimes picks a move whose resulting position, searched one
    ply deeper, turns out to be lost (a horizon effect). Then the move played looks
    like a blunder and the "best move" shown is that same blunder. Returns the
    positions (before and after such moves) that need a deeper look.
    """
    rows = conn.execute(
        """SELECT DISTINCT m.epd_before, m.epd_after, eb.cp AS cp_before, ea.cp AS cp_after
           FROM moves m
           JOIN evals eb ON eb.epd = m.epd_before
           JOIN evals ea ON ea.epd = m.epd_after
           WHERE m.uci = eb.best_uci AND eb.source = 'stockfish' AND eb.depth <= ?
             AND eb.cp IS NOT NULL AND ea.cp IS NOT NULL""",
        (max_depth,),
    ).fetchall()
    out: list[str] = []
    for epd_before, epd_after, cp_before, cp_after in rows:
        white_to_move = epd_before.split()[1] == "w"
        before, after = _win_pct(cp_before), _win_pct(cp_after)
        drop = (before - after) if white_to_move else (after - before)
        if drop >= RECHECK_DROP:
            out += [epd_before, epd_after]
    return list(dict.fromkeys(out))


def recheck_contradictions(conn: sqlite3.Connection, stockfish: Path, depth: int) -> int:
    """Re-analyse self-contradicting positions deeper so blunders get the right best move."""
    epds = find_contradictions(conn, depth)
    if not epds:
        return 0
    deeper = depth + RECHECK_EXTRA_DEPTH
    batch: list[tuple] = []
    engine = chess.engine.SimpleEngine.popen_uci(str(stockfish))
    try:
        engine.configure({"Threads": max(1, min(8, os.cpu_count() or 1)), "Hash": 256})
        for epd in epds:
            board = chess.Board()
            board.set_epd(epd)
            if board.is_game_over():
                continue
            try:
                info = engine.analyse(board, chess.engine.Limit(depth=deeper))
            except chess.engine.EngineError:
                continue
            score = info.get("score")
            if score is None:
                continue
            pv = info.get("pv") or []
            batch.append((epd, score.white().score(mate_score=MATE_SCORE), pv[0].uci() if pv else None, deeper))
    finally:
        with contextlib.suppress(Exception):
            engine.quit()
    n = len(batch)
    _flush(conn, batch)
    return n


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    if not batch:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO evals (epd, cp, best_uci, depth, source) VALUES (?, ?, ?, ?, 'stockfish')",
        batch,
    )
    conn.commit()
    batch.clear()
