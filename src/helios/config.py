# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""Locations and the strap's auth file."""

from __future__ import annotations

import contextlib
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _xdg(name: str, fallback: str) -> Path:
    # A value that isn't an absolute path is ignored, as the XDG spec says.
    value = os.environ.get(name, "")
    return Path(value) if value.startswith("/") else Path.home() / fallback


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "helios"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "helios"
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "helios"

AUTH_FILE = CONFIG_DIR / "auth.toml"
DATABASE = DATA_DIR / "helios.db"
LOCK_FILE = DATA_DIR / "daemon.lock"
STATE_FILE = RUNTIME_DIR / "state.json"
LIVE_FILE = RUNTIME_DIR / "live.json"
CONTROL_SOCKET = RUNTIME_DIR / "control.sock"


SETTINGS_FILE = CONFIG_DIR / "config.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    # Keep the connection open for live heart rate. Off: connect only to sync,
    # which spares the strap's battery at the cost of a live reading.
    live: bool = True
    sync_minutes: int = 10
    backfill_days: int = 14
    keep_days: int = 365


def _bool(raw: dict, key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{SETTINGS_FILE}: {key} must be true or false, without quotes")
    return value


def _int(raw: dict, key: str, default: int, low: int, high: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"{SETTINGS_FILE}: {key} must be a whole number")
    return max(low, min(high, value))


def load_settings(path: Path = SETTINGS_FILE) -> Settings:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return Settings()
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from None
    except OSError as error:
        raise ConfigError(f"{path} can't be read: {error.strerror or error}") from None
    defaults = Settings()
    return Settings(
        live=_bool(raw, "live", defaults.live),
        sync_minutes=_int(raw, "sync_minutes", defaults.sync_minutes, 2, 240),
        backfill_days=_int(raw, "backfill_days", defaults.backfill_days, 1, 60),
        keep_days=_int(raw, "keep_days", defaults.keep_days, 0, 100000),
    )


@dataclass(frozen=True)
class StrapAuth:
    address: str
    key: bytes


def load_auth(path: Path = AUTH_FILE) -> StrapAuth:
    try:
        with path.open("rb") as handle:
            strap = tomllib.load(handle).get("strap")
    except FileNotFoundError:
        raise ConfigError(f"{path} does not exist; run setup to fetch the strap's key") from None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from None
    except OSError as error:
        raise ConfigError(f"{path} can't be read: {error.strerror or error}") from None
    if not isinstance(strap, dict):
        raise ConfigError(f"{path}: needs a [strap] table holding address and auth_key")

    address = str(strap.get("address", "")).upper()
    key = str(strap.get("auth_key", "")).removeprefix("0x")
    if len(address.split(":")) != 6:
        raise ConfigError(f"{path}: [strap] address must look like AA:BB:CC:DD:EE:FF")
    try:
        key_bytes = bytes.fromhex(key)
    except ValueError:
        key_bytes = b""
    if len(key_bytes) != 16:
        raise ConfigError(f"{path}: [strap] auth_key must be 32 hex characters")
    return StrapAuth(address, key_bytes)


def save_auth(address: str, key: str, path: Path = AUTH_FILE) -> None:
    """Write the strap's address and key (32 hex characters) where the
    daemon reads them: a file only its owner can read, put in place whole."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = (
        "# Helios: Bluetooth auth key for your Amazfit Helio Strap. Keep this file private.\n"
        "# It changes whenever the strap is paired again in the Zepp app; run setup --auth then.\n"
        f'[strap]\naddress = "{address.upper()}"\nauth_key = "{key}"\n'
    )
    # mkstemp makes the file mode 600 whatever the umask is.
    handle, temporary = tempfile.mkstemp(prefix=".auth.", dir=path.parent)
    try:
        with os.fdopen(handle, "w") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
