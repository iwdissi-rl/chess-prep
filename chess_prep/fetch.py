"""Download a player's public games from Chess.com or Lichess.

Both sites offer free public APIs that need no account or API key:
  - Chess.com: https://www.chess.com/news/view/published-data-api
  - Lichess:   https://lichess.org/api#tag/Games/operation/apiGamesUser
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import requests

from chess_prep import __version__, db
from chess_prep.paths import Player
from chess_prep.pgnparse import SkipGame, parse_game, store_game

USER_AGENT = f"chess-prep/{__version__} (+https://github.com/iwdissi-rl/chess-prep)"
CHESSCOM_API = "https://api.chess.com/pub/player"
LICHESS_API = "https://lichess.org/api"
SPEEDS = ("bullet", "blitz", "rapid", "classical", "daily")


class FetchError(RuntimeError):
    pass


class UserNotFound(FetchError):
    pass


@dataclass
class RawGame:
    pgn: str
    url: str
    time_class: str | None
    end_time: int | None


@dataclass
class FetchStats:
    new: int = 0
    already_had: int = 0
    skipped: int = 0
    total_in_db: int = 0


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s  # requests honours HTTPS_PROXY / HTTP_PROXY from the environment


def _get(session: requests.Session, url: str, *, retries: int = 4, **kwargs) -> requests.Response:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)
        except requests.RequestException as exc:
            if attempt == retries:
                raise FetchError(f"Could not reach {url.split('/')[2]}: {exc}") from exc
            time.sleep(delay)
            delay *= 2
            continue
        if resp.status_code == 404:
            return resp
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            wait = float(resp.headers.get("Retry-After") or (60 if resp.status_code == 429 else delay))
            time.sleep(min(wait, 90))
            delay *= 2
            continue
        if resp.status_code >= 400:
            raise FetchError(f"{url.split('/')[2]} answered HTTP {resp.status_code} for {url}")
        return resp
    raise FetchError(f"Gave up on {url}")


# ---------------------------------------------------------------- Chess.com

_CHESSCOM_CLASS = {"bullet": "bullet", "blitz": "blitz", "rapid": "rapid", "daily": "daily"}


def iter_chesscom(username: str, session: requests.Session, speeds: set[str] | None) -> Iterator[RawGame]:
    """Newest games first."""
    resp = _get(session, f"{CHESSCOM_API}/{username.lower()}/games/archives")
    if resp.status_code == 404:
        raise UserNotFound(f"Chess.com has no player called {username!r}.")
    archives = resp.json().get("archives", [])
    for archive_url in reversed(archives):
        month = _get(session, archive_url)
        if month.status_code == 404:
            continue
        for g in reversed(month.json().get("games", [])):
            if g.get("rules", "chess") != "chess" or not g.get("pgn"):
                continue
            tclass = _CHESSCOM_CLASS.get(g.get("time_class", ""), g.get("time_class"))
            if speeds and tclass not in speeds:
                continue
            yield RawGame(g["pgn"], g.get("url", ""), tclass, g.get("end_time"))


# ------------------------------------------------------------------ Lichess

_LICHESS_PERF = {
    "bullet": "ultraBullet,bullet",
    "blitz": "blitz",
    "rapid": "rapid",
    "classical": "classical",
    "daily": "correspondence",
}
_LICHESS_CLASS = {"ultraBullet": "bullet", "correspondence": "daily"}


def iter_lichess(
    username: str, session: requests.Session, speeds: set[str] | None, max_games: int
) -> Iterator[RawGame]:
    """Newest games first, streamed as NDJSON."""
    user = _get(session, f"{LICHESS_API}/user/{username}")
    if user.status_code == 404:
        raise UserNotFound(f"Lichess has no player called {username!r}.")
    if user.json().get("disabled"):
        raise UserNotFound(f"The Lichess account {username!r} is closed.")
    params = {
        "max": max_games,
        "clocks": "true",
        "evals": "true",
        "opening": "true",
        "pgnInJson": "true",
    }
    if speeds:
        params["perfType"] = ",".join(_LICHESS_PERF[s] for s in sorted(speeds))
    resp = _get(
        session,
        f"{LICHESS_API}/games/user/{username}",
        params=params,
        headers={"Accept": "application/x-ndjson"},
        stream=True,
        timeout=60,
    )
    if resp.status_code == 404:
        raise UserNotFound(f"Lichess has no player called {username!r}.")
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        g = json.loads(line)
        if g.get("variant", "standard") != "standard" or not g.get("pgn"):
            continue
        speed = g.get("speed")
        last = g.get("lastMoveAt")
        yield RawGame(
            g["pgn"],
            f"https://lichess.org/{g['id']}",
            _LICHESS_CLASS.get(speed, speed),
            int(last / 1000) if last else None,
        )


# -------------------------------------------------------------------- main


def fetch_games(
    player: Player,
    max_games: int = 200,
    speeds: set[str] | None = None,
    *,
    session: requests.Session | None = None,
    on_progress: Callable[[FetchStats], None] | None = None,
) -> FetchStats:
    """Fetch up to ``max_games`` of the player's most recent games into their database."""
    if speeds:
        unknown = set(speeds) - set(SPEEDS)
        if unknown:
            raise ValueError(f"Unknown speed(s): {', '.join(sorted(unknown))}. Use: {', '.join(SPEEDS)}")
    session = session or make_session()
    if player.site == "chesscom":
        source = iter_chesscom(player.username, session, speeds)
    else:
        source = iter_lichess(player.username, session, speeds, max_games)

    stats = FetchStats()
    conn = db.connect(player.db_path)
    try:
        db.set_meta(conn, "username", player.username)
        db.set_meta(conn, "site", player.site)
        for seen, raw in enumerate(itertools.islice(source, max_games), 1):
            try:
                parsed = parse_game(
                    raw.pgn, player.username, url=raw.url, time_class=raw.time_class, end_time=raw.end_time
                )
                if store_game(conn, player.site, parsed):
                    stats.new += 1
                else:
                    stats.already_had += 1
            except (SkipGame, ValueError) as exc:  # ValueError: illegal SAN etc.
                stats.skipped += 1
                _ = exc
            if seen % 25 == 0:
                conn.commit()
            if on_progress:
                on_progress(stats)
        db.set_meta(conn, "fetched_at", time.strftime("%Y-%m-%d %H:%M"))
        conn.commit()
        stats.total_in_db = db.game_count(conn)
    finally:
        conn.close()
    if stats.total_in_db == 0:
        player.db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            player.db_path.with_name(player.db_path.name + suffix).unlink(missing_ok=True)
    return stats
