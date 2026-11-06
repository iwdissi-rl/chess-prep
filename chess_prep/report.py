"""Turn a player's stored games into a weakness report (a plain dict / JSON).

Works without an engine (results, openings, clock habits). After
``chessprep analyze`` has run Stockfish, it also finds and classifies mistakes.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import chess

from chess_prep import db
from chess_prep.paths import Player
from chess_prep.pgnparse import MATE_SCORE, opening_family

PIECE_VALUE = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3, chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 100}
EVAL_CAP = 1500

MOTIF_TEXT = {
    "allowed mate": "missing mating threats",
    "hanging piece": "leaving pieces undefended",
    "fork": "walking into forks",
    "pin": "walking into pins",
    "back rank": "back-rank weaknesses",
    "passed pawn": "underestimating passed pawns",
    "missed tactic": "missing their own captures and checks",
    "king safety": "letting checks and attacks reach their king",
}

# Thresholds in win-percentage points lost by a single move (same idea as Lichess).
INACCURACY, MISTAKE, BLUNDER = 5.0, 10.0, 15.0


# ------------------------------------------------------------------ helpers


def win_pct(cp: int | None) -> float | None:
    """Centipawns (White's view) to White's winning chances, 0-100."""
    if cp is None:
        return None
    cp = max(-EVAL_CAP, min(EVAL_CAP, cp))
    return 50 + 50 * (2 / (1 + math.exp(-0.00368208 * cp)) - 1)


def score_pct(w: int, d: int, total: int) -> float:
    return round((w + 0.5 * d) / total * 100, 1) if total else 0.0


def phase_of(board: chess.Board, ply: int) -> str:
    pieces = sum(
        len(board.pieces(pt, c)) for pt in (chess.KNIGHT, chess.BISHOP, chess.ROOK, chess.QUEEN) for c in chess.COLORS
    )
    if pieces <= 6:
        return "endgame"
    if ply < 24:
        return "opening"
    return "middlegame"


def board_from_epd(epd: str) -> chess.Board:
    b = chess.Board()
    b.set_epd(epd)
    return b


def move_label(ply: int, san: str) -> str:
    return f"{ply // 2 + 1}.{san}" if ply % 2 == 0 else f"{ply // 2 + 1}...{san}"


def line_text(sans: list[str]) -> str:
    out = []
    for i, san in enumerate(sans):
        out.append(f"{i // 2 + 1}.{san}" if i % 2 == 0 else san)
    return " ".join(out)


def _record(rows) -> dict:
    w = sum(1 for r in rows if r["result"] == "win")
    d = sum(1 for r in rows if r["result"] == "draw")
    total = len(rows)
    return {"games": total, "wins": w, "draws": d, "losses": total - w - d, "score_pct": score_pct(w, d, total)}


# ------------------------------------------------------------------ motifs


def classify_blunder(
    board_after: chess.Board,
    refutation_uci: str | None,
    player_color: chess.Color,
    *,
    board_before: chess.Board | None = None,
    played_uci: str | None = None,
    best_before_uci: str | None = None,
    cp_after: int | None = None,
) -> str:
    """Name what went wrong, using the engine's reply (refutation) and the move they missed."""
    if cp_after is not None:
        mine = cp_after if player_color == chess.WHITE else -cp_after
        if mine <= -(MATE_SCORE - 200):
            return "allowed mate"
    ref = _legal(board_after, refutation_uci)
    if ref is not None:
        mover = board_after.piece_at(ref.from_square)
        after = board_after.copy(stack=False)
        after.push(ref)
        if after.is_checkmate():
            return "allowed mate"
        if board_after.is_capture(ref) and mover is not None:
            victim = board_after.piece_at(ref.to_square)
            if victim is not None:
                defended = board_after.is_attacked_by(player_color, ref.to_square)
                if not defended or PIECE_VALUE[victim.piece_type] > PIECE_VALUE[mover.piece_type]:
                    return "hanging piece"
        if mover is not None:
            attacked = [
                sq
                for sq in after.attacks(ref.to_square)
                if (p := after.piece_at(sq)) is not None
                and p.color == player_color
                and (
                    p.piece_type == chess.KING
                    or PIECE_VALUE[p.piece_type] > PIECE_VALUE[mover.piece_type]
                    or (PIECE_VALUE[p.piece_type] >= 3 and not after.is_attacked_by(player_color, sq))
                )
            ]
            if len(attacked) >= 2:
                return "fork"
            if mover.piece_type in (chess.BISHOP, chess.ROOK, chess.QUEEN):
                for sq in chess.SquareSet(after.occupied_co[player_color]):
                    p = after.piece_at(sq)
                    if (
                        p
                        and p.piece_type != chess.KING
                        and after.is_pinned(player_color, sq)
                        and not board_after.is_pinned(player_color, sq)
                    ):
                        return "pin"
        king = after.king(player_color)
        back_rank = 0 if player_color == chess.WHITE else 7
        if (
            king is not None
            and chess.square_rank(king) == back_rank
            and after.is_check()
            and mover is not None
            and mover.piece_type in (chess.ROOK, chess.QUEEN)
            and chess.square_rank(ref.to_square) == back_rank
        ):
            return "back rank"
        if ref.promotion or (
            mover is not None and mover.piece_type == chess.PAWN and chess.square_rank(ref.to_square) in (1, 6)
        ):
            return "passed pawn"
    if board_before is not None:
        best = _legal(board_before, best_before_uci)
        if (
            best is not None
            and best.uci() != played_uci
            and (board_before.is_capture(best) or board_before.gives_check(best))
        ):
            return "missed tactic"
    if ref is not None and board_after.gives_check(ref):
        return "king safety"
    return "positional"


def _legal(board: chess.Board, uci: str | None) -> chess.Move | None:
    if not uci:
        return None
    try:
        mv = chess.Move.from_uci(uci)
    except ValueError:
        return None
    return mv if mv in board.legal_moves else None


# ------------------------------------------------------------------ main


def _load(conn: sqlite3.Connection):
    games = {r["id"]: r for r in conn.execute("SELECT * FROM games ORDER BY end_time, id")}
    moves: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in conn.execute(
        """SELECT m.game_id, m.ply, m.san, m.uci, m.epd_before, m.epd_after, m.clock_seconds,
                  eb.cp AS cp_before, eb.best_uci AS best_before,
                  ea.cp AS cp_after, ea.best_uci AS refutation
           FROM moves m
           LEFT JOIN evals eb ON eb.epd = m.epd_before
           LEFT JOIN evals ea ON ea.epd = m.epd_after
           ORDER BY m.game_id, m.ply"""
    ):
        moves[r["game_id"]].append(r)
    return games, moves


def build_report(db_path: Path, player: Player) -> dict:
    conn = db.connect(db_path)
    try:
        games, moves = _load(conn)
        if not games:
            raise ValueError(f"No games stored for {player.label}. Run 'chessprep fetch' first.")
        rows = list(games.values())
        report = {
            "player": {"username": player.username, "site": player.site, "label": player.label},
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "summary": _summary(rows),
            "openings": _openings(rows, moves),
            "first_moves": _first_moves(rows, moves),
            "mistakes": _mistakes(games, moves),
            "clock": _clock(games, moves),
            "critical_positions": _critical_positions(games, moves),
            "endgames": _endgames(games, moves),
            "losses": _losses(rows),
        }
        report["notes"] = _notes(report)
        return report
    finally:
        conn.close()


def _summary(rows) -> dict:
    s = _record(rows)
    dates = sorted(r["played_at"] for r in rows if r["played_at"])
    ratings = [r["player_rating"] for r in rows if r["player_rating"]]
    recent = sorted((r for r in rows if r["player_rating"]), key=lambda r: r["end_time"] or 0)[-20:]
    by_class: dict[str, list] = defaultdict(list)
    for r in rows:
        by_class[r["time_class"] or "unknown"].append(r)
    s.update(
        {
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "recent_rating": round(statistics.median(r["player_rating"] for r in recent)) if recent else None,
            "avg_rating": round(statistics.mean(ratings)) if ratings else None,
            "by_color": {c: _record([r for r in rows if r["color"] == c]) for c in ("white", "black")},
            "by_time_class": {k: _record(v) for k, v in sorted(by_class.items(), key=lambda kv: -len(kv[1]))},
        }
    )
    return s


def _main_line(game_ids: list[int], moves, ply_limit: int = 16, min_count: int = 2) -> list[str]:
    seqs = [[m["san"] for m in moves[g][:ply_limit]] for g in game_ids]
    line: list[str] = []
    while len(line) < ply_limit:
        nxt = Counter(s[len(line)] for s in seqs if len(s) > len(line) and s[: len(line)] == line)
        if not nxt:
            break
        san, count = nxt.most_common(1)[0]
        if count < min_count:
            break
        line.append(san)
    return line


def _openings(rows, moves, top: int = 6) -> dict:
    out = {}
    for color in ("white", "black"):
        groups: dict[str, list] = defaultdict(list)
        for r in rows:
            if r["color"] == color:
                groups[opening_family(r["opening"] or "Unknown opening")].append(r)
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:top]
        items = []
        for name, grp in ranked:
            eco = Counter(r["eco"] for r in grp if r["eco"]).most_common(1)
            variations = Counter(r["opening"] for r in grp if r["opening"] != name).most_common(3)
            rec = _record(grp)
            rec.update(
                {
                    "name": name,
                    "variations": [{"name": v, "games": n} for v, n in variations],
                    "eco": eco[0][0] if eco else "",
                    "share_pct": round(len(grp) / max(1, sum(1 for r in rows if r["color"] == color)) * 100, 1),
                    "main_line": line_text(_main_line([r["id"] for r in grp], moves)),
                    "game_ids": [r["id"] for r in grp],
                }
            )
            items.append(rec)
        out[color] = items
    return out


def _first_moves(rows, moves) -> dict:
    as_white: dict[str, list] = defaultdict(list)
    as_black: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        mv = moves.get(r["id"], [])
        if r["color"] == "white" and mv:
            as_white[mv[0]["san"]].append(r)
        elif r["color"] == "black" and len(mv) >= 2:
            as_black[mv[0]["san"]][mv[1]["san"]].append(r)

    def table(d: dict[str, list], total: int) -> list[dict]:
        return [
            {"move": k, "count": len(v), "pct": round(len(v) / total * 100, 1), "score_pct": _record(v)["score_pct"]}
            for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))[:4]
        ]

    white_total = sum(len(v) for v in as_white.values())
    black = {}
    for first, replies in sorted(as_black.items(), key=lambda kv: -sum(len(v) for v in kv[1].values()))[:3]:
        total = sum(len(v) for v in replies.values())
        black[f"1.{first}"] = table(replies, total)
    return {"as_white": table(as_white, white_total) if white_total else [], "as_black": black}


def _mistakes(games, moves) -> dict:
    out = {
        "has_evals": False,
        "moves_checked": 0,
        "inaccuracies": 0,
        "mistakes": 0,
        "blunders": 0,
        "per_100_moves": {"mistakes": 0.0, "blunders": 0.0},
        "by_phase": {
            p: {"moves": 0, "avg_loss": 0.0, "mistakes": 0, "blunders": 0} for p in ("opening", "middlegame", "endgame")
        },
        "motifs": {},
        "time_trouble_blunders": 0,
        "examples": [],
    }
    motifs: Counter = Counter()
    loss_sum: dict[str, float] = defaultdict(float)
    examples = []
    for gid, g in games.items():
        player_color = chess.WHITE if g["color"] == "white" else chess.BLACK
        trouble_limit = _trouble_limit(g)
        for m in moves[gid]:
            if not db.is_player_move(g["color"], m["ply"]):
                continue
            before, after = win_pct(m["cp_before"]), win_pct(m["cp_after"])
            if before is None or after is None:
                continue
            if player_color == chess.BLACK:
                before, after = 100 - before, 100 - after
            loss = max(0.0, before - after)
            board_before = board_from_epd(m["epd_before"])
            phase = phase_of(board_before, m["ply"])
            out["moves_checked"] += 1
            ph = out["by_phase"][phase]
            ph["moves"] += 1
            loss_sum[phase] += loss
            if loss >= BLUNDER:
                out["blunders"] += 1
                ph["blunders"] += 1
                board_after = board_from_epd(m["epd_after"])
                motif = classify_blunder(
                    board_after,
                    m["refutation"],
                    player_color,
                    board_before=board_before,
                    played_uci=m["uci"],
                    best_before_uci=m["best_before"],
                    cp_after=m["cp_after"],
                )
                motifs[motif] += 1
                if trouble_limit and m["clock_seconds"] is not None and m["clock_seconds"] < trouble_limit:
                    out["time_trouble_blunders"] += 1
                # A shallow search can name the blunder itself as best; show nothing then.
                best = uci_to_san(board_before, m["best_before"]) if m["best_before"] != m["uci"] else None
                board_before.fullmove_number = m["ply"] // 2 + 1
                examples.append(
                    {
                        "url": g["url"],
                        "move": move_label(m["ply"], m["san"]),
                        "fen": board_before.fen(),
                        "win_pct_lost": round(loss, 1),
                        "phase": phase,
                        "motif": motif,
                        "best_move": best,
                        "clock": m["clock_seconds"],
                    }
                )
            elif loss >= MISTAKE:
                out["mistakes"] += 1
                ph["mistakes"] += 1
            elif loss >= INACCURACY:
                out["inaccuracies"] += 1
    if out["moves_checked"]:
        out["has_evals"] = True
        n = out["moves_checked"]
        out["per_100_moves"] = {
            "mistakes": round(out["mistakes"] / n * 100, 2),
            "blunders": round(out["blunders"] / n * 100, 2),
        }
        for phase, ph in out["by_phase"].items():
            ph["avg_loss"] = round(loss_sum[phase] / ph["moves"], 2) if ph["moves"] else 0.0
    out["motifs"] = dict(motifs.most_common())
    # The same position can come up more than once in a game (repetitions); list it once.
    unique: dict[tuple, dict] = {}
    for ex in sorted(examples, key=lambda e: -e["win_pct_lost"]):
        unique.setdefault((ex["url"], " ".join(ex["fen"].split()[:4]), ex["move"].split(".")[-1]), ex)
    out["examples"] = list(unique.values())[:12]
    return out


def uci_to_san(board: chess.Board, uci: str | None) -> str | None:
    if not uci:
        return None
    try:
        move = chess.Move.from_uci(uci)
    except ValueError:
        return None
    return board.san(move) if move in board.legal_moves else None


def _trouble_limit(g) -> float | None:
    base = g["base_seconds"]
    if not base:
        return None
    return min(60.0, 0.2 * base)


def _clock(games, moves) -> dict:
    by_class: dict[str, list[int]] = defaultdict(list)
    for gid, g in games.items():
        if g["base_seconds"] and any(m["clock_seconds"] is not None for m in moves[gid]):
            by_class[g["time_class"] or "unknown"].append(gid)
    if not by_class:
        return {"has_clocks": False}
    main_class, gids = max(by_class.items(), key=lambda kv: len(kv[1]))

    think_phase: dict[str, list[float]] = defaultdict(list)
    think_move: dict[int, list[float]] = defaultdict(list)
    trouble_games, trouble_first = [], []
    for gid, g in games.items():
        if not g["base_seconds"]:
            continue
        limit = _trouble_limit(g)
        prev_clock = None
        entered = None
        for m in moves[gid]:
            if not db.is_player_move(g["color"], m["ply"]):
                continue
            clock = m["clock_seconds"]
            if clock is None:
                continue
            if gid in gids and prev_clock is not None:
                think = max(0.0, prev_clock - clock + (g["increment"] or 0))
                board = board_from_epd(m["epd_before"])
                think_phase[phase_of(board, m["ply"])].append(think)
                think_move[m["ply"] // 2 + 1].append(think)
            if limit and clock < limit and entered is None:
                entered = m["ply"] // 2 + 1
            prev_clock = clock
        if entered is not None:
            trouble_games.append(g)
            trouble_first.append(entered)

    timed = [g for g in games.values() if g["base_seconds"]]
    trouble_ids = {g["id"] for g in trouble_games}
    calm = [g for g in timed if g["id"] not in trouble_ids]
    longest = sorted(
        (
            {"move": k, "avg_seconds": round(statistics.mean(v), 1), "samples": len(v)}
            for k, v in think_move.items()
            if len(v) >= 3
        ),
        key=lambda x: -x["avg_seconds"],
    )[:5]
    return {
        "has_clocks": True,
        "time_class": main_class,
        "games_with_clocks": len(gids),
        "avg_think_by_phase": {p: round(statistics.mean(v), 1) for p, v in think_phase.items() if v},
        "longest_thinks": longest,
        "time_trouble_games": len(trouble_games),
        "time_trouble_pct": round(len(trouble_games) / len(timed) * 100, 1) if timed else 0.0,
        "time_trouble_score_pct": _record(trouble_games)["score_pct"],
        "calm_score_pct": _record(calm)["score_pct"],
        "typical_time_trouble_move": round(statistics.median(trouble_first)) if trouble_first else None,
    }


def _critical_positions(games, moves, min_games: int | None = None, max_ply: int = 30) -> list[dict]:
    min_games = min_games or max(3, len(games) // 50)
    seen: dict[str, dict] = {}
    for gid, g in games.items():
        sans: list[str] = []
        visited = set()
        for m in moves[gid]:
            if m["ply"] >= max_ply:
                break
            if db.is_player_move(g["color"], m["ply"]) and m["epd_before"] not in visited:
                visited.add(m["epd_before"])
                entry = seen.setdefault(
                    m["epd_before"],
                    {
                        "games": [],
                        "replies": Counter(),
                        "line": list(sans),
                        "ply": m["ply"],
                        "cp": m["cp_before"],
                        "color": g["color"],
                    },
                )
                entry["games"].append(g)
                entry["replies"][m["san"]] += 1
            sans.append(m["san"])
    out = []
    used_sets = set()
    for epd, e in sorted(seen.items(), key=lambda kv: kv[1]["ply"]):
        if len(e["games"]) < min_games:
            continue
        rec = _record(e["games"])
        if rec["score_pct"] > 40:
            continue
        key = frozenset(g["id"] for g in e["games"])
        if key in used_sets:
            continue
        used_sets.add(key)
        board = board_from_epd(epd)
        board.fullmove_number = e["ply"] // 2 + 1
        out.append(
            {
                "fen": board.fen(),
                "line": line_text(e["line"]),
                "they_play": e["color"],
                "times_reached": rec["games"],
                "score_pct": rec["score_pct"],
                "record": f"{rec['wins']}W {rec['draws']}D {rec['losses']}L",
                "usual_move": e["replies"].most_common(1)[0][0],
                "eval_cp": e["cp"],
            }
        )
    out.sort(key=lambda x: (x["score_pct"], -x["times_reached"]))
    return out[:10]


def _endgames(games, moves) -> dict:
    reached, losses_sum, n_moves, blunders = [], 0.0, 0, 0
    for gid, g in games.items():
        got_there = False
        for m in moves[gid]:
            board = board_from_epd(m["epd_before"])
            if phase_of(board, m["ply"]) != "endgame":
                continue
            got_there = True
            if not db.is_player_move(g["color"], m["ply"]):
                continue
            before, after = win_pct(m["cp_before"]), win_pct(m["cp_after"])
            if before is None or after is None:
                continue
            if g["color"] == "black":
                before, after = 100 - before, 100 - after
            loss = max(0.0, before - after)
            losses_sum += loss
            n_moves += 1
            blunders += loss >= BLUNDER
        if got_there:
            reached.append(g)
    rec = _record(reached)
    return {
        "games_reaching_endgame": rec["games"],
        "score_pct": rec["score_pct"],
        "record": f"{rec['wins']}W {rec['draws']}D {rec['losses']}L",
        "moves_checked": n_moves,
        "avg_loss": round(losses_sum / n_moves, 2) if n_moves else None,
        "blunders": blunders,
    }


def _losses(rows) -> dict:
    lost = [r for r in rows if r["result"] == "loss"]
    by_term = Counter(r["termination"] or "unknown" for r in lost)
    by_opening: dict[str, list] = defaultdict(list)
    for r in rows:
        by_opening[(opening_family(r["opening"] or "Unknown opening"), r["color"])].append(r)
    worst = []
    for (name, color), grp in by_opening.items():
        if len(grp) >= 5:
            rec = _record(grp)
            worst.append({"opening": name, "they_play": color, **rec})
    worst.sort(key=lambda x: (x["score_pct"], -x["games"]))
    best = sorted(worst, key=lambda x: (-x["score_pct"], -x["games"]))
    return {
        "count": len(lost),
        "by_termination": dict(by_term.most_common()),
        "avg_length_moves": round(statistics.mean((r["ply_count"] + 1) // 2 for r in lost), 1) if lost else 0,
        "worst_openings": worst[:5],
        "best_openings": best[:3],
    }


def _notes(rep: dict) -> list[str]:
    notes: list[str] = []
    s = rep["summary"]
    w, b = s["by_color"]["white"], s["by_color"]["black"]
    if w["games"] >= 5 and b["games"] >= 5:
        if w["score_pct"] - b["score_pct"] >= 12:
            notes.append(f"Much stronger with White ({w['score_pct']}%) than with Black ({b['score_pct']}%).")
        elif b["score_pct"] - w["score_pct"] >= 12:
            notes.append(f"Scores better with Black ({b['score_pct']}%) than with White ({w['score_pct']}%).")

    worst = [x for x in rep["losses"]["worst_openings"] if x["score_pct"] <= 40]
    for x in worst[:2]:
        you = "Black" if x["they_play"] == "white" else "White"
        notes.append(
            f"Aim for the {x['opening']} when you have {you}: "
            f"they score only {x['score_pct']}% there ({x['games']} games)."
        )
    for x in rep["losses"]["best_openings"][:1]:
        if x["score_pct"] >= 70:
            you = "Black" if x["they_play"] == "white" else "White"
            notes.append(
                f"Avoid the {x['opening']} when you have {you} if you can: "
                f"they score {x['score_pct']}% in it with {x['they_play'].capitalize()} ({x['games']} games)."
            )

    c = rep["clock"]
    if c.get("has_clocks") and c["time_trouble_games"] >= 5 and c["calm_score_pct"] - c["time_trouble_score_pct"] >= 10:
        notes.append(
            f"Results drop in time trouble ({c['time_trouble_score_pct']}% vs {c['calm_score_pct']}%). "
            "Keep the position complicated and let the clock work for you."
        )
    lost = rep["losses"]
    if lost["count"] >= 5:
        timeouts = lost["by_termination"].get("timeout", 0)
        if timeouts / lost["count"] >= 0.2:
            notes.append(f"{round(timeouts / lost['count'] * 100)}% of their losses are on time.")

    m = rep["mistakes"]
    if m["has_evals"] and m["blunders"]:
        phases = {p: v for p, v in m["by_phase"].items() if v["moves"] >= 20}
        if phases:
            worst_phase = max(phases, key=lambda p: phases[p]["avg_loss"])
            notes.append(f"Their play is weakest in the {worst_phase} (largest average loss per move).")
        motifs = [k for k in m["motifs"] if k != "positional"]
        if motifs:
            notes.append(f"Most common tactical slip: {MOTIF_TEXT.get(motifs[0], motifs[0])}.")

    e = rep["endgames"]
    if e["games_reaching_endgame"] >= 8 and s["score_pct"] - e["score_pct"] >= 8:
        notes.append(
            f"Weaker in endgames ({e['score_pct']}% vs {s['score_pct']}% overall). Trading down is a good plan."
        )
    elif e["games_reaching_endgame"] >= 8 and e["score_pct"] - s["score_pct"] >= 8:
        notes.append(f"Strong in endgames ({e['score_pct']}%). Look for chances before the pieces come off.")
    if not notes:
        notes.append("No strong patterns yet. Fetch more games or run the engine pass for more detail.")
    return notes


def write_report(report: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "report.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def load_report(out_dir: Path) -> dict | None:
    path = out_dir / "report.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
