import random

import chess

from chess_prep.clone import CloneBot, OpponentBook
from chess_prep.engines import EngineSet

NO_ENGINES = EngineSet(stockfish=None, lc0=None, maia={})


def test_book_uses_the_right_colour(alice_db):
    book = OpponentBook.from_db(alice_db.db_path)
    rng = random.Random(1)
    assert book.book_move("white", [], rng)[0] == "e4"
    # as Black against 1.d4 alice always answered d5
    assert book.book_move("black", ["d4"], rng)[0] == "d5"
    # against 1.e4 as Black alice played e5 (twice)
    assert book.book_move("black", ["e4"], rng)[0] == "e5"
    assert book.book_move("white", ["a3"], rng) is None


def test_history_matches_transpositions(alice_db):
    book = OpponentBook.from_db(alice_db.db_path)
    board = chess.Board()
    for san in ["e4", "e5", "Nf3", "Nc6", "Bc4"]:
        board.push_san(san)
    assert book.history_move(board, random.Random(0)) is None  # Black to move: not alice's side here
    board.push_san("Bc5")
    move, share = book.history_move(board, random.Random(0))
    assert move == "c3" and share == 1.0


def test_clone_plays_a_full_legal_game_without_engines(alice_db):
    book = OpponentBook.from_db(alice_db.db_path)
    board = chess.Board()
    history = []
    with CloneBot(book, "white", NO_ENGINES, seed=5) as white, CloneBot(book, "black", NO_ENGINES, seed=6) as black:
        assert any("random" in n for n in white.notes)
        while not board.is_game_over(claim_draw=True) and len(history) < 120:
            bot = white if board.turn == chess.WHITE else black
            mv, info = bot.pick(board, history)
            assert mv in board.legal_moves
            assert info.source in {"book", "history", "random"}
            history.append(board.san(mv))
            board.push(mv)
    assert history[0] == "e4"


def test_stockfish_fallback(alice_db, fake_engine):
    book = OpponentBook.from_db(alice_db.db_path)
    engines = EngineSet(stockfish=fake_engine, lc0=None, maia={})
    board = chess.Board()
    board.push_san("a3")  # alice never saw this
    with CloneBot(book, "black", engines, seed=1) as bot:
        mv, info = bot.pick(board, ["a3"])
    assert info.source == "stockfish"
    assert mv in board.legal_moves


def test_maia_path(alice_db, fake_engine, tmp_path):
    weights = tmp_path / "maia-1500.pb.gz"
    weights.write_bytes(b"0" * 200_000)
    book = OpponentBook.from_db(alice_db.db_path)
    engines = EngineSet(stockfish=None, lc0=fake_engine, maia={1500: weights})
    board = chess.Board()
    board.push_san("h4")
    with CloneBot(book, "black", engines, seed=1) as bot:
        assert bot.maia_rating == 1500
        mv, info = bot.pick(board, ["h4"])
    assert info.source == "maia"
    assert mv in board.legal_moves
