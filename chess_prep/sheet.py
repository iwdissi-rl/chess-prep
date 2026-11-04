"""One-page cheatsheet in PDF, Markdown and plain text."""

from __future__ import annotations

import textwrap
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

from chess_prep import __version__
from chess_prep.prep import _fmt_cp

PAGE_W, PAGE_H = landscape(A4)
MARGIN = 10 * mm
GUTTER = 5 * mm
TITLE_H = 24
HEADER_H = 16
BODY_SIZE = 8.2
LINE_H = 10.0
CHAR_W = BODY_SIZE * 0.6  # Courier is monospaced: every glyph is 0.6 em wide
QUAD_W = (PAGE_W - 2 * MARGIN - GUTTER) / 2
QUAD_H = (PAGE_H - 2 * MARGIN - TITLE_H - GUTTER) / 2

Line = tuple[str, bool]  # (text, bold)


def _latin1(text: str) -> str:
    return text.encode("latin-1", "replace").decode("latin-1")


def _opening_section(report: dict, prep: dict | None, color: str) -> list[Line]:
    lines: list[Line] = []
    fm = report["first_moves"]
    if color == "white" and fm["as_white"]:
        moves = ", ".join(f"{m['move']} {m['pct']:.0f}%" for m in fm["as_white"])
        lines.append((f"First move: {moves}", True))
    if color == "black" and fm["as_black"]:
        for first, replies in list(fm["as_black"].items())[:2]:
            moves = ", ".join(f"{m['move']} {m['pct']:.0f}%" for m in replies[:3])
            lines.append((f"vs {first}: {moves}", True))
    rec = report["summary"]["by_color"][color]
    lines.append((f"Record: {rec['wins']}W {rec['draws']}D {rec['losses']}L ({rec['score_pct']}%)", False))
    lines.append(("", False))

    prep_items = {p["name"]: p for p in (prep or {}).get(color, [])}
    for op in report["openings"][color][:3]:
        lines.append((f"{op['name']} ({op['games']} g, they score {op['score_pct']}%)", True))
        if op["main_line"]:
            lines.append((f"  {op['main_line']}", False))
        p = prep_items.get(op["name"])
        d = p and p.get("decision")
        if d and d.get("recommended"):
            rec_move = d["recommended"]
            cp = _fmt_cp(rec_move.get("cp"))
            where = d["line"] or "move 1"
            lines.append((f"  You: after {where} play {rec_move['move']}" + (f" ({cp})" if cp else ""), True))
            if d.get("surprise") and d["surprise"]["move"] != rec_move["move"]:
                lines.append(
                    (f"  Surprise: {d['surprise']['move']} ({_fmt_cp(d['surprise']['cp'])}), never faced", False)
                )
        if p and p.get("struggles"):
            s = p["struggles"][0]
            lines.append((f"  They struggle: {s['line']} ({s['score_pct']}%)", False))
    for op in report["openings"][color][3:5]:
        lines.append((f"{op['name']} ({op['games']} g, {op['score_pct']}%)", False))
    return lines


def _mistake_section(report: dict) -> list[Line]:
    m = report["mistakes"]
    lines: list[Line] = []
    if m["has_evals"]:
        lines.append(
            (
                f"Blunders: {m['per_100_moves']['blunders']} per 100 moves  "
                f"(mistakes {m['per_100_moves']['mistakes']})",
                True,
            )
        )
        for phase, ph in m["by_phase"].items():
            if ph["moves"]:
                lines.append((f"  {phase:<10} {ph['blunders']:>3} blunders, avg loss {ph['avg_loss']}%/move", False))
        if m["motifs"]:
            motifs = ", ".join(f"{k} {v}" for k, v in list(m["motifs"].items())[:5])
            lines.append((f"Missed ideas: {motifs}", True))
        if m["time_trouble_blunders"]:
            lines.append((f"  {m['time_trouble_blunders']} blunders came in time trouble", False))
        for ex in m["examples"][:3]:
            best = f", best was {ex['best_move']}" if ex.get("best_move") else ""
            lines.append((f"  {ex['move']} ({ex['phase']}, {ex['motif']}{best})", False))
        lines.append(("", False))
    else:
        lines.append(("No engine pass yet: run 'chessprep analyze'", True))
        lines.append(("for blunder counts and tactical motifs.", False))
        lines.append(("", False))

    lost = report["losses"]
    if lost["count"]:
        terms = ", ".join(f"{k} {v}" for k, v in lost["by_termination"].items())
        lines.append((f"How they lose ({lost['count']}): {terms}", True))
        lines.append((f"  Average lost game: {lost['avg_length_moves']} moves", False))
    for w in lost["worst_openings"][:3]:
        if w["score_pct"] <= 45:
            lines.append((f"  Weak in {w['opening']} as {w['they_play']}: {w['score_pct']}%", False))
    crit = report["critical_positions"]
    if crit:
        c = crit[0]
        lines.append((f"Trouble spot ({c['times_reached']} games, {c['score_pct']}%):", True))
        lines.append((f"  {c['line']}", False))
    return lines


def _reminder_section(report: dict) -> list[Line]:
    s = report["summary"]
    lines: list[Line] = []
    rating = f", rating ~{s['recent_rating']}" if s.get("recent_rating") else ""
    lines.append((f"{s['games']} games {s['first_date']} to {s['last_date']}{rating}", True))
    lines.append((f"  Overall {s['wins']}W {s['draws']}D {s['losses']}L ({s['score_pct']}%)", False))
    classes = ", ".join(f"{k} {v['games']}g {v['score_pct']}%" for k, v in list(s["by_time_class"].items())[:4])
    lines.append((f"  {classes}", False))
    c = report["clock"]
    if c.get("has_clocks"):
        ph = c["avg_think_by_phase"]
        lines.append((f"Clock ({c['time_class']}): " + ", ".join(f"{k} {v}s" for k, v in ph.items()), True))
        if c["time_trouble_games"]:
            move = f", usually from move {c['typical_time_trouble_move']}" if c["typical_time_trouble_move"] else ""
            lines.append((f"  Time trouble in {c['time_trouble_pct']}% of games{move}", False))
            lines.append((f"  Score in time trouble {c['time_trouble_score_pct']}% vs {c['calm_score_pct']}%", False))
        if c["longest_thinks"]:
            thinks = ", ".join(f"m{t['move']} {t['avg_seconds']}s" for t in c["longest_thinks"][:4])
            lines.append((f"  Longest thinks: {thinks}", False))
    e = report["endgames"]
    if e["games_reaching_endgame"]:
        lines.append((f"Endgames: {e['games_reaching_endgame']} games, they score {e['score_pct']}%", True))
    lines.append(("", False))
    lines.append(("GAME PLAN", True))
    for note in report["notes"]:
        lines.append((f"- {note}", False))
    return lines


def sections(report: dict, prep: dict | None) -> list[tuple[str, list[Line]]]:
    return [
        ("WHEN THEY HAVE WHITE (you are Black)", _opening_section(report, prep, "white")),
        ("WHEN THEY HAVE BLACK (you are White)", _opening_section(report, prep, "black")),
        ("MISTAKES AND LOSSES", _mistake_section(report)),
        ("KEY REMINDERS", _reminder_section(report)),
    ]


def _wrap(lines: list[Line], width: int) -> list[Line]:
    out: list[Line] = []
    for text, bold in lines:
        if not text:
            out.append(("", bold))
            continue
        indent = len(text) - len(text.lstrip(" "))
        for part in textwrap.wrap(text, width=width, subsequent_indent=" " * (indent + 2)):
            out.append((part, bold))
    return out


def write_pdf(report: dict, prep: dict | None, path: Path) -> Path:
    who = report["player"]["label"]
    c = canvas.Canvas(str(path), pagesize=landscape(A4))
    c.setTitle(_latin1(f"Cheatsheet: {who}"))
    c.setAuthor("chess-prep")
    c.setSubject("Opponent preparation cheatsheet")
    c.setCreator(f"chess-prep {__version__}")

    c.setFillColor(colors.HexColor("#1f2d3d"))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN, PAGE_H - MARGIN - 12, _latin1(f"Opponent cheatsheet: {who}"))
    c.setFont("Helvetica", 8)
    c.setFillColor(colors.HexColor("#5a6b7b"))
    c.drawRightString(PAGE_W - MARGIN, PAGE_H - MARGIN - 12, f"generated {report['generated_at']} by chess-prep")

    top = PAGE_H - MARGIN - TITLE_H
    spots = [
        (MARGIN, top),
        (MARGIN + QUAD_W + GUTTER, top),
        (MARGIN, top - QUAD_H - GUTTER),
        (MARGIN + QUAD_W + GUTTER, top - QUAD_H - GUTTER),
    ]
    width_chars = int((QUAD_W - 10) / CHAR_W)
    max_lines = int((QUAD_H - HEADER_H - 8) / LINE_H)
    for (x, y), (title, body) in zip(spots, sections(report, prep), strict=True):
        c.setFillColor(colors.HexColor("#f6f8fa"))
        c.setStrokeColor(colors.HexColor("#d0d7de"))
        c.rect(x, y - QUAD_H, QUAD_W, QUAD_H, fill=True, stroke=True)
        c.setFillColor(colors.HexColor("#1f2d3d"))
        c.rect(x, y - HEADER_H, QUAD_W, HEADER_H, fill=True, stroke=False)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(x + 5, y - HEADER_H + 4.5, title)

        wrapped = _wrap(body, width_chars)
        if len(wrapped) > max_lines:
            wrapped = wrapped[: max_lines - 1] + [("  ... (more in cheatsheet.md)", False)]
        ty = y - HEADER_H - LINE_H - 1
        c.setFillColor(colors.black)
        for text, bold in wrapped:
            c.setFont("Courier-Bold" if bold else "Courier", BODY_SIZE)
            c.drawString(x + 5, ty, _latin1(text))
            ty -= LINE_H
    c.showPage()
    c.save()
    return path


MD_TITLES = {
    "WHEN THEY HAVE WHITE (you are Black)": "When they have White (you are Black)",
    "WHEN THEY HAVE BLACK (you are White)": "When they have Black (you are White)",
    "MISTAKES AND LOSSES": "Mistakes and losses",
    "KEY REMINDERS": "Key reminders",
}


def write_markdown(report: dict, prep: dict | None, path: Path) -> Path:
    out = [f"# Opponent cheatsheet: {report['player']['label']}", "", f"_Generated {report['generated_at']}_", ""]
    for title, body in sections(report, prep):
        out += [f"## {MD_TITLES.get(title, title)}", ""]
        in_list = False
        for text, bold in body:
            if not text:
                out.append("")
                in_list = False
                continue
            content = f"**{text.strip()}**" if bold else text.strip()
            if text.startswith("  ") or text.startswith("- "):
                out.append(f"- {content.removeprefix('- ')}")
                in_list = True
            else:
                if in_list:
                    out.append("")
                out.append(f"{content}  ")
                in_list = False
        out.append("")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


def write_text(report: dict, prep: dict | None, path: Path) -> Path:
    out = [f"OPPONENT CHEATSHEET: {report['player']['label']}", "=" * 60, ""]
    for title, body in sections(report, prep):
        out += [title, "-" * len(title)]
        out += [t for t, _ in _wrap(body, 72)]
        out.append("")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return path


def write_all(report: dict, prep: dict | None, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "pdf": write_pdf(report, prep, out_dir / "cheatsheet.pdf"),
        "md": write_markdown(report, prep, out_dir / "cheatsheet.md"),
        "txt": write_text(report, prep, out_dir / "cheatsheet.txt"),
    }
