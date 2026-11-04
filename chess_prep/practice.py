"""Play practice games against the clone in the terminal."""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import chess
import chess.engine
import chess.pgn
from rich.console import Console
from rich.table import Table
from rich.text import Text

from chess_prep.clone import CloneBot, OpponentBook
from chess_prep.engines import EngineSet
from chess_prep.paths import Player, practice_db
from chess_prep.pgnparse import MATE_SCORE

GLYPHS = {
    "K": "♔",
    "Q": "♕",
    "R": "♖",
    "B": "♗",
    "N": "♘",
    "P": "♙",
    "k": "♚",
    "q": "♛",
    "r": "♜",
    "b": "♝",
    "n": "♞",
    "p": "♟",
}
LIGHT, DARK, HL_LIGHT, HL_DARK = "#e9d7b4", "#b58863", "#f2e27a", "#d4b84a"

PRACTICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS practice_games (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    player_key  TEXT NOT NULL,
    played_at   TEXT NOT NULL,
    your_color  TEXT NOT NULL,
    result      TEXT NOT NULL,   -- win / loss / draw / unfinished
    plies       INTEGER,
    book_exit   INTEGER,         -- ply where the clone left the opponent's book
    pgn         TEXT
);
"""

HELP = (
    "Type a move like [bold]e4[/], [bold]Nf3[/], [bold]O-O[/] or [bold]e2e4[/]. "
    "Commands: [bold]moves[/] (legal moves), [bold]hint[/], [bold]undo[/], [bold]flip[/], "
    "[bold]resign[/], [bold]quit[/]."
)


def render_board(
    board: chess.Board, perspective: chess.Color, last: chess.Move | None = None, *, color: bool = True
) -> Text:
    """Board as Rich text. Without colour, white pieces use outline glyphs so the sides stay distinct."""
    text = Text()
    ranks = range(7, -1, -1) if perspective == chess.WHITE else range(8)
    files = range(8) if perspective == chess.WHITE else range(7, -1, -1)
    hl = {last.from_square, last.to_square} if last else set()
    for r in ranks:
        text.append(f" {r + 1} ", style="dim")
        for f in files:
            sq = chess.square(f, r)
            light = (r + f) % 2 == 1
            bg = (HL_LIGHT if light else HL_DARK) if sq in hl else (LIGHT if light else DARK)
            piece = board.piece_at(sq)
            if not color:
                glyph = GLYPHS[piece.symbol()] if piece else ("." if not light else " ")
                text.append(f" {glyph} ")
                continue
            glyph = GLYPHS[piece.symbol().lower()] if piece else " "
            fg = "#ffffff" if piece and piece.color == chess.WHITE else "#000000"
            text.append(f" {glyph} ", style=f"bold {fg} on {bg}")
        text.append("\n")
    names = "abcdefgh" if perspective == chess.WHITE else "hgfedcba"
    text.append("    " + "  ".join(names), style="dim")
    return text


@dataclass
class GameSummary:
    your_color: str
    result: str
    plies: int
    book_exit: int | None
    pgn: str
    sources: dict[str, int] = field(default_factory=dict)
    your_mistakes: list[dict] = field(default_factory=list)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(practice_db()))
    conn.row_factory = sqlite3.Row
    conn.executescript(PRACTICE_SCHEMA)
    return conn


def save_game(player: Player, summary: GameSummary) -> int:
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO practice_games (player_key, played_at, your_color, result, plies, book_exit, pgn) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                player.key,
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                summary.your_color,
                summary.result,
                summary.plies,
                summary.book_exit,
                summary.pgn,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def history(player: Player) -> list[sqlite3.Row]:
    conn = _db()
    try:
        return conn.execute("SELECT * FROM practice_games WHERE player_key = ? ORDER BY id", (player.key,)).fetchall()
    finally:
        conn.close()


def history_table(player: Player) -> Table | None:
    rows = history(player)
    if not rows:
        return None
    table = Table(title=f"Practice games vs {player.label} clone", header_style="bold cyan")
    for col in ("#", "Date", "You", "Result", "Moves", "Left book at"):
        table.add_column(col)
    style = {"win": "green", "loss": "red", "draw": "yellow", "unfinished": "dim"}
    for i, r in enumerate(rows, 1):
        exit_move = f"move {r['book_exit'] // 2 + 1}" if r["book_exit"] is not None else "-"
        table.add_row(
            str(i),
            r["played_at"],
            r["your_color"],
            f"[{style.get(r['result'], '')}]{r['result']}[/]",
            str((r["plies"] + 1) // 2),
            exit_move,
        )
    done = [r for r in rows if r["result"] in ("win", "loss", "draw")]
    if done:
        pts = sum({"win": 1, "draw": 0.5}.get(r["result"], 0) for r in done)
        table.caption = f"Your score: {pts:g}/{len(done)} ({pts / len(done) * 100:.0f}%)"
    return table


def _parse_move(board: chess.Board, raw: str) -> chess.Move | None:
    raw = raw.strip()
    try:
        return board.parse_san(raw)
    except ValueError:
        pass
    try:
        mv = chess.Move.from_uci(raw.lower())
    except ValueError:
        return None
    if mv in board.legal_moves:
        return mv
    promo = chess.Move(mv.from_square, mv.to_square, promotion=chess.QUEEN)
    return promo if promo in board.legal_moves else None


def _review(stockfish_path, moves: list[chess.Move], your_color: chess.Color, depth: int = 10) -> list[dict]:
    """Your three biggest mistakes, by Stockfish."""
    if not stockfish_path or not moves:
        return []
    out = []
    with chess.engine.SimpleEngine.popen_uci(str(stockfish_path)) as eng:
        eng.configure({"Threads": 1, "Hash": 32})
        board = chess.Board()
        for ply, mv in enumerate(moves):
            if board.turn == your_color:
                before = eng.analyse(board, chess.engine.Limit(depth=depth))
                best = before.get("pv", [None])[0]
                cp_before = before["score"].pov(your_color).score(mate_score=MATE_SCORE)
                san = board.san(mv)
                best_san = board.san(best) if best else None
                board.push(mv)
                after = eng.analyse(board, chess.engine.Limit(depth=depth))
                cp_after = after["score"].pov(your_color).score(mate_score=MATE_SCORE)
                loss = cp_before - cp_after
                if loss >= 80 and best_san != san:
                    label = f"{ply // 2 + 1}.{san}" if ply % 2 == 0 else f"{ply // 2 + 1}...{san}"
                    out.append({"move": label, "loss_cp": loss, "best": best_san})
            else:
                board.push(mv)
    return sorted(out, key=lambda x: -x["loss_cp"])[:3]


def play_game(
    player: Player,
    book: OpponentBook,
    engines: EngineSet,
    your_color: str,
    *,
    console: Console,
    scripted: list[str] | None = None,
    auto: int | None = None,
    input_fn: Callable[[str], str] | None = None,
    seed: int | None = None,
    show_board: bool = True,
    review: bool = True,
) -> GameSummary:
    """Play one game. ``scripted`` / ``auto`` make it non-interactive (for demos and tests)."""
    you = chess.WHITE if your_color == "white" else chess.BLACK
    clone_color = "black" if your_color == "white" else "white"
    board = chess.Board()
    history_san: list[str] = []
    sources: dict[str, int] = {}
    book_exit: int | None = None
    perspective = you
    rng = random.Random(seed)
    non_interactive = scripted is not None or auto is not None
    queue = list(scripted or [])
    auto_left = auto or 0
    result = "unfinished"
    ask = input_fn or (lambda prompt: console.input(prompt))
    auto_engine = None
    if auto and engines.stockfish:
        auto_engine = chess.engine.SimpleEngine.popen_uci(str(engines.stockfish))
        auto_engine.configure({"Threads": 1, "Hash": 16})

    use_color = console.color_system is not None

    def show() -> None:
        if show_board:
            last = board.peek() if board.move_stack else None
            console.print(render_board(board, perspective, last, color=use_color))

    with CloneBot(book, clone_color, engines, seed=seed) as bot:
        for note in bot.notes:
            console.print(f"[dim]Clone engine: {note}[/]")
        if not non_interactive:
            console.print(HELP)
        try:
            while not board.is_game_over(claim_draw=True):
                if board.turn == you:
                    if non_interactive and not queue and auto_left <= 0:
                        break
                    show()
                    if non_interactive:
                        if queue:
                            raw = queue.pop(0)
                            mv = _parse_move(board, raw)
                            if mv is None:
                                raise ValueError(f"Scripted move {raw!r} is not legal here ({board.fen()})")
                        elif auto_left > 0:
                            auto_left -= 1
                            if auto_engine:
                                mv = auto_engine.play(board, chess.engine.Limit(depth=4)).move
                            else:
                                mv = rng.choice(list(board.legal_moves))
                        else:
                            break
                    else:
                        raw = ask("[bold cyan]Your move:[/] ").strip()
                        cmd = raw.lower()
                        if cmd in ("quit", "exit", "q"):
                            break
                        if cmd == "resign":
                            result = "loss"
                            break
                        if cmd in ("help", "?"):
                            console.print(HELP)
                            continue
                        if cmd == "moves":
                            console.print(", ".join(sorted(board.san(m) for m in board.legal_moves)))
                            continue
                        if cmd == "flip":
                            perspective = not perspective
                            continue
                        if cmd == "undo":
                            if len(board.move_stack) >= 2:
                                board.pop()
                                board.pop()
                                history_san = history_san[:-2]
                            continue
                        if cmd == "hint":
                            if engines.stockfish:
                                with chess.engine.SimpleEngine.popen_uci(str(engines.stockfish)) as e:
                                    best = e.play(board, chess.engine.Limit(depth=14)).move
                                console.print(f"Stockfish suggests [bold]{board.san(best)}[/]")
                            else:
                                console.print("[yellow]Hints need Stockfish. Run: chessprep setup-engines[/]")
                            continue
                        mv = _parse_move(board, raw)
                        if mv is None:
                            console.print(
                                f"[red]'{raw}' is not a legal move here.[/] Type [bold]moves[/] to list them."
                            )
                            continue
                    san = board.san(mv)
                    board.push(mv)
                    history_san.append(san)
                    console.print(f"You played [bold]{san}[/]")
                else:
                    mv, info = bot.pick(board, history_san)
                    san = board.san(mv)
                    if info.source != "book" and book_exit is None:
                        book_exit = len(history_san)
                    board.push(mv)
                    history_san.append(san)
                    sources[info.source] = sources.get(info.source, 0) + 1
                    tag = {"book": "green", "history": "cyan", "maia": "magenta", "stockfish": "yellow"}.get(
                        info.source, "red"
                    )
                    console.print(
                        f"Clone played [bold]{san}[/]  [{tag}]({info.source}"
                        f"{f', {info.confidence:.0%}' if info.source in ('book', 'history') else ''})[/]"
                    )
        finally:
            if auto_engine:
                auto_engine.quit()

    if board.is_game_over(claim_draw=True):
        outcome = board.outcome(claim_draw=True)
        if outcome.winner is None:
            result = "draw"
        else:
            result = "win" if outcome.winner == you else "loss"
    show()

    game = chess.pgn.Game.from_board(board)
    game.headers["Event"] = f"Practice vs {player.label} clone"
    game.headers["Date"] = datetime.now().strftime("%Y.%m.%d")
    game.headers["White"] = "You" if you == chess.WHITE else f"{player.username} clone"
    game.headers["Black"] = "You" if you == chess.BLACK else f"{player.username} clone"
    if result == "unfinished":
        game.headers["Result"] = "*"
    elif result == "loss" and not board.is_game_over(claim_draw=True):
        game.headers["Result"] = "0-1" if you == chess.WHITE else "1-0"
    summary = GameSummary(
        your_color=your_color,
        result=result,
        plies=len(board.move_stack),
        book_exit=book_exit,
        pgn=str(game),
        sources=sources,
    )
    if review and board.move_stack:
        summary.your_mistakes = _review(engines.stockfish, list(board.move_stack), you)
    return summary
