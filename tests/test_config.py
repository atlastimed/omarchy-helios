import pytest

from helios.config import ConfigError, Settings, load_auth, load_settings


def test_missing_file_gives_defaults(tmp_path):
    assert load_settings(tmp_path / "none.toml") == Settings()


def test_values_are_clamped(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("live = false\nsync_minutes = 1\nbackfill_days = 999\n")
    settings = load_settings(path)
    assert (settings.live, settings.sync_minutes, settings.backfill_days) == (False, 2, 60)


@pytest.mark.parametrize("line", ['live = "false"', 'sync_minutes = "ten"', "sync_minutes = true"])
def test_wrong_types_are_reported(tmp_path, line):
    path = tmp_path / "config.toml"
    path.write_text(line + "\n")
    with pytest.raises(ConfigError):
        load_settings(path)


@pytest.mark.parametrize(
    "text",
    [
        'strap = "not a table"',
        "strap = 3",
        "strap = [1, 2]",
        "[other]\nkey = 1",
        "[strap]\naddress = 5\nauth_key = 7",
        '[strap]\naddress = "AA:BB:CC:DD:EE:FF"\nauth_key = "zz"',
        "",
    ],
)
def test_an_auth_file_of_the_wrong_shape_is_a_config_error(tmp_path, text):
    path = tmp_path / "auth.toml"
    path.write_text(text + "\n")
    with pytest.raises(ConfigError):
        load_auth(path)


def test_an_auth_file_that_is_not_text_is_a_config_error(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(ConfigError):
        load_auth(path)
    with pytest.raises(ConfigError):
        load_settings(path)


def test_a_good_auth_file_loads(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text('[strap]\naddress = "aa:bb:cc:dd:ee:ff"\nauth_key = "0x' + "ab" * 16 + '"\n')
    auth = load_auth(path)
    assert (auth.address, auth.key) == ("AA:BB:CC:DD:EE:FF", bytes.fromhex("ab" * 16))


def test_a_file_that_cannot_be_read_is_a_config_error(tmp_path):
    folder = tmp_path / "auth.toml"
    folder.mkdir()  # a folder where the file should be
    with pytest.raises(ConfigError, match="can't be read"):
        load_auth(folder)
    with pytest.raises(ConfigError, match="can't be read"):
        load_settings(folder)
    locked = tmp_path / "locked.toml"
    locked.write_text("[strap]\n")
    locked.chmod(0)
    try:
        with pytest.raises(ConfigError, match="can't be read"):
            load_auth(locked)
    finally:
        locked.chmod(0o600)


def test_relative_xdg_paths_are_ignored(monkeypatch):
    from pathlib import Path

    from helios import config

    monkeypatch.setenv("XDG_DATA_HOME", "relative/share")
    assert config._xdg("XDG_DATA_HOME", ".local/share") == Path.home() / ".local/share"
    monkeypatch.setenv("XDG_DATA_HOME", "/srv/data")
    assert config._xdg("XDG_DATA_HOME", ".local/share") == Path("/srv/data")
