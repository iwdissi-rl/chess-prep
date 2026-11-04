"""Tiny JSON config: remembers engine locations between runs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from chess_prep.paths import config_file

KEYS = {
    "stockfish": "Path to the Stockfish binary",
    "lc0": "Path to the Lc0 binary (only needed for Maia)",
    "maia_dir": "Folder that holds maia-XXXX.pb.gz weight files",
}
ENV_VARS = {
    "stockfish": "CHESSPREP_STOCKFISH",
    "lc0": "CHESSPREP_LC0",
    "maia_dir": "CHESSPREP_MAIA_DIR",
}


def load() -> dict[str, str]:
    path = config_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: str(v) for k, v in data.items() if k in KEYS and v}


def save(values: dict[str, str]) -> Path:
    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in values.items() if k in KEYS and v}
    path.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
    return path


def set_value(key: str, value: str | None) -> Path:
    if key not in KEYS:
        raise KeyError(f"Unknown setting {key!r}. Known settings: {', '.join(KEYS)}")
    values = load()
    if value:
        values[key] = str(Path(value).expanduser().resolve())
    else:
        values.pop(key, None)
    return save(values)


def get(key: str) -> str | None:
    """Environment variable first, then the config file."""
    env = os.environ.get(ENV_VARS[key])
    if env:
        return env
    return load().get(key)
