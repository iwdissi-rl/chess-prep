"""Where chess-prep keeps its files.

Everything lives in a per-user data folder chosen by platformdirs, so nothing is
written into the source checkout. Set ``CHESSPREP_HOME`` to put everything in one
folder of your choice instead (handy for testing or a portable install).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import platformdirs

APP_NAME = "chess-prep"
SITES = ("chesscom", "lichess")
SITE_LABELS = {"chesscom": "Chess.com", "lichess": "Lichess"}


def _home_override() -> Path | None:
    value = os.environ.get("CHESSPREP_HOME")
    return Path(value).expanduser() if value else None


def data_dir() -> Path:
    home = _home_override()
    path = home if home else Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    home = _home_override()
    if home:
        return home / "config.json"
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)) / "config.json"


def engines_dir() -> Path:
    path = data_dir() / "engines"
    path.mkdir(parents=True, exist_ok=True)
    return path


def players_dir() -> Path:
    path = data_dir() / "players"
    path.mkdir(parents=True, exist_ok=True)
    return path


def practice_db() -> Path:
    return data_dir() / "practice.db"


def reports_root() -> Path:
    home = _home_override()
    if home:
        return home / "reports"
    return Path(platformdirs.user_documents_dir()) / APP_NAME


def _slug(username: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "_", username.strip().lower())


@dataclass(frozen=True)
class Player:
    """A player account on one site. The same name on two sites is two different people."""

    site: str
    username: str

    def __post_init__(self) -> None:
        if self.site not in SITES:
            raise ValueError(f"Unknown site {self.site!r}. Use one of: {', '.join(SITES)}")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,30}", self.username.strip()):
            raise ValueError(
                f"{self.username!r} does not look like a valid username (letters, digits, '-' and '_' only)."
            )

    @property
    def key(self) -> str:
        return f"{self.site}-{_slug(self.username)}"

    @property
    def label(self) -> str:
        return f"{self.username} ({SITE_LABELS[self.site]})"

    @property
    def db_path(self) -> Path:
        return players_dir() / f"{self.key}.db"

    def out_dir(self, override: Path | None = None) -> Path:
        path = Path(override).expanduser() if override else reports_root() / self.key
        path.mkdir(parents=True, exist_ok=True)
        return path


def known_players() -> list[Player]:
    """Players that already have a local database."""
    found: list[Player] = []
    for db in sorted(players_dir().glob("*.db")):
        site, _, name = db.stem.partition("-")
        if site not in SITES or not name:
            continue
        stored = _stored_username(db)
        try:
            found.append(Player(site, stored or name))
        except ValueError:
            continue
    return found


def _stored_username(db: Path) -> str | None:
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'username'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def resolve_player(username: str, site: str | None) -> Player:
    """Find which site a username belongs to when ``--site`` was not given."""
    if site:
        return Player(site, username)
    matches = [p for p in known_players() if _slug(p.username) == _slug(username)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(
            f"No games stored for {username!r} yet. "
            f"Run: chessprep fetch {username} --site chesscom   (or --site lichess)"
        )
    raise LookupError(f"{username!r} exists for more than one site. Add --site chesscom or --site lichess.")
