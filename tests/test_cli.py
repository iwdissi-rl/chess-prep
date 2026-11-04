import json

import pytest
from typer.testing import CliRunner

from chess_prep import __version__
from chess_prep.cli import app
from tests.conftest import read_fixture_games
from tests.test_fetch import FakeResponse, FakeSession

runner = CliRunner()


@pytest.fixture
def fake_lichess(monkeypatch):
    lines = [
        json.dumps({"id": f"g{i}", "pgn": pgn, "variant": "standard", "speed": "blitz"})
        for i, pgn in enumerate(read_fixture_games("alice_lichess.pgn"))
        if "Chess960" not in pgn
    ]
    session = FakeSession(
        {
            "https://lichess.org/api/user/alice": FakeResponse(payload={"id": "alice"}),
            "https://lichess.org/api/games/user/alice": FakeResponse(lines=lines),
        }
    )
    monkeypatch.setattr("chess_prep.fetch.make_session", lambda: session)
    return session


def invoke(*args):
    result = runner.invoke(app, list(args), env={"COLUMNS": "120"})
    return result


def test_version_and_help():
    assert __version__ in invoke("--version").output
    out = invoke().output  # no TTY in tests: prints help instead of the menu
    assert "fetch" in out and "cheatsheet" in out and "doctor" in out


def test_doctor_offline():
    r = invoke("doctor", "--offline")
    assert r.exit_code == 0, r.output
    assert "Stockfish" in r.output and "How to fix" in r.output


def test_unknown_player_is_a_friendly_error():
    r = invoke("analyze", "nobody")
    assert r.exit_code == 1
    assert "chessprep fetch nobody" in r.output
    assert "Traceback" not in r.output


def test_full_flow_without_engines(fake_lichess, tmp_path):
    r = invoke("fetch", "alice", "--site", "lichess", "--max", "50")
    assert r.exit_code == 0, r.output
    assert "8 new games" in r.output

    r = invoke("players")
    assert "alice" in r.output and "Lichess" in r.output

    r = invoke("analyze", "alice")
    assert r.exit_code == 0, r.output
    assert "Stockfish not found" in r.output and "Game plan" in r.output

    out = tmp_path / "reports"
    r = invoke("prep", "alice", "--no-engine", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert (out / "prep.md").exists()

    r = invoke("cheatsheet", "alice", "--out", str(out))
    assert r.exit_code == 0, r.output
    assert (out / "cheatsheet.pdf").read_bytes().startswith(b"%PDF")

    r = invoke("practice", "alice", "--moves", "d4 c4", "--color", "white", "--no-board")
    assert r.exit_code == 0, r.output
    assert "1. d4" in r.output and "unfinished" in r.output

    r = invoke("practice", "alice", "--history")
    assert "Practice games vs alice" in r.output


def test_engine_pass_through_cli(fake_lichess, fake_engine, monkeypatch):
    monkeypatch.setenv("CHESSPREP_STOCKFISH", str(fake_engine))
    assert invoke("fetch", "alice", "--site", "lichess").exit_code == 0
    r = invoke("analyze", "alice", "--depth", "4", "--workers", "2")
    assert r.exit_code == 0, r.output
    assert "Engine pass done" in r.output
    r = invoke("prep", "alice", "--depth", "6")
    assert r.exit_code == 0, r.output
    assert "Your decision points" in r.output
    r = invoke("practice", "alice", "--auto", "3", "--color", "black", "--seed", "1", "--no-board")
    assert r.exit_code == 0, r.output


def test_practice_needs_moves_without_terminal(fake_lichess):
    invoke("fetch", "alice", "--site", "lichess")
    r = invoke("practice", "alice")
    assert r.exit_code == 1 and "--moves" in r.output
    r = invoke("practice", "alice", "--moves", "e4 Ke2 Qh5 Kd3 Kc4 Kb5")
    assert r.exit_code == 1 and "not legal" in r.output


def test_fetch_unknown_user(monkeypatch):
    monkeypatch.setattr("chess_prep.fetch.make_session", lambda: FakeSession({}))
    r = invoke("fetch", "ghost", "--site", "lichess")
    assert r.exit_code == 1 and "no player called" in r.output


def test_config_commands(fake_engine):
    r = invoke("config", "set", "stockfish", str(fake_engine))
    assert r.exit_code == 0, r.output
    assert str(fake_engine.resolve()) in invoke("config", "show").output
    assert invoke("config", "set", "colour", "x").exit_code == 1
    assert invoke("config", "unset", "stockfish").exit_code == 0
