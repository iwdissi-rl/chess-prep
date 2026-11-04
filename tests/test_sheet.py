import re
import shutil
import subprocess

from chess_prep.prep import build_prep
from chess_prep.report import build_report
from chess_prep.sheet import write_all


def test_cheatsheet_files(alice_db, tmp_path):
    rep = build_report(alice_db.db_path, alice_db)
    prep = build_prep(alice_db.db_path, rep, stockfish=None)
    files = write_all(rep, prep, tmp_path / "sheet")
    pdf = files["pdf"].read_bytes()
    assert pdf.startswith(b"%PDF")
    assert len(re.findall(rb"/Type /Page\b", pdf)) == 1  # exactly one page
    md = files["md"].read_text()
    txt = files["txt"].read_text()
    assert "alice (Lichess)" in md and "alice (Lichess)" in txt
    assert "Italian Game" in txt
    if shutil.which("pdftotext"):
        text = subprocess.run(["pdftotext", str(files["pdf"]), "-"], capture_output=True, text=True).stdout
        assert "Opponent cheatsheet: alice (Lichess)" in text


def test_cheatsheet_without_prep(alice_db, tmp_path):
    rep = build_report(alice_db.db_path, alice_db)
    files = write_all(rep, None, tmp_path / "sheet")
    assert files["pdf"].stat().st_size > 1000
