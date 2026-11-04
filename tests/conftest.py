import contextlib
import os
import stat
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own empty chess-prep home, and no real engines."""
    home = tmp_path / "home"
    monkeypatch.setenv("CHESSPREP_HOME", str(home))
    for var in ("CHESSPREP_STOCKFISH", "CHESSPREP_LC0", "CHESSPREP_MAIA_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("chess_prep.engines.COMMON_DIRS", [])
    monkeypatch.setattr("chess_prep.engines._which", lambda name: None)
    return home


@pytest.fixture
def fake_engine(tmp_path):
    """Path to an executable that runs tests/fake_uci.py."""
    if os.name == "nt":
        pytest.skip("fake engine wrapper uses a shell script")
    script = tmp_path / "fake-engine"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(__file__).parent / "fake_uci.py"}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def read_fixture_games(name: str) -> list[str]:
    text = (FIXTURES / name).read_text()
    games, current = [], []
    for line in text.splitlines():
        if line.startswith("[Event ") and current:
            games.append("\n".join(current).strip() + "\n")
            current = []
        current.append(line)
    if current:
        games.append("\n".join(current).strip() + "\n")
    return games


@pytest.fixture
def alice_db():
    """A player database for 'alice' on Lichess built from the fixture PGNs."""
    from chess_prep import db
    from chess_prep.paths import Player
    from chess_prep.pgnparse import SkipGame, parse_game, store_game

    player = Player("lichess", "alice")
    conn = db.connect(player.db_path)
    db.set_meta(conn, "username", "alice")
    for pgn in read_fixture_games("alice_lichess.pgn"):
        with contextlib.suppress(SkipGame):
            store_game(conn, "lichess", parse_game(pgn, "alice"))
    conn.commit()
    conn.close()
    return player
