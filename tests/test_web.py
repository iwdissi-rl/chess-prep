import chess
import pytest

from chess_prep.engines import EngineSet
from chess_prep.web import parse_drag

flask = pytest.importorskip("flask")


def test_parse_drag_normal_move_and_promotion():
    board = chess.Board()
    # the original web UI always appended "q" and rejected every normal move
    assert parse_drag(board, "e2", "e4") == chess.Move.from_uci("e2e4")
    promo = chess.Board("8/P6k/8/8/8/8/8/K7 w - - 0 1")
    assert parse_drag(promo, "a7", "a8") == chess.Move.from_uci("a7a8q")
    assert parse_drag(board, "e2", "e5") is None
    assert parse_drag(board, "zz", "e5") is None


def test_web_game_flow(alice_db):
    from chess_prep.clone import OpponentBook
    from chess_prep.practice import history
    from chess_prep.web import create_app

    app = create_app(alice_db, OpponentBook.from_db(alice_db.db_path), EngineSet(None, None, {}))
    client = app.test_client()
    assert b"Practice vs alice" in client.get("/").data
    game = client.post("/api/new", json={"color": "black"}).get_json()
    assert len(game["moves"]) == 1 and game["moves"][0]["san"] == "e4"  # alice's book move
    reply = client.post("/api/move", json={"game_id": game["game_id"], "from": "e7", "to": "e5"}).get_json()
    assert "error" not in reply and len(reply["moves"]) == 3
    bad = client.post("/api/move", json={"game_id": game["game_id"], "from": "a7", "to": "a3"}).get_json()
    assert bad["error"] == "Illegal move."
    done = client.post("/api/resign", json={"game_id": game["game_id"]}).get_json()
    assert done["over"] is True
    assert history(alice_db)[-1]["result"] == "loss"
