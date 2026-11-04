import chess

from chess_prep import db
from chess_prep.prep import build_prep, build_tree, decision_point, prep_markdown, render_tree, write_prep
from chess_prep.report import build_report


def _italian_ids(conn):
    return [r[0] for r in conn.execute("SELECT id FROM games WHERE color='white' AND opening LIKE 'Italian%'")]


def test_tree_counts_every_move(alice_db):
    conn = db.connect(alice_db.db_path)
    tree = build_tree(conn, _italian_ids(conn))
    conn.close()
    assert tree["n"] == 3
    e4 = tree["children"]["e4"]
    assert e4["n"] == 3
    # opponents' replies are counted too (an old bug left them at 0)
    assert e4["children"]["e5"]["n"] == 3
    bc4 = e4["children"]["e5"]["children"]["Nf3"]["children"]["Nc6"]["children"]["Bc4"]
    assert set(bc4["children"]) == {"Bc5", "Nf6"}
    assert bc4["children"]["Bc5"]["n"] == 2


def test_render_tree_marks_whose_move(alice_db):
    conn = db.connect(alice_db.db_path)
    lines = render_tree(build_tree(conn, _italian_ids(conn)), "white")
    conn.close()
    assert lines[0].startswith("1.e4") and "them" in lines[0]
    assert "1...e5" in lines[1] and "you" in lines[1]


def test_decision_point_is_on_your_move(alice_db):
    conn = db.connect(alice_db.db_path)
    tree = build_tree(conn, _italian_ids(conn))
    conn.close()
    d = decision_point(tree, "white", engine=None, depth=1)
    board = chess.Board(d["fen"])
    assert board.turn == chess.BLACK  # alice has White, so it is your (Black's) move
    assert d["line"] == "1.e4 e5 2.Nf3 Nc6 3.Bc4"


def test_decision_point_with_engine(alice_db, fake_engine):
    from chess_prep.prep import open_engine

    conn = db.connect(alice_db.db_path)
    tree = build_tree(conn, _italian_ids(conn))
    conn.close()
    with open_engine(fake_engine) as eng:
        d = decision_point(tree, "white", eng, depth=1)
    assert d["recommended"]["move"]
    assert d["engine"]


def test_build_prep_and_markdown(alice_db, tmp_path):
    rep = build_report(alice_db.db_path, alice_db)
    prep = build_prep(alice_db.db_path, rep, stockfish=None)
    assert prep["white"][0]["name"] == "Italian Game"
    md = prep_markdown(prep)
    assert "# Opening prep: alice (Lichess)" in md
    assert "When they have White" in md and "When they have Black" in md
    md_path, json_path = write_prep(prep, tmp_path / "out")
    assert md_path.exists() and json_path.exists()
