import pytest

from chess_prep import config, engines

SF19 = [
    "stockfish-android-arm64-universal.tar.gz",
    "stockfish-linux-arm64-universal.tar.gz",
    "stockfish-linux-x86-64-universal.tar.gz",
    "stockfish-macos-universal.tar.gz",
    "stockfish-windows-arm64-universal.zip",
    "stockfish-windows-x86-64-universal.zip",
]
SF17 = [
    "stockfish-android-armv8.tar",
    "stockfish-macos-m1-apple-silicon.tar",
    "stockfish-macos-x86-64-avx2.tar",
    "stockfish-ubuntu-x86-64.tar",
    "stockfish-ubuntu-x86-64-avx2.tar",
    "stockfish-ubuntu-x86-64-bmi2.tar",
    "stockfish-windows-x86-64-avx2.zip",
    "stockfish-windows-x86-64.zip",
]


@pytest.mark.parametrize(
    "names, system, machine, flags, expected",
    [
        (SF19, "linux", "x86_64", set(), "stockfish-linux-x86-64-universal.tar.gz"),
        (SF19, "linux", "arm64", set(), "stockfish-linux-arm64-universal.tar.gz"),
        (SF19, "darwin", "arm64", set(), "stockfish-macos-universal.tar.gz"),
        (SF19, "windows", "x86_64", set(), "stockfish-windows-x86-64-universal.zip"),
        (SF17, "linux", "x86_64", {"avx2"}, "stockfish-ubuntu-x86-64-avx2.tar"),
        (SF17, "linux", "x86_64", set(), "stockfish-ubuntu-x86-64.tar"),
        (SF17, "darwin", "arm64", set(), "stockfish-macos-m1-apple-silicon.tar"),
        (SF17, "windows", "x86_64", set(), "stockfish-windows-x86-64.zip"),
    ],
)
def test_choose_stockfish_asset(names, system, machine, flags, expected):
    assert engines.choose_stockfish_asset(names, system, machine, flags) == expected


def test_choose_asset_unknown_platform():
    with pytest.raises(engines.EngineError):
        engines.choose_stockfish_asset(SF19, "linux", "riscv32", set())


def test_find_stockfish_from_config_and_env(fake_engine, monkeypatch):
    assert engines.find_stockfish() is None
    config.set_value("stockfish", str(fake_engine))
    assert engines.find_stockfish() == fake_engine.resolve()
    config.set_value("stockfish", None)
    monkeypatch.setenv("CHESSPREP_STOCKFISH", str(fake_engine))
    assert engines.find_stockfish() == fake_engine


def test_uci_name(fake_engine, tmp_path):
    assert engines.uci_name(fake_engine) == "FakeEngine"
    not_engine = tmp_path / "nope"
    not_engine.write_text("#!/bin/sh\necho hello\n")
    not_engine.chmod(0o755)
    with pytest.raises(engines.EngineError):
        engines.uci_name(not_engine)


def test_maia_weights_and_pick(tmp_path, monkeypatch):
    folder = tmp_path / "maia"
    folder.mkdir()
    for r in (1100, 1500, 1900):
        (folder / f"maia-{r}.pb.gz").write_bytes(b"0" * 150_000)
    (folder / "maia-1700.pb.gz").write_bytes(b"truncated")  # too small: ignored
    monkeypatch.setenv("CHESSPREP_MAIA_DIR", str(folder))
    found = engines.find_maia_weights()
    assert list(found) == [1100, 1500, 1900]
    assert engines.pick_maia(found, 1620)[0] == 1500
    assert engines.pick_maia(found, 2600)[0] == 1900
    assert engines.pick_maia({}, 1500) is None


def test_lc0_hint_mentions_build_on_linux():
    assert "build.sh" in engines.lc0_install_hint("linux")
    assert engines.lc0_install_hint("darwin") == "brew install lc0"
