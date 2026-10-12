#!/usr/bin/env python3
"""A pane that shows one fixed frame, such as Claude Code's usage-limit menu,
and records every byte typed into it.

A menu reads typed keys as choices: a digit can choose an option by its number
and a line feed can confirm the one under the pointer. So the property under
test is that a send types nothing at all into a menu, and the record is raw:
--keys PATH appends each byte read, exactly as it arrived (raw mode, so a LF
or a CR arrives as itself), and an empty file afterwards is the proof.

Usage: menu-pane-stub.py FRAME --keys PATH
"""
import os
import sys
import tty

frame, keys = sys.argv[1], sys.argv[sys.argv.index("--keys") + 1]
tty.setraw(sys.stdin.fileno())
with open(frame, "rb") as fh:
    # Raw mode turns off output processing, so each line ends with its own CR.
    sys.stdout.buffer.write(fh.read().rstrip(b"\n").replace(b"\n", b"\r\n"))
sys.stdout.buffer.flush()
record = os.open(keys, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
while True:
    data = os.read(sys.stdin.fileno(), 1024)
    if not data:
        break
    os.write(record, data)
