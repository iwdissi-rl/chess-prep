import os
import shutil

import chess
import pytest

from chess_prep import db
from chess_prep.analyze import pending_positions, run_analysis, terminal_eval


def test_terminal_eval():
    mate = chess.Board("rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3")
    assert terminal_eval(mate) == -10000  # White is mated
    assert terminal_eval(chess.Board("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")) == 0  # stalemate
    assert terminal_eval(chess.Board()) is None


def test_analysis_with_fake_engine(alice_db, fake_engine):
    conn = db.connect(alice_db.db_path)
    todo, total = pending_positions(conn)
    conn.close()
    assert total > 50 and len(todo) == total  # Lichess evals do not count as done

    starts = []
    stats = run_analysis(alice_db.db_path, fake_engine, depth=1, workers=2, on_start=starts.append)
    assert stats.errors == 0
    assert stats.analysed == total
    conn = db.connect(alice_db.db_path)
    row = conn.execute("SELECT cp, best_uci FROM evals WHERE epd = ?", (chess.Board().epd(),)).fetchone()
    assert row["cp"] == 13 and row["best_uci"]
    assert pending_positions(conn)[0] == []
    conn.close()

    again = run_analysis(alice_db.db_path, fake_engine, depth=1)
    assert again.analysed == 0 and again.cached == total


REAL_STOCKFISH = os.environ.get("CHESSPREP_TEST_STOCKFISH") or shutil.which("stockfish")


@pytest.mark.skipif(not REAL_STOCKFISH, reason="real Stockfish not installed (set CHESSPREP_TEST_STOCKFISH)")
def test_analysis_with_real_stockfish(alice_db):
    stats = run_analysis(alice_db.db_path, REAL_STOCKFISH, depth=6, workers=2)
    assert stats.analysed > 50 and stats.errors == 0
