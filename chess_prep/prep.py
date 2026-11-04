"""Opening preparation: a move tree of the player's main openings plus engine advice
at the point where you have to choose a move."""

from __future__ import annotations

import json
import math
from contextlib import contextmanager
from pathlib import Path

import chess
import chess.engine

from chess_prep import db
from chess_prep.pgnparse import MATE_SCORE
from chess_prep.report import line_text, move_label

TREE_PLIES = 20
SURPRISE_MARGIN_CP = 40


def build_tree(conn, game_ids: list[int], max_ply: int = TREE_PLIES) -> dict:
    """Nested dict of moves played in these games, with counts and the player's points."""
    root = {"n": 0, "pts": 0.0, "children": {}}
    if not game_ids:
        return root
    marks = ",".join("?" * len(game_ids))
    results = {
        r["id"]: {"win": 1.0, "draw": 0.5, "loss": 0.0}[r["result"]]
        for r in conn.execute(f"SELECT id, result FROM games WHERE id IN ({marks})", game_ids)
    }
    per_game: dict[int, list[str]] = {}
    for r in conn.execute(
        f"SELECT game_id, san FROM moves WHERE game_id IN ({marks}) AND ply < ? ORDER BY game_id, ply",
        [*game_ids, max_ply],
    ):
        per_game.setdefault(r["game_id"], []).append(r["san"])
    for gid, sans in per_game.items():
        pts = results.get(gid, 0.0)
        node = root
        node["n"] += 1
        node["pts"] += pts
        for san in sans:
            node = node["children"].setdefault(san, {"n": 0, "pts": 0.0, "children": {}})
            node["n"] += 1
            node["pts"] += pts
    return root


def _pct(node: dict) -> float:
    return round(node["pts"] / node["n"] * 100, 1) if node["n"] else 0.0


def render_tree(node: dict, they_play: str, max_depth: int = 12, max_children: int = 3) -> list[str]:
    lines: list[str] = []

    def walk(n: dict, ply: int, indent: str) -> None:
        if ply >= max_depth:
            return
        kids = sorted(n["children"].items(), key=lambda kv: -kv[1]["n"])
        limit = max(2, math.ceil(n["n"] * 0.1))
        shown = [(s, k) for s, k in kids if k["n"] >= limit][:max_children]
        for san, kid in shown:
            who = "them" if db.is_player_move(they_play, ply) else "you "
            label = move_label(ply, san)
            lines.append(f"{indent}{label:<14}{who}  {kid['n']:>3} games  they score {_pct(kid):>5}%")
            walk(kid, ply + 1, indent + "  ")

    walk(node, 0, "")
    return lines


def main_line_path(node: dict, min_games: int = 3, max_ply: int = 16) -> list[tuple[str, dict]]:
    path = []
    while node["children"] and len(path) < max_ply:
        san, kid = max(node["children"].items(), key=lambda kv: kv[1]["n"])
        if kid["n"] < min_games:
            break
        path.append((san, kid))
        node = kid
    return path


def struggles(node: dict, they_play: str, min_games: int = 3, max_ply: int = 14) -> list[dict]:
    """Your moves after which the player scores 40% or less."""
    found: list[dict] = []

    def walk(n: dict, ply: int, sans: list[str]) -> None:
        if ply >= max_ply:
            return
        for san, kid in n["children"].items():
            if kid["n"] < min_games:
                continue
            line = [*sans, san]
            if not db.is_player_move(they_play, ply) and _pct(kid) <= 40:
                found.append({"line": line_text(line), "games": kid["n"], "score_pct": _pct(kid)})
            walk(kid, ply + 1, line)

    walk(node, 0, [])
    found.sort(key=lambda x: (x["score_pct"], -x["games"]))
    # drop lines that just extend an already listed (shallower) line with the same count
    out: list[dict] = []
    for f in sorted(found, key=lambda x: len(x["line"])):
        if any(f["line"].startswith(o["line"]) and f["games"] == o["games"] for o in out):
            continue
        out.append(f)
    out.sort(key=lambda x: (x["score_pct"], -x["games"]))
    return out[:5]


@contextmanager
def open_engine(stockfish: Path | None):
    if stockfish is None:
        yield None
        return
    engine = chess.engine.SimpleEngine.popen_uci(str(stockfish))
    try:
        engine.configure({"Threads": 2, "Hash": 128})
        yield engine
    finally:
        engine.quit()


def engine_options(engine, board: chess.Board, depth: int, multipv: int = 5) -> list[dict]:
    infos = engine.analyse(board, chess.engine.Limit(depth=depth), multipv=multipv)
    if isinstance(infos, dict):
        infos = [infos]
    out = []
    for info in infos:
        pv = info.get("pv") or []
        score = info.get("score")
        if not pv or score is None:
            continue
        cp = score.pov(board.turn).score(mate_score=MATE_SCORE)
        b = board.copy()
        sans = []
        for mv in pv[:6]:
            sans.append(b.san(mv))
            b.push(mv)
        out.append({"move": sans[0], "cp": cp, "pv": " ".join(sans)})
    return out


def decision_point(tree: dict, they_play: str, engine, depth: int) -> dict | None:
    """Deepest well-trodden position on the main line where it is your move."""
    path = main_line_path(tree)
    board = chess.Board()
    best = None
    sans: list[str] = []
    nodes = [tree] + [kid for _, kid in path]
    for i in range(len(nodes)):
        ply = i
        if not db.is_player_move(they_play, ply) and nodes[i]["n"] >= 3 and not board.is_game_over():
            best = (ply, list(sans), board.copy(), nodes[i])
        if i < len(path):
            san = path[i][0]
            board.push_san(san)
            sans.append(san)
    if best is None:
        return None
    ply, sans, board, node = best
    faced = [
        {"move": s, "games": k["n"], "they_score_pct": _pct(k)}
        for s, k in sorted(node["children"].items(), key=lambda kv: -kv[1]["n"])
    ]
    result = {
        "line": line_text(sans),
        "fen": board.fen(),
        "games_here": node["n"],
        "faced": faced[:5],
        "engine": [],
        "recommended": None,
        "surprise": None,
    }
    faced_moves = {f["move"] for f in faced}
    if engine is not None:
        options = engine_options(engine, board, depth)
        result["engine"] = options[:3]
        if options:
            result["recommended"] = options[0]
            for opt in options:
                if opt["move"] not in faced_moves and options[0]["cp"] - opt["cp"] <= SURPRISE_MARGIN_CP:
                    result["surprise"] = opt
                    break
    else:
        tried = [f for f in faced if f["games"] >= 2]
        if tried:
            pick = min(tried, key=lambda f: f["they_score_pct"])
            result["recommended"] = {"move": pick["move"], "cp": None, "pv": pick["move"]}
    return result


def build_prep(db_path: Path, report: dict, stockfish: Path | None, depth: int = 18, per_color: int = 3) -> dict:
    conn = db.connect(db_path)
    prep: dict = {"player": report["player"], "engine_used": stockfish is not None, "white": [], "black": []}
    try:
        with open_engine(stockfish) as engine:
            for color in ("white", "black"):
                for op in report["openings"][color][:per_color]:
                    tree = build_tree(conn, op["game_ids"])
                    prep[color].append(
                        {
                            "name": op["name"],
                            "eco": op["eco"],
                            "games": op["games"],
                            "score_pct": op["score_pct"],
                            "main_line": op["main_line"],
                            "tree": render_tree(tree, color),
                            "decision": decision_point(tree, color, engine, depth),
                            "struggles": struggles(tree, color),
                        }
                    )
    finally:
        conn.close()
    return prep


def _fmt_cp(cp: int | None) -> str:
    if cp is None:
        return ""
    if abs(cp) >= MATE_SCORE - 500:
        return "mate" if cp > 0 else "gets mated"
    return f"{cp / 100:+.2f}"


def prep_markdown(prep: dict) -> str:
    who = prep["player"]["label"]
    out = [f"# Opening prep: {who}", ""]
    if not prep["engine_used"]:
        out += ["_Stockfish was not found, so there are no engine suggestions. Run `chessprep setup-engines`._", ""]
    for color in ("white", "black"):
        you = "Black" if color == "white" else "White"
        out += [f"## When they have {color.title()} (you play {you})", ""]
        if not prep[color]:
            out += ["_No games with this colour._", ""]
        for op in prep[color]:
            eco = f" ({op['eco']})" if op["eco"] else ""
            out += [f"### {op['name']}{eco}", f"{op['games']} games, they score {op['score_pct']}%.", ""]
            if op["main_line"]:
                out += [f"Their main line: `{op['main_line']}`", ""]
            if op["tree"]:
                out += ["```text", *op["tree"], "```", ""]
            d = op["decision"]
            if d:
                out += [
                    f"**Your decision point** after `{d['line'] or 'the first move'}` ({d['games_here']} games):",
                    "",
                ]
                for f in d["faced"]:
                    out.append(f"- They have faced **{f['move']}** {f['games']}x and scored {f['they_score_pct']}%")
                for e in d["engine"]:
                    out.append(f"- Engine: **{e['move']}** ({_fmt_cp(e['cp'])} for you), line `{e['pv']}`")
                rec, sur = d["recommended"], d["surprise"]
                if rec:
                    never = " (they have never faced it here)" if sur and sur["move"] == rec["move"] else ""
                    out.append(f"- Recommended: **{rec['move']}**{never}")
                if sur and (not rec or sur["move"] != rec["move"]):
                    s = d["surprise"]
                    out.append(f"- Surprise try: **{s['move']}** ({_fmt_cp(s['cp'])}). They have never faced it here.")
                out.append("")
            if op["struggles"]:
                out.append("Lines where they struggle:")
                for s in op["struggles"]:
                    out.append(f"- `{s['line']}`: they score {s['score_pct']}% in {s['games']} games")
                out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_prep(prep: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / "prep.md"
    js = out_dir / "prep.json"
    md.write_text(prep_markdown(prep), encoding="utf-8")
    js.write_text(json.dumps(prep, indent=2), encoding="utf-8")
    return md, js


def load_prep(out_dir: Path) -> dict | None:
    path = out_dir / "prep.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
