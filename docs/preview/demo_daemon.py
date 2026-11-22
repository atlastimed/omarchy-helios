"""Serve a demo state (demo_state.py) on Helios's socket, in place of the
service, with a pulse that keeps ticking. It refuses to start while the real
service is answering there: stop helios.service first.

    python3 docs/preview/demo_daemon.py <state.json>
"""

import asyncio
import contextlib
import json
import os
import socket
import sys
import time
from pathlib import Path

data = json.loads(Path(sys.argv[1]).read_text())
runtime = os.environ.get("XDG_RUNTIME_DIR", "")
sock = Path(runtime if runtime.startswith("/") else f"/run/user/{os.getuid()}") / "helios" / "control.sock"
sock.parent.mkdir(exist_ok=True)
if sock.exists():
    # A socket something answers on is the real service's: taking its place
    # would leave it running with nobody able to reach it.
    with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(1)
        probe.connect(str(sock))
        sys.exit(
            "demo_daemon: a Helios service is answering on its socket; stop it first (systemctl --user stop helios)"
        )
    sock.unlink()
clients = set()


async def client(reader, writer):
    clients.add(writer)
    writer.write((json.dumps(data["connected"]) + "\n").encode())
    try:
        while line := await reader.readline():
            print("cmd", line.decode().strip(), flush=True)
    finally:
        clients.discard(writer)


async def main():
    await asyncio.start_unix_server(client, path=str(sock))
    while True:
        await asyncio.sleep(5)
        for w in list(clients):
            w.write((json.dumps({"type": "hr", "bpm": 68, "at": int(time.time())}) + "\n").encode())


asyncio.run(main())
