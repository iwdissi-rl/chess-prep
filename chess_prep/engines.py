"""Find, check and download chess engines (Stockfish, Lc0 and Maia weights)."""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from chess_prep import config
from chess_prep.paths import engines_dir

STOCKFISH_RELEASES = "https://api.github.com/repos/official-stockfish/Stockfish/releases/latest"
LC0_RELEASES = "https://api.github.com/repos/LeelaChessZero/lc0/releases/latest"
MAIA_URL = "https://github.com/CSSLab/maia-chess/releases/download/v1.0/maia-{rating}.pb.gz"
MAIA_RATINGS = (1100, 1200, 1300, 1400, 1500, 1600, 1700, 1800, 1900)

EXE = ".exe" if os.name == "nt" else ""
COMMON_DIRS = [
    "/usr/games",
    "/usr/local/bin",
    "/opt/homebrew/bin",
    "/usr/bin",
    "/snap/bin",
    str(Path.home() / ".local" / "bin"),
]

_which = shutil.which  # indirection so tests can hide engines installed on the machine

ProgressFn = Callable[[int, int | None], None]  # (bytes so far, total or None)


class EngineError(RuntimeError):
    pass


# ------------------------------------------------------------------ discovery


def _is_executable(path: Path) -> bool:
    return path.is_file() and (os.name == "nt" or os.access(path, os.X_OK))


def _search(name: str, configured: str | None) -> Path | None:
    if configured:
        p = Path(configured).expanduser()
        return p if _is_executable(p) else None
    local = engines_dir() / f"{name}{EXE}"
    if _is_executable(local):
        return local
    on_path = _which(name)
    if on_path:
        return Path(on_path)
    for folder in COMMON_DIRS:
        p = Path(folder) / f"{name}{EXE}"
        if _is_executable(p):
            return p
    return None


def find_stockfish() -> Path | None:
    return _search("stockfish", config.get("stockfish"))


def find_lc0() -> Path | None:
    return _search("lc0", config.get("lc0"))


def maia_dir() -> Path:
    configured = config.get("maia_dir")
    return Path(configured).expanduser() if configured else engines_dir() / "maia"


def find_maia_weights() -> dict[int, Path]:
    found: dict[int, Path] = {}
    folder = maia_dir()
    if folder.is_dir():
        for f in folder.glob("maia-*.pb.gz"):
            m = re.fullmatch(r"maia-(\d{4})\.pb\.gz", f.name)
            if m and f.stat().st_size > 100_000:
                found[int(m.group(1))] = f
    return dict(sorted(found.items()))


def pick_maia(weights: dict[int, Path], rating: int | None) -> tuple[int, Path] | None:
    if not weights:
        return None
    target = rating or 1500
    best = min(weights, key=lambda r: abs(r - target))
    return best, weights[best]


def uci_name(path: Path, timeout: float = 15.0) -> str:
    """Start the engine, ask for its name, quit. Raises EngineError if it does not speak UCI."""
    try:
        proc = subprocess.run([str(path)], input="uci\nquit\n", capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise EngineError(f"{path} did not start: {exc}") from exc
    for line in proc.stdout.splitlines():
        if line.startswith("id name "):
            return line[len("id name ") :].strip()
    raise EngineError(f"{path} did not answer the UCI handshake")


@dataclass
class EngineSet:
    """Engines that are available on this machine right now."""

    stockfish: Path | None
    lc0: Path | None
    maia: dict[int, Path]

    @property
    def has_maia(self) -> bool:
        return bool(self.lc0 and self.maia)


def detect() -> EngineSet:
    return EngineSet(find_stockfish(), find_lc0(), find_maia_weights())


# ------------------------------------------------------------------ downloads


def system_and_arch() -> tuple[str, str]:
    system = platform.system().lower()  # linux / darwin / windows
    machine = platform.machine().lower()
    if machine in ("amd64", "x64", "x86-64"):
        machine = "x86_64"
    if machine in ("aarch64", "armv8", "arm64e"):
        machine = "arm64"
    return system, machine


def _cpu_flags() -> set[str]:
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return set()
    m = re.search(r"^flags\s*:\s*(.*)$", text, re.M)
    return set(m.group(1).split()) if m else set()


def choose_stockfish_asset(names: list[str], system: str, machine: str, cpu_flags: set[str] | None = None) -> str:
    """Pick the right download from a Stockfish release's asset names."""
    os_words = {"linux": ("linux", "ubuntu"), "darwin": ("macos", "mac"), "windows": ("windows",)}
    if system not in os_words:
        raise EngineError(f"No official Stockfish build for {system}. Install it with your package manager.")
    cands = [
        n
        for n in names
        if any(w in n for w in os_words[system]) and "android" not in n and n.endswith((".tar", ".tar.gz", ".zip"))
    ]
    if system == "darwin":
        uni = [n for n in cands if "universal" in n]
        if uni:
            return uni[0]
    arch_word = {"x86_64": "x86-64", "arm64": "arm"}.get(machine)
    if arch_word is None:
        raise EngineError(f"No official Stockfish build for {system}/{machine}.")
    if machine == "arm64" and system == "darwin":
        arch_cands = [n for n in cands if "apple-silicon" in n or "m1" in n or "arm" in n]
    else:
        arch_cands = [n for n in cands if arch_word in n]
    if not arch_cands:
        raise EngineError(f"This Stockfish release has no build for {system}/{machine}.")
    for n in arch_cands:
        if "universal" in n:
            return n
    flags = _cpu_flags() if cpu_flags is None else cpu_flags
    if "avx2" in flags:
        for n in arch_cands:
            if "avx2" in n:
                return n
    plain = [n for n in arch_cands if re.search(r"x86-64\.(tar|zip)|x86-64\.tar\.gz", n)]
    if plain:
        return plain[0]
    return min(arch_cands, key=len)


def _download(url: str, dest: Path, progress: ProgressFn | None = None) -> Path:
    with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "chess-prep"}) as resp:
        if resp.status_code != 200:
            raise EngineError(f"Download failed (HTTP {resp.status_code}): {url}")
        total = int(resp.headers.get("Content-Length") or 0) or None
        done = 0
        tmp = dest.with_suffix(dest.suffix + ".part")
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                fh.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        tmp.replace(dest)
    return dest


def _extract(archive: Path, into: Path) -> None:
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(into)
    else:
        with tarfile.open(archive) as tf:
            try:
                tf.extractall(into, filter="data")
            except TypeError:  # Python < 3.12
                tf.extractall(into)


def _find_binary(folder: Path, prefix: str) -> Path:
    cands = [
        p
        for p in folder.rglob(f"{prefix}*")
        if p.is_file() and p.suffix.lower() in ("", ".exe") and "." not in p.name.replace(".exe", "")[len(prefix) :]
    ]
    if not cands:
        raise EngineError(f"Could not find the {prefix} program inside the download")
    return max(cands, key=lambda p: p.stat().st_size)


def _release(api_url: str) -> dict:
    resp = requests.get(api_url, timeout=30, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code != 200:
        raise EngineError(f"GitHub answered HTTP {resp.status_code} for {api_url}")
    return resp.json()


def install_stockfish(progress: ProgressFn | None = None, log: Callable[[str], None] = print) -> Path:
    system, machine = system_and_arch()
    rel = _release(STOCKFISH_RELEASES)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    name = choose_stockfish_asset(list(assets), system, machine)
    log(f"Stockfish {rel.get('tag_name', '')}: {name}")
    target = engines_dir() / f"stockfish{EXE}"
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        archive = _download(assets[name], tmpdir / name, progress)
        _extract(archive, tmpdir / "x")
        binary = _find_binary(tmpdir / "x", "stockfish")
        shutil.copy2(binary, target)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    uci_name(target)
    config.set_value("stockfish", str(target))
    return target


def install_maia(progress: ProgressFn | None = None, log: Callable[[str], None] = print) -> list[Path]:
    folder = engines_dir() / "maia"
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for rating in MAIA_RATINGS:
        dest = folder / f"maia-{rating}.pb.gz"
        if dest.exists() and dest.stat().st_size > 100_000:
            out.append(dest)
            continue
        log(f"Maia {rating} weights")
        out.append(_download(MAIA_URL.format(rating=rating), dest, progress))
    config.set_value("maia_dir", str(folder))
    return out


def lc0_install_hint(system: str | None = None) -> str:
    system = system or system_and_arch()[0]
    if system == "darwin":
        return "brew install lc0"
    if system == "windows":
        return "chessprep setup-engines --lc0   (downloads the official Windows CPU build)"
    return (
        "Lc0 has no official Linux download. Build it from source:\n"
        "  sudo apt install meson ninja-build g++ libopenblas-dev zlib1g-dev\n"
        "  git clone --recurse-submodules https://github.com/LeelaChessZero/lc0.git\n"
        "  cd lc0 && ./build.sh\n"
        '  chessprep config set lc0 "$PWD/build/release/lc0"'
    )


def install_lc0(progress: ProgressFn | None = None, log: Callable[[str], None] = print) -> Path:
    system, machine = system_and_arch()
    if system != "windows" or machine != "x86_64":
        raise EngineError("No official Lc0 download for this system. " + lc0_install_hint(system))
    rel = _release(LC0_RELEASES)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    choice = next((n for n in assets if "windows-cpu-dnnl" in n), None) or next(
        (n for n in assets if "windows-cpu" in n), None
    )
    if not choice:
        raise EngineError("Could not find a CPU build of Lc0 in the latest release.")
    log(f"Lc0 {rel.get('tag_name', '')}: {choice}")
    folder = engines_dir() / "lc0-dist"
    with tempfile.TemporaryDirectory() as tmp:
        archive = _download(assets[choice], Path(tmp) / choice, progress)
        if folder.exists():
            shutil.rmtree(folder)
        _extract(archive, folder)
    binary = _find_binary(folder, "lc0")
    config.set_value("lc0", str(binary))
    return binary


def install_hint_stockfish(system: str | None = None) -> str:
    system = system or system_and_arch()[0]
    pkg = {
        "linux": "sudo apt install stockfish   (Debian/Ubuntu)",
        "darwin": "brew install stockfish",
        "windows": "download from https://stockfishchess.org/download/",
    }.get(system, "download from https://stockfishchess.org/download/")
    return f"chessprep setup-engines   or   {pkg}"
