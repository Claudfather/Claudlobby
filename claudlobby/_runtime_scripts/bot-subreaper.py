#!/usr/bin/env python3
"""Start a bot's tmux session under the bot's own child subreaper (#2158).

Every bot on a host runs as one user under one systemd user manager, and that
manager is the child subreaper that adopts an orphan by default. A kill aimed
at an orphaned job's parent therefore reached the manager, which stopped every
bot on the host. start-bot.sh starts its tmux client through this script
(bot_session_spawn in lib-common.sh). It sets PR_SET_CHILD_SUBREAPER and runs
the client; the tmux server daemonizes, so the kernel re-parents the server
here, and with it every process the session orphans.

    bot-subreaper.py CLIENT...   run the tmux client; CLIENT carries -P -F '#{pid}'
                                 so it prints the server's pid
    bot-subreaper.py reap        the long-lived phase it re-executes into

The process the caller starts prints one line,
`subreaper=PID server=PID adopted=yes|no`, and exits with the client's status.
That line is the contract: without it the client never ran (this process then
exits 125), and the caller runs the client itself.

The long-lived phase reaps every child, its own and adopted, and exits when it
has none left: a process that outlives the session keeps its parent here until
it ends, and the subreaper never outlives its last child. It ignores what a
stray kill sends, set after the client ran so tmux and the session inherit
nothing from it (execve keeps an ignored signal ignored), and it never sends a
signal.
"""

import os
import sys

PR_SET_CHILD_SUBREAPER = 36
NAME = b"bot-subreaper"  # its comm, which orphan-browser-reaper.sh reads
NOT_RUN = 125


def reap():
    """Reap every child until none is left."""
    try:
        with open("/proc/self/comm", "wb") as comm:
            comm.write(NAME)
    except OSError:
        pass
    while True:
        try:
            os.waitpid(-1, 0)
        except ChildProcessError:
            return 0


def parent(pid):
    with open(f"/proc/{pid}/stat") as stat:
        return int(stat.read().rsplit(")", 1)[1].split()[1])


def close_above(fd):
    """Close every descriptor above `fd`. SC_OPEN_MAX is -1 under an unlimited
    soft limit, which would make closerange a no-op and leave the caller's
    descriptors open for the session's life, so close from this process's
    own descriptor list then."""
    limit = os.sysconf("SC_OPEN_MAX")
    if limit > fd:
        os.closerange(fd + 1, limit)
        return
    for held in sorted(int(name) for name in os.listdir("/proc/self/fd")):
        if held > fd:
            try:
                os.close(held)
            except OSError:
                pass  # the listing's own descriptor, closed already


def keep(argv, report):
    """Become the subreaper, run the client, report on `report`, then reap."""
    import ctypes  # only this phase needs these: the reaper stays small
    import signal

    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 1)  # the caller reads this process's report, never its stdout
    flagged = ctypes.CDLL(None, use_errno=True).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    # A plain fork and exec, so the client gets exactly the dispositions and
    # descriptors this process got. (subprocess takes glibc's posix_spawn path
    # here, which leaves glibc's internal signals ignored in the child.)
    out_r, out_w = os.pipe()
    client = os.fork()
    if client == 0:
        try:
            os.dup2(out_w, 1)
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    os.close(out_w)
    status = os.waitstatus_to_exitcode(os.waitpid(client, 0)[1])
    status = 128 - status if status < 0 else status
    subreaper, server, adopted = "none", "unknown", "no"
    try:
        # What a stray kill of an orphan's parent sends. SIGCHLD stays default.
        for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM,
                    signal.SIGUSR1, signal.SIGUSR2, signal.SIGPIPE, signal.SIGALRM):
            signal.signal(sig, signal.SIG_IGN)
        os.set_blocking(out_r, False)
        out = os.read(out_r, 64).decode(errors="replace").strip()
        if out.isdigit():
            server = int(out)
            if flagged and parent(server) == os.getpid():
                subreaper, adopted = os.getpid(), "yes"
    except OSError:
        pass  # nothing printed, or the server already gone: not adopted
    finally:
        # Hold nothing the caller had open by the time the caller reads the report.
        os.dup2(null, 0)
        os.dup2(null, 2)
        os.closerange(3, report)
        close_above(report)
        os.write(report, f"{status} subreaper={subreaper} server={server} adopted={adopted}\n".encode())
        os.close(report)
    if not flagged:
        return 0
    while True:  # with no child left, nothing can be adopted any more
        try:
            if os.waitpid(-1, os.WNOHANG)[0] == 0:
                break
        except ChildProcessError:
            return 0
    try:
        os.execv(sys.executable, [sys.executable, "-I", "-B", "-S", os.path.abspath(__file__), "reap"])
    except OSError:
        return reap()


def launch(argv):
    """Relay the subreaper's report and the client's status to the caller."""
    r, w = os.pipe()
    if os.fork() == 0:
        os.close(r)
        code = 1
        try:
            code = keep(argv, w)
        finally:
            os._exit(code)
    os.close(w)
    report = b""
    while chunk := os.read(r, 4096):
        report += chunk
    status, _, line = report.decode().partition(" ")
    if not status.isdigit():
        return NOT_RUN
    sys.stdout.write(line)
    return int(status)


def main(argv):
    if argv == ["reap"]:
        return reap()
    if not argv:
        print("usage: bot-subreaper.py TMUX_CLIENT_ARGV... | reap", file=sys.stderr)
        return 2
    return launch(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
