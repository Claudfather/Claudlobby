#!/usr/bin/env python3
"""A stand-in for Claude Code's input box, for a test pane a send must land in.

The send presses Enter only once the box SHOWS the payload (#1236), so a pane
that never draws one (`cat`, `sleep`) is, correctly, never submitted to. This
draws a "> " prompt line and echoes every byte typed after it; a LF starts a new
line of the box, indented as Claude Code's are; Enter (CR) submits, and a fresh,
empty box is drawn under it. Raw mode, so no tty line limit applies (a
canonical-mode reader holds 1024 bytes on macOS) and a CR stays a CR.

--chrome draws the box as Claude Code does, with a border above and below the
prompt line and a footer under it, and keeps the cursor on the prompt line: for
a pane whose geometry is part of what it tests (a verify that read a fixed tail
of the pane once never reached the input line for exactly this reason). A submit
leaves the typed line above the next box, as the transcript does.
"""
import os
import sys
import tty

CHROME = "--chrome" in sys.argv[1:]
fd = sys.stdin.fileno()
tty.setraw(fd)
out = sys.stdout.buffer


def box():
    if CHROME:
        # ESC 7 saves the cursor on the prompt line and ESC 8 returns to it
        # once the chrome under it is drawn.
        out.write(b"\r\n--------\r\n> \x1b7\r\n--------\r\n\r\n  auto mode on\x1b8")
    else:
        out.write(b"> ")


box()
out.flush()
while True:
    data = os.read(fd, 4096)
    if not data:
        break
    for byte in data:
        if byte == 13:
            if CHROME:
                out.write(b"\x1b[J")  # the chrome under the typed text goes
            out.write(b"\r\n")
            box()
        elif byte == 10:
            out.write(b"\r\n  ")
        else:
            out.write(bytes([byte]))
    out.flush()
