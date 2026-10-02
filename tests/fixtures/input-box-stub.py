#!/usr/bin/env python3
"""A stand-in for Claude Code's input box, for a test pane a send must land in.

The send presses Enter only once the box SHOWS the payload (#1236), so a pane
that never draws one (`cat`, `sleep`) is, correctly, never submitted to. This
draws a "> " prompt line and echoes every byte typed after it; a LF starts a new
line of the box, indented as Claude Code's are; Enter (CR) submits, and a fresh,
empty box is drawn under it. Raw mode, so no tty line limit applies (a
canonical-mode reader holds 1024 bytes on macOS) and a CR stays a CR.
"""
import os
import sys
import tty

fd = sys.stdin.fileno()
tty.setraw(fd)
out = sys.stdout.buffer
out.write(b"> ")
out.flush()
while True:
    data = os.read(fd, 4096)
    if not data:
        break
    for byte in data:
        if byte == 13:
            out.write(b"\r\n> ")
        elif byte == 10:
            out.write(b"\r\n  ")
        else:
            out.write(bytes([byte]))
    out.flush()
