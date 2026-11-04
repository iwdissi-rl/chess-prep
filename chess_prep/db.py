"""SQLite storage: one database file per scouted player."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS games (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    site            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    played_at       TEXT,          -- ISO date (YYYY-MM-DD)
    end_time        INTEGER,       -- unix seconds, used for recency weighting
    white           TEXT,
    black           TEXT,
    color           TEXT NOT NULL, -- colour the scouted player had
    player_rating   INTEGER,
    opponent_rating INTEGER,
    result          TEXT NOT NULL, -- win / loss / draw, from the player's side
    termination     TEXT,          -- checkmate / resignation / timeout / abandoned / draw
    time_control    TEXT,
    time_class      TEXT,          -- bullet / blitz / rapid / classical / daily
    base_seconds    INTEGER,
    increment       INTEGER,
    eco             TEXT,
    opening         TEXT,
    ply_count       INTEGER,
    pgn             TEXT
);
CREATE TABLE IF NOT EXISTS moves (
    game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    ply           INTEGER NOT NULL, -- 0 = White's first move
    san           TEXT NOT NULL,
    uci           TEXT NOT NULL,
    epd_before    TEXT NOT NULL,    -- position key (FEN without move counters)
    epd_after     TEXT NOT NULL,
    clock_seconds REAL,             -- clock of the side that just moved, after the move
    PRIMARY KEY (game_id, ply)
);
CREATE TABLE IF NOT EXISTS evals (
    epd      TEXT PRIMARY KEY,
    cp       INTEGER,  -- White's point of view; mates are +/-(10000 - distance)
    best_uci TEXT,
    depth    INTEGER,
    source   TEXT      -- stockfish / lichess / terminal
);
CREATE INDEX IF NOT EXISTS idx_moves_before ON moves(epd_before);
CREATE INDEX IF NOT EXISTS idx_games_color ON games(color);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def game_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]


def eval_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM evals WHERE source = 'stockfish'").fetchone()[0]


def is_player_move(color: str, ply: int) -> bool:
    """True when the move at ``ply`` was made by the scouted player."""
    return (ply % 2 == 0) == (color == "white")
