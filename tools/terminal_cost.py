"""Measure the real terminal stream: start-up, idle, and what a key press costs.

    uv run python tools/terminal_cost.py [seconds] [--size 140x44] [--keys 2,6,1]

Unlike ``tools/ui_cost.py`` (which measures headlessly and deterministically), this runs
the real application in a pseudo-terminal that this script owns, so nothing can inject
input by accident and every byte counted is a byte a terminal would receive. Use it when
the dashboard *feels* slow: it says whether the app floods the terminal, and how much of
the flood is start-up, idle ticking, or a deliberate key press.

The pty is sized explicitly because the cost scales with the screen: a full repaint is
about 5 bytes per cell, so a 200x50 window costs three times a 140x44 one.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def parse_size(text: str) -> tuple[int, int]:
    columns, _, rows = text.partition("x")
    return int(columns), int(rows)


def parse_keys(text: str) -> list[str]:
    """Keys to press, in order, as literal text: ``2,6,1`` means three tab switches."""
    return [part for part in text.split(",") if part]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seconds", nargs="?", type=float, default=20.0)
    parser.add_argument("--size", default="140x44", help="terminal size, e.g. 140x44")
    parser.add_argument(
        "--keys",
        default="",
        help="keys to press, comma separated, one every two seconds (e.g. 2,6,1)",
    )
    parser.add_argument("--command", default="", help="override the command to run")
    args = parser.parse_args()

    columns, rows = parse_size(args.size)
    keys = parse_keys(args.keys)
    command = args.command or f"{sys.executable} -m sideboard"

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
    environment = dict(os.environ, TERM="xterm-256color", COLUMNS=str(columns), LINES=str(rows))
    process = subprocess.Popen(
        command.split(),
        cwd=REPO,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=environment,
        close_fds=True,
    )
    os.close(slave)

    writes: list[tuple[float, int]] = []
    started = time.perf_counter()
    next_key = started + 4.0
    pending = list(keys)
    while time.perf_counter() - started < args.seconds:
        ready, _, _ = select.select([master], [], [], 0.05)
        if ready:
            with contextlib.suppress(OSError):
                chunk = os.read(master, 65536)
                if chunk:
                    writes.append((time.perf_counter() - started, len(chunk)))
        if pending and time.perf_counter() >= next_key:
            os.write(master, pending.pop(0).encode())
            writes.append((time.perf_counter() - started, -1))  # marker: a key was sent
            next_key += 2.0
    process.terminate()
    with contextlib.suppress(Exception):
        process.wait(timeout=5)
    os.close(master)

    elapsed = max(0.001, time.perf_counter() - started)
    total = sum(size for _, size in writes if size > 0)
    print(f"terminal cost — {elapsed:.1f} s at {columns}x{rows}, {len(writes)} reads")
    print(f"  total written: {total / 1024:.0f} KiB ({total / elapsed / 1024:.1f} KiB/s average)")
    if not writes:
        print("  nothing was written: the app never started or never painted")
        return 1

    # Start-up: everything before the first quiet gap longer than 1.5 s.
    first = writes[0][0]
    startup_end = next(
        (at for at, size in writes if size > 0 and at - first > 1.5),
        elapsed,
    )
    startup = sum(size for at, size in writes if size > 0 and at <= startup_end)
    print(f"  start-up (first {startup_end:.1f} s): {startup / 1024:.0f} KiB")
    tail = [size for at, size in writes if size > 0 and at > elapsed - 5.0]
    print(f"  last 5 s: {sum(tail) / 1024 / min(5.0, elapsed):.1f} KiB/s over {len(tail)} reads")
    for index, key in enumerate(keys, start=1):
        marker = next(
            (at for at, size in writes if size == -1 and at > 0),
            0.0,
        )
        # Recompute per key: the nth marker is the nth key press.
        markers = [at for at, size in writes if size == -1]
        if index > len(markers):
            break
        at = markers[index - 1]
        after = [size for when, size in writes if size > 0 and at <= when <= at + 1.5]
        print(f"  key {key!r}: {sum(after) / 1024:.0f} KiB over {len(after)} reads in 1.5 s")
        _ = marker
    print()
    print("A full repaint is about 5 bytes per cell:")
    print(f"  one {columns}x{rows} screen ≈ {5 * columns * rows / 1024:.0f} KiB")
    print("If a key press costs more than about two screens, something is re-presenting")
    print("more than the visible tab; if the last 5 s are not near zero, the dashboard is")
    print("ticking when nothing has changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
