"""Turn one PGN game into rows for the database. No network code in here."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import chess
import chess.pgn

MATE_SCORE = 10000


class SkipGame(Exception):
    """Raised for games we deliberately do not store (variants, odd setups, ...)."""


@dataclass
class ParsedMove:
    ply: int
    san: str
    uci: str
    epd_before: str
    epd_after: str
    clock: float | None
    eval_after: int | None  # from PGN comments (Lichess), White's point of view


@dataclass
class ParsedGame:
    url: str
    played_at: str | None
    end_time: int | None
    white: str
    black: str
    color: str
    player_rating: int | None
    opponent_rating: int | None
    result: str
    termination: str
    time_control: str
    time_class: str | None
    base_seconds: int | None
    increment: int | None
    eco: str
    opening: str
    pgn: str
    moves: list[ParsedMove] = field(default_factory=list)

    @property
    def ply_count(self) -> int:
        return len(self.moves)


def position_key(board: chess.Board) -> str:
    """FEN without the move counters, so transpositions match."""
    return board.epd()


def parse_time_control(tc: str | None) -> tuple[int | None, int | None]:
    """'180+2' -> (180, 2); '600' -> (600, 0); daily ('1/86400') or '-' -> (None, None)."""
    if not tc:
        return None, None
    tc = tc.strip()
    m = re.fullmatch(r"(\d+)(?:\+(\d+))?", tc)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2) or 0)


def time_class_for(base: int | None, inc: int | None) -> str | None:
    """Lichess-style speed buckets from an estimated game duration."""
    if base is None:
        return "daily"
    estimate = base + 40 * (inc or 0)
    if estimate < 180:
        return "bullet"
    if estimate < 480:
        return "blitz"
    if estimate < 1500:
        return "rapid"
    return "classical"


_APOSTROPHES = {
    "Kings": "King's",
    "Queens": "Queen's",
    "Bishops": "Bishop's",
    "Petrovs": "Petrov's",
    "Alekhines": "Alekhine's",
    "Owens": "Owen's",
    "Birds": "Bird's",
}


def clean_opening_name(raw: str | None, eco: str = "") -> str:
    """Make a readable opening name from a Lichess 'Opening' header or a Chess.com ECOUrl."""
    if not raw:
        return eco or "Unknown opening"
    raw = raw.strip()
    if raw.startswith("http"):
        slug = raw.rstrip("/").split("/")[-1]
        words = []
        for token in slug.split("-"):
            if re.match(r"^\d+\.", token) or not token:
                break  # the rest is a move sequence such as 3...Bc5
            words.append(_APOSTROPHES.get(token, token))
        name = " ".join(words)
    else:
        name = raw.split(",")[0]  # drop sub-variations after a comma
    name = re.sub(r"\s+", " ", name).strip(" :")
    return name or eco or "Unknown opening"


_FAMILY_END = {"Opening", "Defense", "Defence", "Game", "Gambit", "Attack", "System", "Countergambit"}


def opening_family(name: str) -> str:
    """'Sicilian Defense: Najdorf Variation' / 'Sicilian Defense Najdorf Variation' -> 'Sicilian Defense'."""
    if ":" in name:
        return name.split(":")[0].strip()
    words = name.split()
    for i, w in enumerate(words):
        if w in _FAMILY_END:
            return " ".join(words[: i + 1])
    return " ".join(words[:3]) if words else name


def _result_for(color: str, raw: str) -> str:
    if raw == "1/2-1/2":
        return "draw"
    if raw == "1-0":
        return "win" if color == "white" else "loss"
    if raw == "0-1":
        return "win" if color == "black" else "loss"
    raise SkipGame(f"unfinished game (result {raw!r})")


def _termination(headers: chess.pgn.Headers, board: chess.Board, result: str) -> str:
    text = headers.get("Termination", "").lower()
    if board.is_checkmate():
        return "checkmate"
    if result == "draw":
        if board.is_stalemate():
            return "stalemate"
        if "repetition" in text or board.can_claim_threefold_repetition():
            return "repetition"
        if "insufficient" in text or "timeout vs insufficient" in text:
            return "draw"
        return "draw"
    if "time" in text:
        return "timeout"
    if "abandon" in text:
        return "abandoned"
    return "resignation"


def _date_and_time(headers: chess.pgn.Headers) -> tuple[str | None, int | None]:
    date = headers.get("UTCDate") or headers.get("Date") or ""
    clock = headers.get("UTCTime") or "00:00:00"
    try:
        dt = datetime.strptime(f"{date} {clock}", "%Y.%m.%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, None
    return dt.date().isoformat(), int(dt.timestamp())


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "", "?") else None
    except ValueError:
        return None


def parse_game(
    pgn_text: str,
    username: str,
    *,
    url: str | None = None,
    time_class: str | None = None,
    end_time: int | None = None,
) -> ParsedGame:
    """Parse a single PGN game played by ``username``. Raises SkipGame for unusable games."""
    game = chess.pgn.read_game(io.StringIO(pgn_text))
    if game is None:
        raise SkipGame("empty PGN")
    if game.errors:
        raise SkipGame(f"PGN error: {game.errors[0]}")
    headers = game.headers
    variant = headers.get("Variant", "Standard").lower()
    if variant not in ("standard", "chess", "from position") or headers.get("SetUp") == "1":
        raise SkipGame("not a standard game from the starting position")

    white, black = headers.get("White", "?"), headers.get("Black", "?")
    who = username.lower()
    if white.lower() == who:
        color = "white"
    elif black.lower() == who:
        color = "black"
    else:
        raise SkipGame(f"{username} did not play in this game")

    board = game.board()
    moves: list[ParsedMove] = []
    for ply, node in enumerate(game.mainline()):
        before = position_key(board)
        san = board.san(node.move)
        uci = node.move.uci()
        board.push(node.move)
        pov = node.eval()
        eval_after = pov.white().score(mate_score=MATE_SCORE) if pov is not None else None
        moves.append(ParsedMove(ply, san, uci, before, position_key(board), node.clock(), eval_after))
    if not moves:
        raise SkipGame("no moves")

    result = _result_for(color, headers.get("Result", "*"))
    played_at, header_end = _date_and_time(headers)
    tc = headers.get("TimeControl", "")
    base, inc = parse_time_control(tc)
    eco = headers.get("ECO", "") or ""
    opening = clean_opening_name(headers.get("Opening") or headers.get("ECOUrl"), eco)

    return ParsedGame(
        url=url or headers.get("Link") or headers.get("Site", ""),
        played_at=played_at,
        end_time=end_time or header_end,
        white=white,
        black=black,
        color=color,
        player_rating=_int_or_none(headers.get(f"{color.title()}Elo")),
        opponent_rating=_int_or_none(headers.get("BlackElo" if color == "white" else "WhiteElo")),
        result=result,
        termination=_termination(headers, board, result),
        time_control=tc,
        time_class=time_class or time_class_for(base, inc),
        base_seconds=base,
        increment=inc,
        eco=eco,
        opening=opening,
        pgn=pgn_text.strip() + "\n",
        moves=moves,
    )


def store_game(conn, site: str, game: ParsedGame) -> bool:
    """Insert a parsed game. Returns False if it was already stored."""
    if not game.url:
        raise SkipGame("game has no URL to de-duplicate on")
    if conn.execute("SELECT 1 FROM games WHERE url = ?", (game.url,)).fetchone():
        return False
    cur = conn.execute(
        """INSERT INTO games (site, url, played_at, end_time, white, black, color,
               player_rating, opponent_rating, result, termination, time_control, time_class,
               base_seconds, increment, eco, opening, ply_count, pgn)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            site,
            game.url,
            game.played_at,
            game.end_time,
            game.white,
            game.black,
            game.color,
            game.player_rating,
            game.opponent_rating,
            game.result,
            game.termination,
            game.time_control,
            game.time_class,
            game.base_seconds,
            game.increment,
            game.eco,
            game.opening,
            game.ply_count,
            game.pgn,
        ),
    )
    game_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO moves (game_id, ply, san, uci, epd_before, epd_after, clock_seconds) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(game_id, m.ply, m.san, m.uci, m.epd_before, m.epd_after, m.clock) for m in game.moves],
    )
    # Evals that came with the PGN (Lichess server analysis). No best move, so the
    # engine pass will still fill these in, but reports can use them straight away.
    conn.executemany(
        "INSERT OR IGNORE INTO evals (epd, cp, best_uci, depth, source) VALUES (?, ?, NULL, 0, 'lichess')",
        [(m.epd_after, m.eval_after) for m in game.moves if m.eval_after is not None],
    )
    return True
