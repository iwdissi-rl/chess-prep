import json

import pytest

from chess_prep import db
from chess_prep.fetch import FetchError, UserNotFound, fetch_games
from chess_prep.paths import Player
from tests.conftest import read_fixture_games


class FakeResponse:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code = status
        self._payload = payload
        self._lines = lines or []
        self.headers = {}

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix):
                return resp
        return FakeResponse(404)


def test_chesscom_user_not_found():
    with pytest.raises(UserNotFound):
        fetch_games(Player("chesscom", "nobody_here"), 10, session=FakeSession({}))


def test_chesscom_fetch_newest_first_and_limit():
    pgn = read_fixture_games("alice_chesscom.pgn")[0]
    games = [
        {
            "pgn": pgn.replace("100000001", str(n)),
            "url": f"https://www.chess.com/game/live/{n}",
            "rules": "chess",
            "time_class": "blitz",
            "end_time": 1700000000 + n,
        }
        for n in range(5)
    ]
    games.append({"pgn": pgn, "url": "https://www.chess.com/game/live/960", "rules": "chess960", "time_class": "blitz"})
    session = FakeSession(
        {
            "https://api.chess.com/pub/player/alice/games/archives": FakeResponse(
                payload={
                    "archives": [
                        "https://api.chess.com/pub/player/alice/games/2026/01",
                        "https://api.chess.com/pub/player/alice/games/2026/02",
                    ]
                }
            ),
            "https://api.chess.com/pub/player/alice/games/2026/02": FakeResponse(payload={"games": games}),
            "https://api.chess.com/pub/player/alice/games/2026/01": FakeResponse(payload={"games": []}),
        }
    )
    player = Player("chesscom", "Alice")
    stats = fetch_games(player, 3, session=session)
    assert stats.new == 3 and stats.total_in_db == 3
    # newest month is requested first, the older one is never needed
    assert [c[0] for c in session.calls][1].endswith("/2026/02")
    assert not any(c[0].endswith("/2026/01") for c in session.calls)
    conn = db.connect(player.db_path)
    urls = {r[0] for r in conn.execute("SELECT url FROM games")}
    conn.close()
    assert urls == {f"https://www.chess.com/game/live/{n}" for n in (2, 3, 4)}

    again = fetch_games(player, 3, session=session)
    assert again.new == 0 and again.already_had == 3


def test_lichess_fetch_ndjson_and_speed_filter():
    lines = []
    for i, pgn in enumerate(read_fixture_games("alice_lichess.pgn")):
        variant = "chess960" if "Chess960" in pgn else "standard"
        lines.append(
            json.dumps(
                {"id": f"g{i}", "pgn": pgn, "variant": variant, "speed": "blitz", "lastMoveAt": 1_700_000_000_000}
            )
        )
    session = FakeSession(
        {
            "https://lichess.org/api/user/alice": FakeResponse(payload={"id": "alice"}),
            "https://lichess.org/api/games/user/alice": FakeResponse(lines=lines),
        }
    )
    stats = fetch_games(Player("lichess", "alice"), 50, {"blitz", "rapid"}, session=session)
    assert stats.new == 8 and stats.skipped == 1  # one game alice did not play; chess960 filtered earlier
    params = session.calls[1][1]["params"]
    assert params["perfType"] == "blitz,rapid" and params["max"] == 50 and params["clocks"] == "true"


def test_lichess_closed_account():
    session = FakeSession(
        {"https://lichess.org/api/user/alice": FakeResponse(payload={"id": "alice", "disabled": True})}
    )
    with pytest.raises(UserNotFound):
        fetch_games(Player("lichess", "alice"), 5, session=session)


def test_bad_speed_and_bad_username():
    with pytest.raises(ValueError):
        fetch_games(Player("lichess", "alice"), 5, {"hyperbullet"}, session=FakeSession({}))
    with pytest.raises(ValueError):
        Player("lichess", "../etc/passwd")


def test_server_error_is_reported(monkeypatch):
    monkeypatch.setattr("chess_prep.fetch.time.sleep", lambda s: None)
    session = FakeSession({"https://lichess.org/api/user/alice": FakeResponse(status=503)})
    with pytest.raises(FetchError):
        fetch_games(Player("lichess", "alice"), 5, session=session)
