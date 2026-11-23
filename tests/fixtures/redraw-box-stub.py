#!/usr/bin/env python3
"""A Claude Code input box that redraws, for a test pane whose state changes during a send.

Like held-box-stub.py it draws the frame in argv[1] (a capture in Claude Code's layout: a
rule above and below the box, the glyph and its NBSP) and appends every byte it is sent to
argv[2]. It also draws what is typed into its box as Claude Code does, the first line after
the glyph and the rest indented below it, until a CR submits it and the box empties. And it
redraws whenever argv[1] is replaced, so a test can start a turn in the pane while a send
waits. Raw mode, so a CR arrives as a CR.
"""

import os
import select
import sys
import tty

frame, log = sys.argv[1], sys.argv[2]
fd = sys.stdin.fileno()
tty.setraw(fd)
out = sys.stdout.buffer
GLYPH = b"\xe2\x9d\xaf\xc2\xa0"  # the box's glyph and the no-break space after it, in UTF-8
typed = b""


def draw():
    with open(frame, "rb") as drawn:
        lines = drawn.read().split(b"\n")
    for i, line in enumerate(lines):
        if line.startswith(GLYPH) and typed:
            first, *rest = typed.split(b"\n")
            lines[i:i + 1] = [GLYPH + first] + [b"  " + more for more in rest]
            break
    out.write(b"\x1b[2J\x1b[H" + b"\r\n".join(lines))
    out.flush()


def stamp():
    st = os.stat(frame)
    return st.st_ino, st.st_mtime_ns


draw()
seen = stamp()
with open(log, "ab", buffering=0) as keys:
    while True:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if ready:
            data = os.read(fd, 4096)
            if not data:
                break
            keys.write(data)
            for byte in data:
                typed = b"" if byte == 13 else typed + bytes([byte])
            draw()
        now = stamp()
        if now != seen:
            seen = now
            draw()
