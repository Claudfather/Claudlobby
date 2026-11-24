"""The computer run (`host owner setup --stdio-computer`) and its admission.

The computer is tests/no_team_start.py's: the updater's own stages run, and only
the native layer, the operator gate and job dispatch are stand-ins. The run's
cards go through the typed guide's consent; the guide's own listener is a
stand-in that hands the pairing and workspace codes a browser would.
"""

from __future__ import annotations

from contextlib import contextmanager
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import tempfile
import threading
from types import SimpleNamespace

import pytest

from claudlobby import git_update as updater, resources
from claudlobby.command_result import CommandFailure
from claudlobby.commands import host_owner_setup as guide
from claudlobby.commands.owner_setup_consent import FixedOwnerProfile, StdioGuideConsent, admitted_update
from claudlobby.commands.owner_stdio import OwnerStdio, StdioRefused
from claudlobby.git_update import UpdateStore, read_update
from claudlobby.installation import _runtime_lock
from claudlobby.paths import Paths
from claudlobby.plane.owner_access import OwnerAccess
from claudlobby.plane.owner_source import start_state
from tests.no_team_start import Case, _commit
from tests.test_plane_owner_start import OWNER, _paired
from tests.test_plane_owner_workspace import ISSUER, ORIGIN, proposed
from tests.test_package_resources import _run

HOST = "https://host.example.test"
#: Fork S2-B's cards, in order.
CARDS = ["prepare_directories", "initialize_owner", "pair_owner", "initialize_link_storage",
         "bind_workspace", "start_workspace"]
NEVER = {"install_provider", "provider_signin", "allow_setup", "initialize_workspace", "initialize_connected"}


class Channel:
    """The typed channel's side the guide calls: every card recorded, approved unless declined."""

    def __init__(self, codes, *, decline=(), cancel=False):
        self.codes, self.decline, self.cards, self.states, self.events = codes, set(decline), [], [], []
        self.cancel, self.cancel_checks = cancel, 0

    def approve(self, action, scope, **kwargs):
        self.cards.append(action)
        self.events.append(("card", action))
        if action in self.decline:
            raise StdioRefused("declined")
        return kwargs.get("original", True)

    def token(self, kind):
        return self.codes[kind]

    def emit(self, kind, **body):
        self.states.append(body.get("state"))

    def check_cancel(self):
        self.cancel_checks += 1
        if self.cancel:
            raise StdioRefused("disconnected")


class Computer:
    """A fresh computer, its typed profile, and the guide's listener as a stand-in."""

    def __init__(self, tmp_path, monkeypatch, *, store=False):
        self.case = case = Case(tmp_path, monkeypatch)
        self.monkeypatch, self.tmp_path = monkeypatch, tmp_path
        if not store:
            for path in case.access.path.parent.glob(case.access.path.name + "*"):
                path.unlink()
        self.access = OwnerAccess(case.root)
        self.revision = case.target
        original = resources.get_resources
        # This computer's own package for the guide's reads; the updater asks for its own.
        monkeypatch.setattr(resources, "get_resources", lambda *args, **kwargs: (
            original(*args, **kwargs) if args or kwargs else case.package(self.revision)))
        monkeypatch.setattr(guide, "_WORKSPACE_ORIGINS", (ISSUER, ORIGIN))
        monkeypatch.setattr(guide, "LINUX_TAILSCALE", case.python)
        home = tmp_path / "home"
        home.mkdir(mode=0o700)
        family = home / ".local/share/claudlobby-browser"
        family.mkdir(parents=True, mode=0o700)
        profile = SimpleNamespace(runtime=None, units=case.directory, project=home / "projects/first-team",
            account=family / "provider-profile", policy=family / "host-policy/system.yaml",
            provider=family / "provider-bin/claude", data=case.root, receipt_revision=case.target,
            origin=lambda: HOST)
        profile.prepare_directories = lambda consent, paths, **kw: FixedOwnerProfile.prepare_directories(
            profile, consent, paths, **kw)
        profile.prepare_provider = lambda *_: pytest.fail("the computer run asked for Claude")
        self.profile = profile
        self.codes, self.listeners = {}, []
        monkeypatch.setattr(guide, "_serving", self._serving)

    @contextmanager
    def _serving(self, command, *_args, **_kwargs):
        self.listeners.append(command)
        owner = self.access.current_grant()
        if owner is None or not owner.active:
            self.codes["pairing"] = self.access.begin_pairing(OWNER).token
        else:
            self.codes["workspace"] = proposed((self.access, self.access.workspace(), owner,
                                                [self.access._now()])).nonce
        try:
            yield SimpleNamespace()
        finally:
            self.channel.events.append(("listener drained", len(self.listeners)))

    def paths(self):
        return Paths(self.case.root, package=self.case.package(self.revision))

    def run(self, *, decline=(), update_record=None, consent=None, cancel=False):
        self.channel = Channel(self.codes, decline=decline, cancel=cancel)
        return guide.run_computer(SimpleNamespace(update_record=update_record), self.paths(), self.access,
                                  consent=consent or StdioGuideConsent(self.channel), profile=self.profile)


def _files(root):
    return sorted((path, path.stat().st_mtime_ns) for path in root.rglob("*")
                  if path.is_file() and not path.name.endswith(("-shm", "-wal")))


@contextmanager
def lasting_service(computer):
    """The started computer's lasting service: answers `/owner` on the socket its settings name.

    The settings' socket is moved to a short path, because a Unix socket path
    under pytest's temporary directory can pass the platform's length limit.
    """
    import yaml
    directory = Path(tempfile.mkdtemp(prefix="cl-", dir="/tmp"))
    path = directory / "owner.sock"
    settings = computer.case.settings
    document = yaml.safe_load(settings.read_text())
    document["host"]["jobs"]["plane-owner"]["socket"] = str(path)
    settings.write_text(yaml.safe_dump(document))
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers["Host"]))
            self.send_response(200 if self.path == "/owner" else 404)
            self.end_headers()

        def log_message(self, *_args):
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

        def get_request(self):
            request, _ = super().get_request()
            return request, ("local", 0)

    server = Server(str(path), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield seen
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(directory, ignore_errors=True)


def test_a_fresh_computer_asks_the_computer_cards_and_returns_at_quiesce_before_the_cutover(tmp_path, monkeypatch):
    """In this process: the run returns at ``quiesce`` with nothing published, the
    guide's listeners close before the start card, and the updater waits on a
    reader lease. The lease here is the test's own; the run's own process and
    its lease are the next test's."""
    computer = Computer(tmp_path, monkeypatch)
    case = computer.case
    lease = _runtime_lock(case.installation)  # A reader lease, as the launcher holds one for the run.
    result = computer.run()
    channel = computer.channel
    assert channel.cards == CARDS and not NEVER & set(channel.cards)
    assert channel.states == ["pairing_ready", "workspace_ready"]
    # Both of the guide's listeners were drained before the start card was asked.
    assert channel.events[-1] == ("card", "start_workspace")
    assert ("listener drained", 2) in channel.events[:-1] and len(computer.listeners) == 2
    record = read_update(case.installation)
    assert result.data == {"state": "computer_starting", "request_id": record["request_id"],
                           "config_fingerprint": record["config_fingerprint"], "target": record["target"]}
    assert case.dispatches == [record["request_id"]] and record["phase"] == "quiesce"
    # The run returned with the start dispatched and nothing cut over: it never waited.
    assert "publish" not in case.trace and not (case.root / "state/config.json").exists()
    seen = []

    def run_exits():
        seen.append((read_update(case.installation)["phase"], "quiesce" in case.trace))
        os.close(lease)
    finished = case.run_job(at={3: run_exits})
    # The updater waited at its drain while a reader lease was held, and cut over
    # only after it was released. Counted in polls, never in seconds.
    assert seen == [("quiesce", False)] and case.polls == 3
    drain, cutover = finished["runtime_waits"]
    assert (drain["gate"], drain["outcome"], cutover["gate"], cutover["outcome"]) == (
        "drain", "taken", "cutover", "taken")
    assert finished["phase"] == "done" and finished["health_errors"] == {}
    assert start_state(case.root, case.installation) == "started"
    # The way back in: no card, no listener of a guide, and the lasting service answers.
    monkeypatch.setattr(guide, "_serving", lambda *_a, **_k: pytest.fail("a guide started a listener"))
    with lasting_service(computer) as requests:
        again = computer.run()
    assert again.data == {"state": "computer_ready", "origin": HOST}
    assert computer.channel.cards == [] and computer.channel.states == []
    assert requests and all(item == ("/owner", "host.example.test") for item in requests)
    assert case.dispatches == [record["request_id"]]


def test_the_typed_channel_carries_the_computer_runs_frames(tmp_path, monkeypatch):
    """The frames a client reads: fork S2-B's proposals, the ready states, then computer_starting."""
    computer = Computer(tmp_path, monkeypatch)
    incoming, client = os.pipe()
    observer, outgoing = os.pipe()
    channel = OwnerStdio(incoming, outgoing)
    frames = []

    def drive():
        seq = 0

        def send(kind, **body):
            nonlocal seq
            seq += 1
            os.write(client, (json.dumps({**body, "version": 1, "type": kind, "seq": seq,
                "session_id": channel.session_id}, sort_keys=True, separators=(",", ":")) + "\n").encode())
        with os.fdopen(observer, "rb", buffering=0) as stream:
            while True:
                line = b""
                while not line.endswith(b"\n"):
                    part = stream.read(1)
                    if not part:
                        return
                    line += part
                frame = json.loads(line)
                frames.append(frame)
                if frame["type"] == "hello":
                    send("start", profile="owner-setup-v1", expected_uid=501, source_revision="a" * 40,
                         binding_sha256="b" * 64, finish_setup=None, update_record=None)
                elif frame["type"] == "proposal":
                    send("decide", proposal_id=frame["proposal_id"], digest=frame["digest"], approve=True)
                elif frame.get("state") == "awaiting_pairing":
                    send("submit_pairing", challenge=computer.codes["pairing"])
                elif frame.get("state") == "awaiting_workspace":
                    send("submit_workspace", nonce=computer.codes["workspace"])
                elif frame["type"] in {"result", "error"}:
                    return
    thread = threading.Thread(target=drive)
    thread.start()
    computer.channel = Channel({})  # receives the listener stand-in's notes only
    try:
        channel.hello()
        assert channel.start(computer=True)["update_record"] is None
        result = guide.run_computer(SimpleNamespace(update_record=None), computer.paths(), computer.access,
                                    consent=StdioGuideConsent(channel), profile=computer.profile)
        channel.emit("result", **result.data)  # As owner_stdio.dispatch sends it.
    finally:
        thread.join(30)
        for fd in (incoming, outgoing, client):
            os.close(fd)
    assert not thread.is_alive()
    seen = [(frame["type"], frame["proposal"]["action"] if frame["type"] == "proposal" else frame.get("state"))
            for frame in frames]
    assert seen == [("hello", None), ("proposal", "prepare_directories"), ("proposal", "initialize_owner"),
                    ("state", "pairing_ready"), ("state", "awaiting_pairing"), ("proposal", "pair_owner"),
                    ("proposal", "initialize_link_storage"), ("state", "workspace_ready"),
                    ("state", "awaiting_workspace"), ("proposal", "bind_workspace"),
                    ("proposal", "start_workspace"), ("result", "computer_starting")]
    storage = next(frame["proposal"]["scope"] for frame in frames
                   if frame["type"] == "proposal" and frame["proposal"]["action"] == "initialize_link_storage")
    assert storage["storages"] == ["workspace", "connected"]
    assert sorted(frames[-1]) == ["config_fingerprint", "request_id", "seq", "session_id", "state", "target",
                                  "type", "version"]
    assert computer.access.connected().preparation_state() == "ready"


def test_a_computer_whose_start_is_under_way_asks_nothing_and_dispatches_nothing(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    first = computer.run()
    computer.case.job_state = "running"
    before = _files(computer.case.root)
    again = computer.run()
    assert again.data == first.data and computer.channel.cards == []
    assert computer.channel.states == [] and len(computer.listeners) == 2
    assert computer.case.dispatches == [first.data["request_id"]] and _files(computer.case.root) == before


def test_a_failed_start_is_reported_with_cores_line_and_continued_only_by_its_request(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    case = computer.case
    first = computer.run()
    case.run_job()
    with UpdateStore(case.installation) as store:
        record = store.load()
        record["health_errors"] = {"claudlobby-plane-daemon.service": "unit failed to start"}
        store.save(record)
    with pytest.raises(StdioRefused) as refused:
        computer.run()
    assert refused.value.code == "start_failed"
    assert refused.value.data["request_id"] == first.data["request_id"]
    assert f"host update {first.data['request_id']} did not finish" in refused.value.data["line"]
    assert computer.channel.cards == [] and case.dispatches == [first.data["request_id"]]
    # Continued over the account channel by its own request id, never by a guide run.
    updater.apply_or_continue(case.installation, expected_request_id=first.data["request_id"])
    case.run_job()
    assert start_state(case.root, case.installation) == "started"
    with lasting_service(computer):
        assert computer.run().data["state"] == "computer_ready"
    assert computer.channel.cards == []


def test_a_computer_run_after_an_earlier_one_asks_only_what_is_left(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    with pytest.raises(StdioRefused, match="declined"):
        computer.run(decline={"start_workspace"})
    assert computer.channel.cards == CARDS
    assert read_update(computer.case.installation) is None
    assert start_state(computer.case.root, computer.case.installation) == "fresh"
    result = computer.run()
    assert computer.channel.cards == ["start_workspace"] and computer.channel.states == []
    assert result.data["state"] == "computer_starting" and len(computer.listeners) == 2


def test_a_started_computer_whose_service_does_not_answer_is_start_failed(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    first = computer.run()
    computer.case.run_job()
    monkeypatch.setattr(guide, "READY_SECONDS", 0)
    with pytest.raises(StdioRefused) as refused:
        computer.run()
    assert refused.value.code == "start_failed"
    assert refused.value.data == {"line": guide._NOT_READY, "request_id": first.data["request_id"]}
    # And when the address native Tailscale reads is no longer the one the service serves.
    with lasting_service(computer) as requests:
        computer.profile.origin = lambda: "https://renamed.example.test"
        with pytest.raises(StdioRefused, match="start_failed"):
            computer.run()
    assert requests == [] and computer.channel.cards == []


@pytest.mark.parametrize("retained", ["allowed", "other"])
def test_a_computer_todays_guide_set_up_is_use_full_guide_with_nothing_written(tmp_path, monkeypatch, retained):
    computer = Computer(tmp_path, monkeypatch, store=True)
    case = computer.case
    if retained == "allowed":
        owner = _paired(case)
        case.access.connected().initialize(expected_owner=owner)
        from claudlobby.plane.owner_setup import OwnerSetup
        setup = OwnerSetup(case.access)
        setup.allow(setup.candidate(owner.principal, template="revenue-web-app", fleet="shop",
                                    repository="example-org/shop", install_directory=str(case.directory)))
    else:
        (case.root / "state/config.json").write_text("{}")
    before = _files(case.root)
    with pytest.raises(StdioRefused, match="use_full_guide"):
        computer.run()
    assert computer.channel.cards == [] and _files(case.root) == before
    assert read_update(case.installation) is None


def _moved(computer):
    """Start the computer at its receipt's revision, then update it to a later one."""
    case = computer.case
    computer.run()
    case.run_job()
    later = case.add_target()
    updated = case.update(later)
    assert updated["phase"] == "done" and updated["health_errors"] == {}
    computer.revision = later
    return updated["request_id"], later


def test_a_computer_moved_by_a_finished_update_is_admitted_only_when_the_run_names_it(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    request_id, later = _moved(computer)
    assert computer.profile.receipt_revision == computer.case.target != later
    for named in (None, ["00000000-0000-4000-8000-0000000000ff", later], [request_id, computer.case.target]):
        with pytest.raises(StdioRefused, match="installation_changed"):
            computer.run(update_record=named)
    with lasting_service(computer):
        assert computer.run(update_record=[request_id, later]).data == {"state": "computer_ready", "origin": HOST}
    assert computer.channel.cards == []


def test_admission_at_a_later_revision_checks_the_record_on_disk(tmp_path, monkeypatch):
    """The record and ancestry checks themselves, against the updater's own records."""
    computer = Computer(tmp_path, monkeypatch)
    case = computer.case
    request_id, later = _moved(computer)
    receipt = case.target
    named = [request_id, later]
    assert admitted_update(case.installation, receipt, later, named)["request_id"] == request_id
    refusals = {
        "nothing named": (receipt, later, None),
        "another id": (receipt, later, ["00000000-0000-4000-8000-0000000000ff", later]),
        "another target": (receipt, later, [request_id, receipt]),
        "not the installed revision": (receipt, receipt, named),
        "a receipt that is not an ancestor": ("f" * 40, later, named),
    }
    for reason, (revision, installed, value) in refusals.items():
        with pytest.raises(StdioRefused, match="installation_changed"):
            admitted_update(case.installation, revision, installed, value)
    with UpdateStore(case.installation) as store:
        record = store.load()
        record["health_errors"] = {"claudlobby-plane-daemon.service": "unit failed to start"}
        store.save(record)
    with pytest.raises(StdioRefused, match="installation_changed"):
        admitted_update(case.installation, receipt, later, named)


def test_a_checkout_moved_by_hand_is_not_admitted(tmp_path, monkeypatch):
    case = Case(tmp_path, monkeypatch)
    _run(["git", "-C", case.installation.source, "checkout", "--quiet", case.target], case.root)
    assert read_update(case.installation) is None
    with pytest.raises(StdioRefused, match="installation_changed"):
        admitted_update(case.installation, case.before, case.target,
                        ["00000000-0000-4000-8000-000000000001", case.target])


def test_the_runs_own_process_exits_and_frees_its_lease_before_the_updater_runs(tmp_path, monkeypatch):
    """A separate process holds the reader lease as the launcher holds it (released
    only by the kernel when the process ends), runs the computer run, and exits.
    The updater then finds the lease free and never waits. The launcher itself is
    not run here: its lease is the same ``_runtime_lock``."""
    import traceback
    computer = Computer(tmp_path, monkeypatch)
    case = computer.case
    reader, writer = os.pipe()
    pid = os.fork()
    if pid == 0:  # The run's own process.
        code = 1
        try:
            os.close(reader)
            _runtime_lock(case.installation)  # Never released by hand.
            os.write(writer, json.dumps(computer.run().data).encode())
            code = 0
        except BaseException:
            os.write(writer, traceback.format_exc().encode())
        finally:
            os._exit(code)
    os.close(writer)
    with os.fdopen(reader, "rb") as stream:
        said = stream.read().decode()
    _, status = os.waitpid(pid, 0)
    assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, said
    record = read_update(case.installation)
    assert json.loads(said) == {"state": "computer_starting", "request_id": record["request_id"],
                                "config_fingerprint": record["config_fingerprint"], "target": record["target"]}
    assert record["phase"] == "quiesce" and "publish" not in case.trace
    # The process is gone, and so is its lease: the cutover's exclusive lock is free now.
    os.close(_runtime_lock(case.installation, exclusive=True))
    finished = case.run_job()
    assert case.polls == 0 and finished["phase"] == "done" and finished["health_errors"] == {}


class _Once:
    """`start_state` says ``value`` the first time, then what core reads."""

    def __init__(self, monkeypatch, value):
        from claudlobby.plane import owner_source
        self.real, self.value, self.calls = owner_source.start_state, value, 0
        monkeypatch.setattr(owner_source, "start_state", self)

    def __call__(self, *args):
        self.calls += 1
        return self.value if self.calls == 1 else self.real(*args)


def test_a_start_whose_record_is_gone_when_read_asks_nothing_and_dispatches_nothing(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    with pytest.raises(StdioRefused, match="declined"):
        computer.run(decline={"start_workspace"})  # Paired and linked; nothing started.
    before = _files(computer.case.root)
    once = _Once(monkeypatch, "starting")
    with pytest.raises(StdioRefused) as refused:
        computer.run()
    assert refused.value.code == "unavailable" and once.calls == 2
    assert computer.channel.cards == [] and computer.channel.states == []
    assert computer.case.dispatches == [] and read_update(computer.case.installation) is None
    # Core's reader takes the update lock to read; its lock file is all that is new.
    assert [item for item in _files(computer.case.root) if item[0].name != "update.lock"] == before


def test_a_start_read_as_under_way_that_has_finished_is_computer_ready(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    first = computer.run()
    computer.case.run_job()
    _Once(monkeypatch, "starting")
    with lasting_service(computer):
        assert computer.run().data == {"state": "computer_ready", "origin": HOST}
    assert computer.channel.cards == [] and computer.case.dispatches == [first.data["request_id"]]


def test_a_fresh_run_whose_computer_was_started_meanwhile_is_unavailable(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    first = computer.run()
    computer.case.run_job()
    _Once(monkeypatch, "fresh")
    with pytest.raises(StdioRefused) as refused:
        computer.run()
    assert refused.value.code == "unavailable"
    assert "start_workspace" not in computer.channel.cards
    assert computer.case.dispatches == [first.data["request_id"]]


def test_a_start_that_fails_after_approval_is_asked_again_with_a_new_settings_plan(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    original = updater._prepare_update_locked
    failures = []

    def prepare(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise RuntimeError("stopped after the settings were saved")
        return original(*args, **kwargs)
    monkeypatch.setattr(updater, "_prepare_update_locked", prepare)
    with pytest.raises(CommandFailure, match="settings were saved and nothing was started"):
        computer.run()
    assert computer.channel.cards == CARDS and read_update(computer.case.installation) is None
    result = computer.run()  # Its settings plan is built again from the saved file.
    assert computer.channel.cards == ["start_workspace"] and computer.channel.states == []
    record = read_update(computer.case.installation)
    assert result.data["state"] == "computer_starting" and result.data["request_id"] == record["request_id"]
    assert computer.case.dispatches == [record["request_id"]]


@pytest.mark.parametrize("declined", CARDS[:-1])
def test_a_declined_card_is_asked_again_with_exactly_the_rest(tmp_path, monkeypatch, declined):
    computer = Computer(tmp_path, monkeypatch)
    with pytest.raises(StdioRefused, match="declined"):
        computer.run(decline={declined})
    rest = CARDS[CARDS.index(declined):]
    assert computer.channel.cards == CARDS[:CARDS.index(declined) + 1]
    result = computer.run()
    assert computer.channel.cards == rest and result.data["state"] == "computer_starting"


def test_a_retained_launch_whose_approval_is_withdrawn_is_use_full_guide(tmp_path, monkeypatch):
    import sqlite3
    computer = Computer(tmp_path, monkeypatch, store=True)
    case = computer.case
    owner = _paired(case)
    case.access.connected().initialize(expected_owner=owner)
    from claudlobby.plane.owner_setup import OwnerSetup
    setup = OwnerSetup(case.access)
    setup.allow(setup.candidate(owner.principal, template="revenue-web-app", fleet="shop",
                                repository="example-org/shop", install_directory=str(case.directory)))
    with sqlite3.connect(case.access.path) as db:
        db.execute("UPDATE setup_grant SET active=0, launch_id=?, launch_plan=? WHERE singleton=1",
                   ("00000000-0000-4000-8000-0000000000aa", "d" * 64))
    retained = setup.retained_state()
    assert retained.state == "revoked" and retained.launch_id is not None
    before = _files(case.root)
    with pytest.raises(StdioRefused, match="use_full_guide"):
        computer.run()
    assert computer.channel.cards == [] and _files(case.root) == before


def test_waiting_for_the_started_service_stops_when_the_client_leaves(tmp_path, monkeypatch):
    computer = Computer(tmp_path, monkeypatch)
    computer.run()
    computer.case.run_job()
    with pytest.raises(StdioRefused) as refused:
        computer.run(cancel=True)  # Nothing answers; the full wait would end in start_failed.
    assert refused.value.code == "disconnected" and computer.channel.cancel_checks == 1


def test_admission_refuses_a_receipt_on_a_sibling_branch(tmp_path, monkeypatch):
    import subprocess
    computer = Computer(tmp_path, monkeypatch)
    case = computer.case
    request_id, later = _moved(computer)
    source = str(case.installation.source)
    tree = subprocess.run(["git", "-C", source, "rev-parse", f"{case.before}^{{tree}}"],
                          capture_output=True, text=True, check=True).stdout.strip()
    sibling = subprocess.run(["git", "-C", source, "commit-tree", tree, "-p", case.before, "-m", "Sibling"],
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "GIT_AUTHOR_NAME": "Example", "GIT_AUTHOR_EMAIL": "example@example.test",
                                  "GIT_COMMITTER_NAME": "Example", "GIT_COMMITTER_EMAIL": "example@example.test"}
                             ).stdout.strip()
    # A real commit that git knows, and not an ancestor: git answers 1, not 128.
    assert subprocess.run(["git", "-C", source, "merge-base", "--is-ancestor", sibling, later]).returncode == 1
    with pytest.raises(StdioRefused, match="installation_changed"):
        admitted_update(case.installation, sibling, later, [request_id, later])


@pytest.mark.parametrize("named", ["x", 7, [], {}, [None, None], [1, 2]])
def test_admission_refuses_a_malformed_update_record(tmp_path, monkeypatch, named):
    computer = Computer(tmp_path, monkeypatch)
    _request_id, later = _moved(computer)
    with pytest.raises(StdioRefused, match="installation_changed"):
        admitted_update(computer.case.installation, computer.case.target, later, named)


@pytest.mark.parametrize("damage", ["symlink", "unreadable", "malformed", "not an object"])
def test_admission_refuses_a_record_it_cannot_read(tmp_path, monkeypatch, damage):
    computer = Computer(tmp_path, monkeypatch)
    request_id, later = _moved(computer)
    path = computer.case.root / "state/update.json"
    if damage == "symlink":
        elsewhere = tmp_path / "update.json"
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
    elif damage == "unreadable":
        path.chmod(0)
    elif damage == "malformed":
        path.write_text("{")
    else:
        path.write_text("[]")
    try:
        with pytest.raises(StdioRefused, match="installation_changed"):
            admitted_update(computer.case.installation, computer.case.target, later, [request_id, later])
    finally:
        if path.exists() and not path.is_symlink():
            path.chmod(0o600)


@pytest.mark.parametrize("field, value", [
    ("health_errors", None), ("health_errors", []), ("maintenance", True), ("maintenance", None),
    ("config_committed", False), ("config_committed", None), ("stage_error", "render failed"),
])
def test_admission_reads_a_finished_record_as_strictly_as_todays_finish(tmp_path, monkeypatch, field, value):
    computer = Computer(tmp_path, monkeypatch)
    request_id, later = _moved(computer)
    record = read_update(computer.case.installation)
    # A real finished start and a real finished update both meet every condition.
    assert (record["maintenance"], record["config_committed"], record.get("stage_error"), record["health_errors"]) == (
        False, True, None, {})
    path = computer.case.root / "state/update.json"
    document = json.loads(path.read_text())
    document[field] = value
    path.write_text(json.dumps(document))
    if field == "maintenance":  # Core's own read already refuses a done record in maintenance.
        with pytest.raises(updater.UpdateError):
            read_update(computer.case.installation)
    else:  # Core reads it; the admission's own condition is what refuses.
        assert read_update(computer.case.installation)[field] == value
    with pytest.raises(StdioRefused, match="installation_changed"):
        admitted_update(computer.case.installation, computer.case.target, later, [request_id, later])


def test_computer_run_after_link_still_ready(tmp_path, monkeypatch):
    """A link approval is its own record (setup_link), never a team approval: the computer run
    still reads a started computer with no team setup, asks nothing and answers computer_ready."""
    import hashlib
    from claudlobby.plane import provider_signin
    from claudlobby.plane.owner_setup import OwnerSetup
    computer = Computer(tmp_path, monkeypatch)
    computer.run()
    computer.case.run_job()
    assert start_state(computer.case.root, computer.case.installation) == "started"
    digest = hashlib.sha256(b"a synthetic provider").hexdigest()
    monkeypatch.setattr(provider_signin, "SUPPORTED", {digest: "2.1.325"})
    scope = {"binary": str(tmp_path / "provider/claude"), "sha256": digest, "version": "2.1.325",
             "home": str(tmp_path / "home"), "account_directory": str(tmp_path / "home/.claude"),
             "environment": str(computer.case.root / ".env")}
    approval = provider_signin.LinkStore(computer.access).write(scope)
    with computer.access._connection() as conn:
        assert provider_signin.LinkStore._read(conn) == approval
    assert OwnerSetup(computer.access).retained_state().state == "absent"
    monkeypatch.setattr(guide, "_serving", lambda *_a, **_k: pytest.fail("a guide started a listener"))
    with lasting_service(computer):
        again = computer.run()
    assert again.data == {"state": "computer_ready", "origin": HOST}
    assert computer.channel.cards == [] and computer.channel.states == []
