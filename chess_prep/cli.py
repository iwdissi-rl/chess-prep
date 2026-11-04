"""Command line interface: ``chessprep``. Run it with no arguments for a guided menu."""

from __future__ import annotations

import contextlib
import os
import random
import sys
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from chess_prep import __version__, config, db, engines
from chess_prep.paths import SITE_LABELS, Player, known_players, resolve_player

console = Console(highlight=False)
err = Console(stderr=True, highlight=False)

app = typer.Typer(
    name="chessprep",
    help="Scout a chess opponent from their public Chess.com or Lichess games, "
    "make a one-page cheatsheet, and practise against a bot that plays like them.",
    no_args_is_help=False,
    add_completion=False,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)
config_app = typer.Typer(help="Show or change saved settings (engine paths).", no_args_is_help=True)
app.add_typer(config_app, name="config")


class Site(str, Enum):
    chesscom = "chesscom"
    lichess = "lichess"


class ColorChoice(str, Enum):
    white = "white"
    black = "black"
    random = "random"


SiteOpt = Annotated[
    Site | None, typer.Option("--site", "-s", help="chesscom or lichess (needed if the name exists on both).")
]
OutOpt = Annotated[
    Path | None, typer.Option("--out", "-o", help="Folder for reports (default: Documents/chess-prep/<site>-<user>).")
]
UserArg = Annotated[str, typer.Argument(help="Username on Chess.com or Lichess.", show_default=False)]


class UserError(Exception):
    """An error with a message meant for the user (no traceback)."""


@contextmanager
def friendly_errors():
    from chess_prep.fetch import FetchError

    try:
        yield
    except (UserError, LookupError, ValueError, FetchError, engines.EngineError) as exc:
        err.print(Panel(str(exc), title="[bold red]Error", border_style="red", expand=False))
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        err.print("\n[yellow]Stopped.[/]")
        raise typer.Exit(130) from None


def _player(user: str, site: Site | None) -> Player:
    return resolve_player(user, site.value if site else None)


def _require_games(player: Player) -> None:
    if not player.db_path.exists():
        raise UserError(
            f"No games stored for {player.label} yet.\nRun: chessprep fetch {player.username} --site {player.site}"
        )


def _version_cb(value: bool) -> None:
    if value:
        console.print(f"chess-prep {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=_version_cb, is_eager=True, help="Show the version.")
    ] = False,
) -> None:
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            interactive()
        else:
            console.print(ctx.get_help())


# ------------------------------------------------------------------ fetch


def do_fetch(player: Player, max_games: int, speeds: set[str] | None) -> None:
    from chess_prep.fetch import fetch_games

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]Downloading games[/] from " + SITE_LABELS[player.site]),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("fetch", total=max_games)
        stats = fetch_games(
            player,
            max_games,
            speeds,
            on_progress=lambda s: progress.update(task, completed=s.new + s.already_had + s.skipped),
        )
    if stats.total_in_db == 0:
        raise UserError(
            f"{player.label} has no standard games we could use"
            + (f" for speed {', '.join(sorted(speeds))}." if speeds else ".")
        )
    console.print(
        f"[green]Done.[/] {stats.new} new games, {stats.already_had} already stored, "
        f"{stats.skipped} skipped (variants or unfinished). "
        f"[bold]{stats.total_in_db}[/] games stored for {player.label}."
    )


@app.command()
def fetch(
    user: UserArg,
    site: Annotated[Site, typer.Option("--site", "-s", help="Which site the account is on.")] = Site.chesscom,
    max_games: Annotated[
        int, typer.Option("--max", "-n", min=1, max=5000, help="How many recent games to download.")
    ] = 200,
    speed: Annotated[
        list[str] | None,
        typer.Option(
            "--speed", help="Only these speeds: bullet, blitz, rapid, classical, daily. Repeat or comma-separate."
        ),
    ] = None,
) -> None:
    """Download a player's most recent public games."""
    with friendly_errors():
        speeds = {s.strip().lower() for item in (speed or []) for s in item.split(",") if s.strip()} or None
        do_fetch(Player(site.value, user), max_games, speeds)


# ---------------------------------------------------------------- analyze


def do_engine_pass(player: Player, depth: int, workers: int | None) -> bool:
    from chess_prep.analyze import run_analysis

    sf = engines.find_stockfish()
    if not sf:
        console.print(
            "[yellow]Stockfish not found, so mistakes cannot be measured.[/] "
            f"Install it with: [bold]{engines.install_hint_stockfish()}[/]"
        )
        return False
    with Progress(
        SpinnerColumn(),
        TextColumn(f"[bold]Stockfish[/] depth {depth}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("positions"),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("analyse", total=None)
        stats = run_analysis(
            player.db_path,
            sf,
            depth=depth,
            workers=workers,
            on_start=lambda n: progress.update(task, total=n),
            on_progress=lambda n: progress.update(task, completed=n),
        )
    console.print(
        f"[green]Engine pass done.[/] {stats.analysed} positions analysed, {stats.cached} already known"
        + (f", [yellow]{stats.errors} failed[/]" if stats.errors else "")
        + "."
    )
    return True


def do_report(player: Player, out: Path | None) -> dict:
    from chess_prep.report import build_report, write_report

    rep = build_report(player.db_path, player)
    path = write_report(rep, player.out_dir(out))
    show_report(rep)
    console.print(f"Report saved: [bold]{path}[/]")
    return rep


def show_report(rep: dict) -> None:
    s = rep["summary"]
    head = Table.grid(padding=(0, 2))
    head.add_row("Games", f"{s['games']} ({s['first_date']} to {s['last_date']})")
    if s.get("recent_rating"):
        head.add_row("Recent rating", str(s["recent_rating"]))
    head.add_row("Overall", f"{s['wins']}W {s['draws']}D {s['losses']}L  ({s['score_pct']}%)")
    for c in ("white", "black"):
        r = s["by_color"][c]
        head.add_row(f"As {c}", f"{r['wins']}W {r['draws']}D {r['losses']}L  ({r['score_pct']}%)")
    console.print(Panel(head, title=f"[bold]{rep['player']['label']}", border_style="cyan", expand=False))

    t = Table(title="Main openings", header_style="bold cyan")
    for col in ("They play", "Opening", "Games", "They score"):
        t.add_column(col, justify="right" if col in ("Games", "They score") else "left")
    for c in ("white", "black"):
        for op in rep["openings"][c][:3]:
            pct = op["score_pct"]
            color = "red" if pct >= 60 else "green" if pct <= 40 else "white"
            t.add_row(c, op["name"], str(op["games"]), f"[{color}]{pct}%[/]")
    console.print(t)

    m = rep["mistakes"]
    if m["has_evals"]:
        console.print(
            f"Mistakes: [bold]{m['per_100_moves']['blunders']}[/] blunders and "
            f"{m['per_100_moves']['mistakes']} mistakes per 100 moves"
            + (f"; most common: [bold]{', '.join(list(m['motifs'])[:3])}[/]" if m["motifs"] else "")
        )
    console.print(
        Panel("\n".join(f"- {n}" for n in rep["notes"]), title="[bold]Game plan", border_style="green", expand=False)
    )


@app.command()
def analyze(
    user: UserArg,
    site: SiteOpt = None,
    depth: Annotated[int, typer.Option(min=4, max=40, help="Stockfish search depth per position.")] = 12,
    workers: Annotated[
        int | None, typer.Option(min=1, help="Parallel Stockfish processes (default: half your CPU cores, max 8).")
    ] = None,
    no_engine: Annotated[
        bool, typer.Option("--no-engine", help="Skip Stockfish; report results, openings and clock habits only.")
    ] = False,
    out: OutOpt = None,
) -> None:
    """Find weaknesses: engine pass over every position, then a weakness report."""
    with friendly_errors():
        player = _player(user, site)
        _require_games(player)
        if not no_engine:
            do_engine_pass(player, depth, workers)
        do_report(player, out)


# ------------------------------------------------------------------- prep


def do_prep(player: Player, out: Path | None, depth: int, use_engine: bool) -> dict:
    from chess_prep.prep import build_prep, write_prep
    from chess_prep.report import build_report, write_report

    out_dir = player.out_dir(out)
    rep = build_report(player.db_path, player)
    write_report(rep, out_dir)
    sf = engines.find_stockfish() if use_engine else None
    if use_engine and not sf:
        console.print("[yellow]Stockfish not found: the prep tree will have no engine suggestions.[/]")
    with console.status("[bold]Building opening prep" + (" with Stockfish" if sf else "") + "..."):
        prep = build_prep(player.db_path, rep, sf, depth=depth)
    md, _ = write_prep(prep, out_dir)
    t = Table(title="Your decision points", header_style="bold cyan")
    for col in ("They play", "Opening", "After", "Play", "Surprise"):
        t.add_column(col)
    for c in ("white", "black"):
        for op in prep[c]:
            d = op["decision"] or {}
            rec = (d.get("recommended") or {}).get("move", "-")
            sur = (d.get("surprise") or {}).get("move", "-")
            if sur == rec:
                sur = "(same, never faced)"
            t.add_row(c, op["name"], d.get("line") or "-", f"[bold]{rec}[/]", sur)
    console.print(t)
    console.print(f"Prep tree saved: [bold]{md}[/]")
    return prep


@app.command()
def prep(
    user: UserArg,
    site: SiteOpt = None,
    depth: Annotated[int, typer.Option(min=6, max=40, help="Stockfish depth for the suggested moves.")] = 18,
    no_engine: Annotated[
        bool, typer.Option("--no-engine", help="Build the tree without Stockfish suggestions.")
    ] = False,
    out: OutOpt = None,
) -> None:
    """Build an opening tree of their main lines with suggested replies."""
    with friendly_errors():
        player = _player(user, site)
        _require_games(player)
        do_prep(player, out, depth, not no_engine)


# ------------------------------------------------------------- cheatsheet


def do_cheatsheet(player: Player, out: Path | None) -> dict[str, Path]:
    from chess_prep.prep import load_prep
    from chess_prep.report import build_report, write_report
    from chess_prep.sheet import write_all

    out_dir = player.out_dir(out)
    rep = build_report(player.db_path, player)
    write_report(rep, out_dir)
    prep_data = load_prep(out_dir)
    if prep_data is None:
        console.print(
            "[dim]No prep tree yet, so the sheet has no suggested replies. Run 'chessprep prep' first for those.[/]"
        )
    files = write_all(rep, prep_data, out_dir)
    t = Table.grid(padding=(0, 2))
    for kind, path in files.items():
        t.add_row(f"[bold]{kind.upper()}[/]", str(path))
    console.print(Panel(t, title="[bold green]Cheatsheet ready", border_style="green", expand=False))
    return files


@app.command()
def cheatsheet(user: UserArg, site: SiteOpt = None, out: OutOpt = None) -> None:
    """Make the one-page cheatsheet (PDF, Markdown and text)."""
    with friendly_errors():
        player = _player(user, site)
        _require_games(player)
        do_cheatsheet(player, out)


@app.command()
def run(
    user: UserArg,
    site: Annotated[Site, typer.Option("--site", "-s", help="Which site the account is on.")] = Site.chesscom,
    max_games: Annotated[
        int, typer.Option("--max", "-n", min=1, max=5000, help="How many recent games to download.")
    ] = 200,
    depth: Annotated[int, typer.Option(min=4, max=40, help="Stockfish depth for the engine pass.")] = 12,
    out: OutOpt = None,
) -> None:
    """Do everything: fetch, analyze, prep and cheatsheet."""
    with friendly_errors():
        player = Player(site.value, user)
        console.rule(f"[bold]1/4 Fetch {player.label}")
        do_fetch(player, max_games, None)
        console.rule("[bold]2/4 Analyze")
        has_sf = do_engine_pass(player, depth, None)
        do_report(player, out)
        console.rule("[bold]3/4 Prep")
        do_prep(player, out, 18, has_sf)
        console.rule("[bold]4/4 Cheatsheet")
        do_cheatsheet(player, out)


# --------------------------------------------------------------- practice


def do_practice(
    player: Player,
    color: str,
    games: int,
    moves: str | None,
    auto: int | None,
    seed: int | None,
    show_board: bool = True,
) -> None:
    from chess_prep.clone import OpponentBook
    from chess_prep.practice import history_table, play_game, save_game

    book = OpponentBook.from_db(player.db_path)
    found = engines.detect()
    rng = random.Random(seed)
    scripted = moves.replace(",", " ").split() if moves else None
    for i in range(games):
        your = color if color != "random" else rng.choice(["white", "black"])
        if color != "random" and games > 1 and i % 2 == 1:
            your = "black" if color == "white" else "white"
        console.rule(f"[bold]Game {i + 1}/{games}: you are {your} vs {player.username} clone")
        summary = play_game(
            player,
            book,
            found,
            your,
            console=console,
            scripted=scripted,
            auto=auto,
            seed=None if seed is None else seed + i,
            show_board=show_board,
        )
        save_game(player, summary)
        exit_txt = (
            f"left their book at move {summary.book_exit // 2 + 1}"
            if summary.book_exit is not None
            else "stayed in their book"
        )
        style = {"win": "green", "loss": "red", "draw": "yellow"}.get(summary.result, "dim")
        lines = [
            f"Result: [{style}]{summary.result}[/] after {(summary.plies + 1) // 2} moves",
            f"Clone {exit_txt}; move sources: " + ", ".join(f"{k} {v}" for k, v in summary.sources.items()),
        ]
        for m in summary.your_mistakes:
            lines.append(f"Your {m['move']} lost {m['loss_cp'] / 100:.1f} pawns; {m['best']} was better")
        console.print(Panel("\n".join(lines), title="[bold]Game over", expand=False))
        console.print("[dim]PGN:[/]")
        console.print(summary.pgn, markup=False)
    table = history_table(player)
    if table:
        console.print(table)


@app.command()
def practice(
    user: UserArg,
    site: SiteOpt = None,
    color: Annotated[ColorChoice, typer.Option("--color", "-c", help="Your colour.")] = ColorChoice.white,
    games: Annotated[int, typer.Option(min=1, max=20, help="Games to play (colours alternate).")] = 1,
    moves: Annotated[
        str | None,
        typer.Option("--moves", help='Play these moves for you, e.g. "e4 Nf3 Bc4", then stop (no typing needed).'),
    ] = None,
    auto: Annotated[
        int | None,
        typer.Option("--auto", min=1, help="Let Stockfish (or random moves) play N of your moves, then stop."),
    ] = None,
    seed: Annotated[int | None, typer.Option(help="Random seed, for repeatable games.")] = None,
    no_board: Annotated[bool, typer.Option("--no-board", help="Do not draw the board after each move.")] = False,
    web: Annotated[
        bool, typer.Option("--web", help='Play in the browser instead (needs: pip install ".[web]").')
    ] = False,
    port: Annotated[int, typer.Option(help="Port for --web.")] = 5151,
    show_history: Annotated[bool, typer.Option("--history", help="Only show your past practice games.")] = False,
) -> None:
    """Play against a bot that copies their openings and habits."""
    with friendly_errors():
        player = _player(user, site)
        _require_games(player)
        if show_history:
            from chess_prep.practice import history_table

            table = history_table(player)
            console.print(table if table else "No practice games yet.")
            return
        if web:
            try:
                from chess_prep.web import create_app
            except ImportError as exc:
                raise UserError('The browser board needs Flask: pip install ".[web]"') from exc
            from chess_prep.clone import OpponentBook

            try:
                application = create_app(player, OpponentBook.from_db(player.db_path), engines.detect())
            except ImportError as exc:
                raise UserError('The browser board needs Flask: pip install ".[web]"') from exc
            console.print(f"Open [bold]http://127.0.0.1:{port}[/] in your browser. Press Ctrl+C to stop.")
            application.run(host="127.0.0.1", port=port)
            return
        if not (moves or auto) and not sys.stdin.isatty():
            raise UserError("Practice needs a terminal to type moves. For a scripted game use --moves or --auto.")
        do_practice(player, color.value, games, moves, auto, seed, show_board=not no_board)


# ---------------------------------------------------------------- players


@app.command()
def players() -> None:
    """List players you have downloaded games for."""
    found = known_players()
    if not found:
        console.print("No players yet. Try: [bold]chessprep fetch hikaru --site chesscom[/]")
        return
    t = Table(header_style="bold cyan")
    for col in ("Player", "Site", "Games", "Positions analysed", "Last fetch"):
        t.add_column(col)
    for p in found:
        conn = db.connect(p.db_path)
        try:
            t.add_row(
                p.username,
                SITE_LABELS[p.site],
                str(db.game_count(conn)),
                str(db.eval_count(conn)),
                db.get_meta(conn, "fetched_at") or "-",
            )
        finally:
            conn.close()
    console.print(t)


# ------------------------------------------------------------------ doctor


@app.command()
def doctor(offline: Annotated[bool, typer.Option("--offline", help="Skip the internet checks.")] = False) -> None:
    """Check Python packages, engines, folders and internet access."""
    from chess_prep.doctor import FAIL, OK, WARN, run_checks

    checks = run_checks(network=not offline)
    t = Table(header_style="bold cyan", title="chessprep doctor")
    t.add_column("Check")
    t.add_column("Status")
    t.add_column("Details", overflow="fold")
    mark = {OK: "[green]OK[/]", WARN: "[yellow]missing[/]", FAIL: "[red]FAIL[/]"}
    for c in checks:
        t.add_row(c.name, mark[c.status], c.detail)
    console.print(t)
    todo = [c for c in checks if c.status != OK and c.hint]
    if todo:
        hints = "\n".join(f"[bold]{c.name}[/]: {c.hint}" for c in todo)
        console.print(Panel(hints, title="[bold]How to fix", border_style="yellow", expand=False))
    else:
        console.print("[green]Everything is set up.[/]")
    if any(c.status == FAIL for c in checks):
        raise typer.Exit(1)


@app.command("setup-engines")
def setup_engines(
    stockfish: Annotated[bool, typer.Option("--stockfish/--no-stockfish", help="Download Stockfish.")] = True,
    maia: Annotated[
        bool, typer.Option("--maia/--no-maia", help="Download Maia weights (small, used with Lc0).")
    ] = True,
    lc0: Annotated[
        bool, typer.Option("--lc0", help="Also download Lc0 (official builds exist for Windows only).")
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Download again even if already installed.")] = False,
) -> None:
    """Download Stockfish (and Maia weights / Lc0) into the chess-prep data folder."""
    with friendly_errors():

        def bar(label: str):
            p = Progress(
                TextColumn(f"[bold]{label}"),
                BarColumn(),
                TextColumn("{task.percentage:>3.0f}%"),
                console=console,
                transient=True,
            )
            return p

        if stockfish:
            current = engines.find_stockfish()
            if current and not force:
                console.print(f"[green]Stockfish already installed:[/] {current}  (use --force to replace)")
            else:
                with bar("Downloading Stockfish") as p:
                    task = p.add_task("sf", total=None)
                    path = engines.install_stockfish(
                        lambda done, total: p.update(task, completed=done, total=total),
                        log=lambda s: p.console.print(s),
                    )
                console.print(f"[green]Stockfish ready:[/] {path} ({engines.uci_name(path)})")
        if maia:
            if engines.find_maia_weights() and not force:
                console.print(f"[green]Maia weights already installed:[/] {engines.maia_dir()}")
            else:
                with bar("Downloading Maia weights") as p:
                    task = p.add_task("maia", total=None)
                    engines.install_maia(
                        lambda done, total: p.update(task, completed=done, total=total), log=lambda s: None
                    )
                console.print(f"[green]Maia weights ready:[/] {engines.maia_dir()}")
        if lc0:
            if engines.find_lc0() and not force:
                console.print(f"[green]Lc0 already installed:[/] {engines.find_lc0()}")
            else:
                with bar("Downloading Lc0") as p:
                    task = p.add_task("lc0", total=None)
                    path = engines.install_lc0(
                        lambda done, total: p.update(task, completed=done, total=total),
                        log=lambda s: p.console.print(s),
                    )
                console.print(f"[green]Lc0 ready:[/] {path}")
        elif not engines.find_lc0():
            console.print(
                Panel(
                    "Lc0 is optional. Without it the practice bot uses the player's own moves plus a "
                    "strength-limited Stockfish. To get Maia's human-like play, install Lc0:\n\n"
                    + engines.lc0_install_hint(),
                    title="Lc0 (optional)",
                    border_style="dim",
                    expand=False,
                )
            )


# ------------------------------------------------------------------ config


@config_app.command("show")
def config_show() -> None:
    """Show saved settings and what was detected."""
    from chess_prep.paths import config_file, data_dir

    t = Table(header_style="bold cyan")
    t.add_column("Setting")
    t.add_column("Saved value")
    t.add_column("Env override")
    for key, desc in config.KEYS.items():
        t.add_row(
            f"{key}\n[dim]{desc}[/]",
            config.load().get(key, "-"),
            os.environ.get(config.ENV_VARS[key], f"[dim]{config.ENV_VARS[key]}[/]"),
        )
    console.print(t)
    found = engines.detect()
    console.print(f"Config file: {config_file()}\nData folder: {data_dir()}")
    console.print(f"Stockfish in use: {found.stockfish or 'none'}", soft_wrap=True)
    console.print(f"Lc0 in use: {found.lc0 or 'none'}", soft_wrap=True)
    console.print(f"Maia weights: {', '.join(map(str, found.maia)) or 'none'}")


@config_app.command("set")
def config_set(key: str, value: str) -> None:
    """Save a setting, e.g. chessprep config set stockfish /usr/games/stockfish"""
    with friendly_errors():
        if key not in config.KEYS:
            raise UserError(f"Unknown setting {key!r}. Known: {', '.join(config.KEYS)}")
        path = Path(value).expanduser()
        if key in ("stockfish", "lc0") and not path.is_file():
            raise UserError(f"{path} is not a file.")
        saved = config.set_value(key, value)
        console.print(f"[green]Saved[/] {key} = {path.resolve()}  ({saved})")


@config_app.command("unset")
def config_unset(key: str) -> None:
    """Forget a saved setting."""
    with friendly_errors():
        if key not in config.KEYS:
            raise UserError(f"Unknown setting {key!r}. Known: {', '.join(config.KEYS)}")
        config.set_value(key, None)
        console.print(f"Removed {key}.")


# ------------------------------------------------------------- interactive


def _ask_player(new: bool) -> Player | None:
    found = known_players()
    if found and not new:
        t = Table(header_style="bold cyan")
        t.add_column("#")
        t.add_column("Player")
        t.add_column("Site")
        for i, p in enumerate(found, 1):
            t.add_row(str(i), p.username, SITE_LABELS[p.site])
        console.print(t)
        choice = Prompt.ask("Pick a number, or type a new username", default="1")
        if choice.isdigit() and 1 <= int(choice) <= len(found):
            return found[int(choice) - 1]
        name = choice
    else:
        name = Prompt.ask("Username to scout (e.g. [bold]hikaru[/] on Chess.com)")
    site = Prompt.ask("Which site?", choices=["chesscom", "lichess"], default="chesscom")
    return Player(site, name.strip())


def interactive() -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]chess-prep[/] {__version__}\n"
            "Scout an opponent, print a cheatsheet, practise against their clone.",
            border_style="cyan",
        )
    )
    if not engines.find_stockfish():
        console.print(
            "[yellow]Tip:[/] Stockfish is not installed yet. Choose 'Install engines' for the full analysis.\n"
        )
    menu = [
        ("1", "Scout a player (fetch + analyze + prep + cheatsheet)"),
        ("2", "Fetch more games for a player"),
        ("3", "Re-run analysis, prep and cheatsheet"),
        ("4", "Practise against a clone"),
        ("5", "List players"),
        ("6", "Check setup (doctor)"),
        ("7", "Install engines"),
        ("q", "Quit"),
    ]
    while True:
        t = Table.grid(padding=(0, 2))
        for key, label in menu:
            t.add_row(f"[bold cyan]{key}[/]", label)
        console.print(Panel(t, title="What would you like to do?", expand=False))
        choice = Prompt.ask("Choice", choices=[k for k, _ in menu], default="1")
        try:
            with friendly_errors():
                if choice == "q":
                    return
                if choice == "1":
                    p = _ask_player(new=True)
                    n = IntPrompt.ask("How many recent games?", default=200)
                    do_fetch(p, n, None)
                    has_sf = do_engine_pass(p, 12, None)
                    do_report(p, None)
                    do_prep(p, None, 18, has_sf)
                    do_cheatsheet(p, None)
                elif choice == "2":
                    p = _ask_player(new=False)
                    n = IntPrompt.ask("How many recent games?", default=200)
                    do_fetch(p, n, None)
                elif choice == "3":
                    p = _ask_player(new=False)
                    _require_games(p)
                    has_sf = do_engine_pass(p, 12, None)
                    do_report(p, None)
                    do_prep(p, None, 18, has_sf)
                    do_cheatsheet(p, None)
                elif choice == "4":
                    p = _ask_player(new=False)
                    _require_games(p)
                    col = Prompt.ask("Your colour", choices=["white", "black", "random"], default="white")
                    do_practice(p, col, 1, None, None, None)
                elif choice == "5":
                    players()
                elif choice == "6":
                    with contextlib.suppress(typer.Exit):
                        doctor(offline=False)
                elif choice == "7":
                    setup_engines(
                        stockfish=True,
                        maia=Confirm.ask("Also download Maia weights (~10 MB)?", default=True),
                        lc0=False,
                        force=False,
                    )
        except typer.Exit:
            pass
        console.print()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
