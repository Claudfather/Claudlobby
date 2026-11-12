#!/usr/bin/env python3
"""A held Claude Code input box for one test pane (#2105).

Draws the frame in argv[1] (a capture in Claude Code's layout: a rule above and
below the box, the glyph and its NBSP) and then appends every byte it is sent to
argv[2], drawing nothing more: the box stays held, so the test reads exactly
which keys a repair sent. Raw mode, so a CR arrives as a CR.
"""

import os
import sys
import tty

frame, log = sys.argv[1], sys.argv[2]
fd = sys.stdin.fileno()
tty.setraw(fd)
out = sys.stdout.buffer
with open(frame, "rb") as drawn:
    out.write(drawn.read().replace(b"\n", b"\r\n"))
out.flush()
with open(log, "ab", buffering=0) as keys:
    while True:
        data = os.read(fd, 4096)
        if not data:
            break
        keys.write(data)
