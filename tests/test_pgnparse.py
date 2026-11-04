import chess
import pytest

from chess_prep.pgnparse import (
    SkipGame,
    clean_opening_name,
    opening_family,
    parse_game,
    parse_time_control,
    position_key,
    time_class_for,
)
from tests.conftest import read_fixture_games


@pytest.mark.parametrize(
    "tc, expected",
    [("180+2", (180, 2)), ("600", (600, 0)), ("1/86400", (None, None)), ("-", (None, None)), ("", (None, None))],
)
def test_parse_time_control(tc, expected):
    assert parse_time_control(tc) == expected


def test_time_class():
    assert time_class_for(60, 0) == "bullet"
    assert time_class_for(180, 2) == "blitz"
    assert time_class_for(600, 0) == "rapid"
    assert time_class_for(1800, 20) == "classical"
    assert time_class_for(None, None) == "daily"


def test_clean_opening_name_chesscom_url():
    url = "https://www.chess.com/openings/Scandinavian-Defense-Mieses-Kotrc-Variation-3.Nc3-Qa5"
    assert clean_opening_name(url) == "Scandinavian Defense Mieses Kotrc Variation"
    assert clean_opening_name("https://www.chess.com/openings/Kings-Pawn-Opening-3...Bc5") == "King's Pawn Opening"


def test_clean_opening_name_lichess():
    assert (
        clean_opening_name("Sicilian Defense: Najdorf Variation, English Attack")
        == "Sicilian Defense: Najdorf Variation"
    )
    assert clean_opening_name(None, "B90") == "B90"


def test_opening_family():
    assert opening_family("Sicilian Defense: Najdorf Variation") == "Sicilian Defense"
    assert opening_family("Scandinavian Defense Mieses Kotrc Variation") == "Scandinavian Defense"
    assert opening_family("Queen's Pawn Opening Accelerated London System") == "Queen's Pawn Opening"


def test_parse_game_white_with_clocks_and_evals():
    pgn = read_fixture_games("alice_lichess.pgn")[0]
    g = parse_game(pgn, "ALICE")  # case-insensitive
    assert g.color == "white"
    assert g.result == "win"
    assert g.player_rating == 1500 and g.opponent_rating == 1450
    assert g.base_seconds == 180 and g.increment == 2 and g.time_class == "blitz"
    assert g.opening == "Italian Game: Classical Variation"
    assert g.ply_count == 16
    assert g.moves[0].san == "e4" and g.moves[0].clock == 179
    assert g.moves[0].eval_after == 30
    assert g.moves[0].epd_after == g.moves[1].epd_before


def test_parse_game_black_checkmate_and_timeout():
    games = read_fixture_games("alice_lichess.pgn")
    mated = parse_game(games[5], "alice")
    assert mated.color == "black" and mated.result == "loss" and mated.termination == "checkmate"
    flagged = parse_game(games[4], "alice")
    assert flagged.result == "loss" and flagged.termination == "timeout"
    won = parse_game(games[6], "alice")
    assert won.result == "win" and won.termination == "checkmate"


def test_chesscom_game():
    pgn = read_fixture_games("alice_chesscom.pgn")[0]
    g = parse_game(pgn, "alice")
    assert g.url == "https://www.chess.com/game/live/100000001"
    assert g.opening == "Scandinavian Defense Mieses Kotrc Variation"
    assert g.result == "loss" and g.termination == "resignation"
    assert g.moves[0].clock == pytest.approx(181.1)


def test_mate_eval_keeps_its_sign():
    pgn = (
        '[White "alice"]\n[Black "bob"]\n[Result "0-1"]\n[Site "https://lichess.org/x"]\n\n'
        "1. f3 { [%eval #-2] } 1... e5 0-1\n"
    )
    g = parse_game(pgn, "alice")
    assert g.moves[0].eval_after < -9000


def test_skip_variants_and_other_players():
    games = read_fixture_games("alice_lichess.pgn")
    with pytest.raises(SkipGame):
        parse_game(games[8], "alice")  # Chess960
    with pytest.raises(SkipGame):
        parse_game(games[9], "alice")  # alice not playing


def test_position_key_matches_transpositions():
    a = chess.Board()
    for san in ["Nf3", "d5", "d4"]:
        a.push_san(san)
    b = chess.Board()
    for san in ["d4", "d5", "Nf3"]:
        b.push_san(san)
    assert position_key(a) == position_key(b)
