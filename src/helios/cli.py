# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 atlastimed
"""helios: command-line entry points."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import json
import logging
import re
import socket
import subprocess
import sys
from datetime import datetime

from . import __version__
from .config import AUTH_FILE, CONTROL_SOCKET, save_auth


def _request(cmd: str) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        try:
            sock.connect(str(CONTROL_SOCKET))
            sock.sendall(json.dumps({"cmd": cmd}).encode() + b"\n")
            buffer = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    sys.exit("helios: the daemon closed the connection")
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if line.startswith(b'{"type":"state"'):
                        return json.loads(line)
        except TimeoutError:
            sys.exit("helios: the daemon did not answer")
        except OSError:
            sys.exit("helios: the daemon is not running (systemctl --user status helios)")


def _clock(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "—"


def _workout(w: dict) -> str:
    when = datetime.fromtimestamp(w["start"]).strftime("%a %H:%M")
    parts = [w.get("name") or "Workout", when, f"{round(w['seconds'] / 60)} min"]
    if w.get("avgHr"):
        parts.append(f"avg {w['avgHr']} bpm")
    if w.get("calories"):
        parts.append(f"{w['calories']} kcal")
    return ", ".join(parts)


def _status(as_json: bool) -> None:
    state = _request("state")
    if as_json:
        print(json.dumps(state, indent=2))
        return
    live = state.get("heartRate") or {}
    latest = state.get("latest") or {}
    today = state.get("today") or {}
    battery = state.get("battery") or {}
    sleep = state.get("sleep") or {}
    naps = state.get("naps") or []
    workouts = (state.get("workouts") or {}).get("recent") or []

    def reading(key: str, unit: str) -> str:
        item = latest.get(key)
        return f"{item['value']:g}{unit} at {_clock(item['at'])}" if item else "—"

    rows = [
        ("Status", f"{state['status']} {state.get('message') or ''}".strip()),
        ("Heart rate", f"{live['bpm']} bpm" if live else reading("heartRate", " bpm")),
        ("Resting HR", f"{today['restingHr']['value']:g} bpm" if today.get("restingHr") else "—"),
        ("HRV", reading("hrv", " ms")),
        ("Stress", reading("stress", "")),
        ("SpO2", reading("spo2", "%")),
        ("Skin temp", reading("temperature", " °C")),
        ("Steps today", "—" if today.get("steps") is None else str(today["steps"])),
        ("Active kcal", "—" if today.get("calories") is None else str(today["calories"])),
        ("PAI (7 days)", f"{today['pai']['total']:g}" if today.get("pai") else "—"),
        (
            "Last sleep",
            f"{sleep['asleepMinutes'] // 60}h {sleep['asleepMinutes'] % 60:02d}m, score {sleep['score']}"
            if sleep
            else "—",
        ),
        ("Naps since", ", ".join(f"{n['asleepMinutes'] // 60}h {n['asleepMinutes'] % 60:02d}m" for n in naps) or "—"),
        ("Last workout", _workout(workouts[0]) if workouts else "—"),
        ("Battery", f"{battery.get('level', '—')}%{' charging' if battery.get('charging') else ''}"),
        ("Last sync", _clock(state.get("lastSync"))),
    ]
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value}")


def _straps_seen() -> tuple[str, ...]:
    """The addresses of every Helio Strap this computer's Bluetooth has seen.
    That includes straps since replaced, and other people's."""
    try:
        listed = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    found = re.findall(
        r"^Device ((?:[0-9A-F]{2}:){5}[0-9A-F]{2}) .*Amazfit Helio Strap", listed, re.MULTILINE | re.IGNORECASE
    )
    return tuple(address.upper() for address in found)


def clean_line(text: str) -> str:
    """What was typed, without the control characters a terminal can leave in
    it (a Backspace it echoed instead of applying, say) or the space around it."""
    return "".join(ch for ch in text if ch.isprintable()).strip()


def _auth(address: str | None) -> None:
    """Fetch the strap's key from the owner's Zepp account and save it."""
    from . import zepp

    if not sys.stdin.isatty():
        sys.exit("helios: this asks for your Zepp email and password, so it needs a terminal")
    # With readline loaded, input() edits the line itself, so Backspace and
    # the arrow keys work whatever the terminal's own settings are.
    with contextlib.suppress(ImportError):
        import readline  # noqa: F401
    try:
        email = clean_line(input("Zepp account email: "))
        if not email:
            sys.exit("helios: no email entered")
        # Hidden as it is typed; sent only to Zepp, and kept nowhere.
        password = getpass.getpass("Zepp password (not shown as you type): ")
        if not password:
            sys.exit("helios: no password entered")
        print("Signing in to Zepp…")
        device = zepp.choose(zepp.fetch_devices(email, password), address, _straps_seen())
    except zepp.ZeppError as error:
        sys.exit(f"helios: {error}")
    except (EOFError, KeyboardInterrupt):
        sys.exit(130)
    save_auth(device.address, device.key)
    print(f"Saved the key for {device.address} to {AUTH_FILE}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="helios", description="Amazfit Helio Strap bridge for Omarchy.")
    parser.add_argument("--version", action="version", version=f"helios {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log protocol traffic")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon", help="run the Bluetooth bridge (normally started by systemd)")
    status = sub.add_parser("status", help="show the latest readings")
    status.add_argument("--json", action="store_true", help="print the full state as JSON")
    sub.add_parser("sync", help="fetch new history from the strap now")
    auth = sub.add_parser("auth", help="fetch the strap's key from your Zepp account (asks for your login)")
    auth.add_argument("address", nargs="?", help="the strap's address, if your account has several devices")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("bleak").setLevel(logging.WARNING)

    if args.command == "daemon":
        from .daemon import run_daemon

        asyncio.run(run_daemon())
    elif args.command == "status":
        _status(args.json)
    elif args.command == "sync":
        _request("sync")
        print("Sync requested.")
    elif args.command == "auth":
        _auth(args.address)
