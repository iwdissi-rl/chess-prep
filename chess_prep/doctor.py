"""``chessprep doctor``: check that everything needed is installed and say how to fix it."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from chess_prep import engines
from chess_prep.paths import data_dir, reports_root

OK, WARN, FAIL = "ok", "warn", "fail"

REQUIRED = [
    ("chess", "chess"),
    ("requests", "requests"),
    ("reportlab", "reportlab"),
    ("typer", "typer"),
    ("rich", "rich"),
    ("platformdirs", "platformdirs"),
]
OPTIONAL = [("flask", "flask", "browser board for practice (chessprep practice --web)")]


@dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""


def _version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return "?"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path):
            pass
        return True
    except OSError:
        return False


def python_hint(system: str) -> str:
    return {
        "linux": "sudo apt install python3.12 python3.12-venv   (Ubuntu 24.04+; older: use the deadsnakes PPA)",
        "darwin": "brew install python@3.12",
        "windows": "winget install Python.Python.3.12",
    }.get(system, "install Python 3.10 or newer from https://www.python.org/downloads/")


def run_checks(network: bool = True) -> list[Check]:
    system, machine = engines.system_and_arch()
    checks: list[Check] = []

    v = sys.version_info
    py = f"{v.major}.{v.minor}.{v.micro}"
    checks.append(
        Check("Python", OK, f"{py} on {system}/{machine}")
        if v >= (3, 10)
        else Check("Python", FAIL, f"{py} is too old (need 3.10+)", python_hint(system))
    )

    for module, dist in REQUIRED:
        try:
            importlib.import_module(module)
            checks.append(Check(f"package: {dist}", OK, _version(dist)))
        except ImportError:
            checks.append(Check(f"package: {dist}", FAIL, "not installed", "pip install ."))
    for module, dist, why in OPTIONAL:
        try:
            importlib.import_module(module)
            checks.append(Check(f"package: {dist}", OK, f"{_version(dist)} (optional)"))
        except ImportError:
            checks.append(Check(f"package: {dist}", WARN, f"not installed (optional: {why})", 'pip install ".[web]"'))

    sf = engines.find_stockfish()
    if sf:
        try:
            checks.append(Check("Stockfish", OK, f"{engines.uci_name(sf)} at {sf}"))
        except engines.EngineError as exc:
            checks.append(Check("Stockfish", FAIL, str(exc), engines.install_hint_stockfish(system)))
    else:
        checks.append(
            Check(
                "Stockfish",
                WARN,
                "not found (needed for 'analyze', engine prep and hints)",
                engines.install_hint_stockfish(system),
            )
        )

    lc0 = engines.find_lc0()
    if lc0:
        try:
            checks.append(Check("Lc0", OK, f"{engines.uci_name(lc0)} at {lc0}"))
        except engines.EngineError as exc:
            checks.append(Check("Lc0", WARN, str(exc), engines.lc0_install_hint(system)))
    else:
        checks.append(
            Check("Lc0", WARN, "not found (optional: lets the clone use Maia)", engines.lc0_install_hint(system))
        )

    maia = engines.find_maia_weights()
    if maia:
        checks.append(Check("Maia weights", OK, f"{', '.join(map(str, maia))} in {engines.maia_dir()}"))
    else:
        checks.append(Check("Maia weights", WARN, "not found (optional)", "chessprep setup-engines --maia"))

    for label, path in (("Data folder", data_dir()), ("Reports folder", reports_root())):
        if _writable(path):
            checks.append(Check(label, OK, str(path)))
        else:
            checks.append(Check(label, FAIL, f"{path} is not writable", "set CHESSPREP_HOME to a folder you own"))

    if network:
        import requests

        for name, url in (
            ("Chess.com API", "https://api.chess.com/pub/player/hikaru"),
            ("Lichess API", "https://lichess.org/api/status"),
        ):
            try:
                r = requests.get(url, timeout=8, headers={"User-Agent": "chess-prep doctor"})
                status = OK if r.status_code < 500 else WARN
                checks.append(Check(name, status, f"reachable (HTTP {r.status_code})"))
            except requests.RequestException as exc:
                proxy = " or set HTTPS_PROXY" if not os.environ.get("HTTPS_PROXY") else ""
                checks.append(
                    Check(name, WARN, f"not reachable: {type(exc).__name__}", f"check your internet connection{proxy}")
                )
    return checks
