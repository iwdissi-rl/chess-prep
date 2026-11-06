import chess

from chess_prep import db
from chess_prep.report import build_report, classify_blunder, win_pct


def test_win_pct_is_symmetric_and_capped():
    assert win_pct(0) == 50
    assert round(win_pct(300) + win_pct(-300), 6) == 100
    assert win_pct(99999) == win_pct(1500)
    assert win_pct(None) is None


def test_report_without_engine(alice_db):
    rep = build_report(alice_db.db_path, alice_db)
    s = rep["summary"]
    assert s["games"] == 8
    assert (s["wins"], s["draws"], s["losses"]) == (4, 1, 3)
    assert s["by_color"]["white"]["games"] == 4
    assert s["by_color"]["black"]["games"] == 4

    white = rep["openings"]["white"]
    assert white[0]["name"] == "Italian Game" and white[0]["games"] == 3
    assert white[0]["main_line"].startswith("1.e4 e5 2.Nf3 Nc6 3.Bc4")

    assert rep["first_moves"]["as_white"][0] == {"move": "e4", "count": 4, "pct": 100.0, "score_pct": 62.5}
    assert set(rep["first_moves"]["as_black"]) == {"1.d4", "1.e4", "1.f3"}

    assert rep["losses"]["by_termination"] == {"checkmate": 1, "timeout": 1, "resignation": 1}
    assert rep["clock"]["has_clocks"] is True
    assert rep["mistakes"]["has_evals"] is True  # the Lichess fixture game carries evals
    assert rep["notes"]


def _insert_eval(conn, epd, cp, best=None):
    conn.execute(
        "INSERT OR REPLACE INTO evals (epd, cp, best_uci, depth, source) VALUES (?, ?, ?, 10, 'stockfish')",
        (epd, cp, best),
    )


def test_blunder_is_found_and_classified(alice_db):
    conn = db.connect(alice_db.db_path)
    conn.execute("DELETE FROM evals")
    # Game 8: alice (white) ... 5.d3 d6. Pretend 5.d3 hung a piece: before +0.3, after -4.0
    gid = conn.execute("SELECT id FROM games WHERE url LIKE '%fixture07'").fetchone()[0]
    m = conn.execute("SELECT epd_before, epd_after FROM moves WHERE game_id = ? AND ply = 8", (gid,)).fetchone()
    _insert_eval(conn, m["epd_before"], 30, "e1g1")
    _insert_eval(conn, m["epd_after"], -400, None)
    conn.commit()
    conn.close()
    rep = build_report(alice_db.db_path, alice_db)
    mis = rep["mistakes"]
    assert mis["blunders"] == 1
    ex = mis["examples"][0]
    assert ex["move"] == "5.d3"
    assert ex["best_move"] == "O-O"
    assert ex["fen"].endswith(" 5")  # real move number, not 1


def test_classify_hanging_piece():
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P1Q1/8/PPPP1PPP/RNB1KBNR b KQkq - 1 2")
    # White queen on g4 is attacked by the c8 bishop? No: by d7 pawn? Use a plain hanging queen.
    board = chess.Board("rnbqkbnr/ppp2ppp/8/3pp3/4P1Q1/8/PPPP1PPP/RNB1KBNR b KQkq - 0 3")
    assert classify_blunder(board, "c8g4", chess.WHITE) == "hanging piece"


def test_classify_fork():
    # Black knight on e4 can jump to f2 forking the white queen on d1 and rook on h1.
    board = chess.Board("rnbqkb1r/pppp1ppp/8/8/4n3/8/PPPP2PP/RNBQKB1R b KQkq - 0 1")
    assert classify_blunder(board, "e4f2", chess.WHITE) == "fork"


def test_classify_allowed_mate():
    board = chess.Board("rnbqkbnr/pppp1ppp/8/4p3/6P1/5P2/PPPPP2P/RNBQKBNR b KQkq - 0 2")
    assert classify_blunder(board, "d8h4", chess.WHITE) == "allowed mate"


def test_classify_missed_tactic():
    before = chess.Board("rnbqkbnr/ppp2ppp/8/3pp3/4P1Q1/8/PPPP1PPP/RNB1KBNR b KQkq - 0 3")
    after = before.copy()
    after.push_uci("a7a6")  # black ignores the free queen
    assert (
        classify_blunder(after, None, chess.BLACK, board_before=before, played_uci="a7a6", best_before_uci="c8g4")
        == "missed tactic"
    )


def test_blunder_never_names_itself_as_best(alice_db):
    # A shallow search can call the played move best and still score the result as lost.
    conn = db.connect(alice_db.db_path)
    conn.execute("DELETE FROM evals")
    gid = conn.execute("SELECT id FROM games WHERE url LIKE '%fixture07'").fetchone()[0]
    m = conn.execute("SELECT uci, epd_before, epd_after FROM moves WHERE game_id = ? AND ply = 8", (gid,)).fetchone()
    _insert_eval(conn, m["epd_before"], 30, m["uci"])
    _insert_eval(conn, m["epd_after"], -400, None)
    conn.commit()
    conn.close()
    ex = build_report(alice_db.db_path, alice_db)["mistakes"]["examples"][0]
    assert ex["move"] == "5.d3"
    assert ex["best_move"] is None


def test_avoid_note_names_the_colour():
    from chess_prep.report import _notes

    rep = {
        "summary": {
            "score_pct": 50,
            "by_color": {"white": {"games": 1, "score_pct": 50}, "black": {"games": 1, "score_pct": 50}},
        },
        "losses": {
            "count": 0,
            "by_termination": {},
            "worst_openings": [],
            "best_openings": [
                {"opening": "English Opening", "they_play": "black", "games": 7, "score_pct": 100.0},
            ],
        },
        "clock": {},
        "mistakes": {"has_evals": False},
        "endgames": {"games_reaching_endgame": 0},
    }
    notes = _notes(rep)
    expected = "Avoid the English Opening when you have White if you can: they score 100.0% in it with Black (7 games)."
    assert expected in notes
