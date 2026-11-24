"""update-status's ingress read: the root ingress card's own predicate, applied to
what the system tailscaled serves, read without writing and never refusing."""
import ast
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types

import pytest

from tests import test_linux_system_preinstall as root_card
from tests.conftest import constructed_env
from tests.test_host_install import (  # noqa: F401 — the preparation fixture and its helpers
    REQUEST_ID, ROOT, SCRIPT, STATUS_KEYS, UPDATE_DRIVER, _prepared, local_install_source, payload, snapshot,
    update_frame)

NODE_KEY = "nodekey:" + "a" * 64
DNS = "fixture.example.ts.net"
TARGET = DNS + ":443"
HOME = "/home/user"
ROUTE = {"Proxy": "unix:" + HOME + "/.local/share/claudlobby-browser/data/state/owner/owner.sock"}
EXACT = {"TCP": {"443": {"HTTPS": True}}, "Web": {TARGET: {"Handlers": {"/": ROUTE}}}}
VERSION = root_card.payload().NATIVE_VERSION


def daemon_status(**changes):
    """The status the system tailscaled answers; `Self` changes merge into its own."""
    own = changes.pop("Self", {})
    value = {"Version": VERSION, "BackendState": "Running", "TUN": True,
             "Self": {"PublicKey": NODE_KEY, "DNSName": DNS + "."}, "CertDomains": [DNS]}
    value.update(changes)
    value["Self"] = {**value["Self"], **own}
    return value


SERVE = {
    "exact": (EXACT, "exact"),
    "exact_beside_other_routes": ({"TCP": {**EXACT["TCP"], "8443": {"HTTPS": True}},
                                   "Web": {**EXACT["Web"], "other.example.ts.net:8443": {"Handlers": {"/": {"Text": "other"}}}},
                                   "AllowFunnel": {"other.example.ts.net:8443": True},
                                   "Services": {"svc:other": {"Tun": True}}}, "exact"),
    "exact_funnel_off": ({**EXACT, "AllowFunnel": {TARGET: False}}, "exact"),
    "nothing_served": (None, "differs"),
    "empty": ({}, "differs"),
    "funnel_on": ({**EXACT, "AllowFunnel": {TARGET: True}}, "differs"),
    "extra_mount": ({**EXACT, "Web": {TARGET: {"Handlers": {"/": ROUTE, "/more": {"Text": "more"}}}}}, "differs"),
    "another_target": ({**EXACT, "Web": {**EXACT["Web"], "other.example.ts.net:443": {"Handlers": {"/": ROUTE}}}},
                       "differs"),
    "tcp_forward": ({**EXACT, "TCP": {"443": {"TCPForward": "localhost:22"}}}, "differs"),
    "plain_http": ({**EXACT, "TCP": {"443": {"HTTP": True}}}, "differs"),
    "another_account": ({**EXACT, "Web": {TARGET: {"Handlers": {"/": {
        "Proxy": "unix:/home/alice/.local/share/claudlobby-browser/data/state/owner/owner.sock"}}}}}, "differs"),
    "another_proxy": ({**EXACT, "Web": {TARGET: {"Handlers": {"/": {"Proxy": "http://localhost:8080"}}}}}, "differs"),
    "https_only": ({"TCP": EXACT["TCP"]}, "differs"),
    "route_only": ({"Web": EXACT["Web"]}, "differs"),
    "foreground": ({**EXACT, "Foreground": {"session": {}}}, "differs"),
    "unknown_field": ({**EXACT, "FutureField": {}}, "differs"),
}
STATUS = {
    "stopped": (daemon_status(BackendState="Stopped"), "differs"),
    "needs_login": (daemon_status(BackendState="NeedsLogin"), "differs"),
    "no_tun": (daemon_status(TUN=False), "differs"),
    "other_version": (daemon_status(Version="1.105.0"), "differs"),
    "other_node": (daemon_status(Self={"PublicKey": "nodekey:" + "d" * 64}), "differs"),
    "no_https": (daemon_status(CertDomains=None), "differs"),
    "other_certificate": (daemon_status(CertDomains=["other.example.ts.net"]), "differs"),
    "not_a_tailnet_name": (daemon_status(Self={"DNSName": "fixture.example.com."}), "differs"),
    "listed_name_outside_tailnet": (daemon_status(Self={"DNSName": "fixture.example.com."},
                                                  CertDomains=["fixture.example.com"]), "differs"),
}
CASES = {**{"serve_" + name: (daemon_status(), serve, expected) for name, (serve, expected) in SERVE.items()},
         **{"status_" + name: (status, EXACT, expected) for name, (status, expected) in STATUS.items()}}


def root_inspect(monkeypatch, status, serve, *, serve_etag="fixture-etag", profile="p001", shell="/bin/bash"):
    """The root card's own inspect against a fake daemon: (its plan, None), or (None,
    its refusal code)."""
    root = root_card.payload()
    with monkeypatch.context() as patch:
        patch.setattr(root.os, "geteuid", lambda: 0)
        patch.setattr(root.platform, "system", lambda: "Linux")
        patch.setattr(root.platform, "machine", lambda: "x86_64")
        patch.setattr(root.pwd, "getpwnam", lambda name: types.SimpleNamespace(
            pw_uid=1001, pw_gid=1001, pw_dir=HOME, pw_shell=shell))
        patch.setattr(root.Path, "read_text", lambda path: 'ID=ubuntu\nVERSION_ID="24.04"\n')
        patch.setattr(root.Path, "read_bytes", lambda path: b"synthetic-machine")

        class API:
            identity = {"pid": 123}

            def __call__(self, method, endpoint, body=None, etag=None):
                assert method == "GET"
                if endpoint == "profiles/current":
                    return 200, None, {"ID": profile}
                return 200, serve_etag, {"status": status, "serve-config": serve}[endpoint]
        try:
            return root.inspect_plan("ingress", root_card.selection(), API()), None
        except root.Refusal as error:
            return None, error.code


def root_verdict(monkeypatch, status, serve):
    """What the root card's own inspect makes of the same daemon: exact for its no_op,
    differs for a change or a refusal."""
    plan, _code = root_inspect(monkeypatch, status, serve)
    return "differs" if plan is None else "exact" if plan["no_op"] else "differs"


def host_verdict(monkeypatch, served, node_key=NODE_KEY, system="Linux", host=None):
    """The read, with `served` standing in for the daemon: endpoint to bytes or an exception."""
    host = host or payload()
    monkeypatch.setattr(host.platform, "system", lambda: system)
    reads = []

    def tailscale_get(endpoint, end):
        reads.append(endpoint)
        assert end <= time.monotonic() + host.INGRESS_SECONDS
        answer = served[endpoint]
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(host, "tailscale_get", tailscale_get)
    return host.ingress(node_key, Path(HOME), time.monotonic() + 15), reads


def _definitions(source):
    return {node.name: ast.get_source_segment(source, node) for node in ast.parse(source).body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))}


def test_the_predicate_is_the_root_cards_own_byte_for_byte():
    host, root = payload(), root_card.payload()
    text = root_card.SCRIPT.read_text()
    body = text.split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
    copied, original = _definitions(host._SERVE_PREDICATE_SOURCE), _definitions(body)
    assert set(copied) == {"Refusal", "require", "canonical", "unique", "decode", "closed_map", "serve_schema",
                           "ingress_after"}
    for name, source in copied.items():
        assert source == original[name], name
    # Nothing else runs in the copy: its two imports, json and re, then those definitions.
    rest = [node for node in ast.parse(host._SERVE_PREDICATE_SOURCE).body
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    assert [(type(node).__name__, [(alias.name, alias.asname) for alias in node.names]) for node in rest] == [
        ("Import", [("json", None)]), ("Import", [("re", None)])]
    # What the read shares with the root card beside the predicate.
    assert (host.NATIVE_VERSION, host.TAILSCALE_SOCKET, host.LOCAL_API_LIMIT) == (root.NATIVE_VERSION, root.SOCKET,
                                                                                  root.LIMIT)
    assert 're.fullmatch(r"' + host.DNS_NAME.pattern + '", dns)' in body
    # Both copies answer each case alike: the same route, or the same refusal.
    predicate = host._serve_predicate()
    for serve, _expected in SERVE.values():
        outcomes = []
        for after, refusal in ((predicate["ingress_after"], predicate["Refusal"]), (root.ingress_after, root.Refusal)):
            try:
                outcomes.append(after(serve, DNS, HOME))
            except refusal as error:
                outcomes.append(error.code)
        assert outcomes[0] == outcomes[1], serve


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_read_agrees_with_the_root_cards_own_inspect(name, monkeypatch):
    """With everything else the root card checks in order, exact exactly when its
    inspect finds nothing to change."""
    status, serve, expected = CASES[name]
    assert root_verdict(monkeypatch, status, serve) == expected
    verdict, reads = host_verdict(monkeypatch, {"status": json.dumps(status).encode(),
                                                "serve-config": json.dumps(serve).encode()})
    assert verdict == expected
    assert reads == (["status", "serve-config"] if name.startswith("serve_") else ["status"])


def test_a_listed_name_outside_the_tailnet_is_never_exact(monkeypatch):
    """CertDomains alone does not admit a name: the root card wants a ts.net name."""
    status = daemon_status(Self={"DNSName": "fixture.example.com."}, CertDomains=["fixture.example.com"])
    assert root_inspect(monkeypatch, status, EXACT) == (None, "native_dns_unavailable")
    verdict, reads = host_verdict(monkeypatch, {"status": json.dumps(status).encode(),
                                                "serve-config": json.dumps(EXACT).encode()})
    assert (verdict, reads) == ("differs", ["status"])


@pytest.mark.parametrize("change, code", [({"serve_etag": None}, "missing_etag"),
                                          ({"profile": "p002"}, "node_or_profile_changed"),
                                          ({"shell": "/usr/sbin/nologin"}, "account_changed")])
def test_exact_says_the_card_cannot_change_serve_not_that_it_would_succeed(change, code, monkeypatch):
    """The root card checks more than this read can: with Serve already exact it can
    still refuse, for an ETag the daemon left out, another profile or the account's shell."""
    assert root_inspect(monkeypatch, daemon_status(), EXACT, **change) == (None, code)
    verdict, _reads = host_verdict(monkeypatch, _good())
    assert verdict == "exact"


def test_the_route_is_this_accounts_own():
    """The same Serve configuration is exact for the account it names, differs for another."""
    host = payload()
    predicate = host._serve_predicate()
    assert predicate["ingress_after"](EXACT, DNS, HOME) == EXACT
    with pytest.raises(predicate["Refusal"]):
        predicate["ingress_after"](EXACT, DNS, "/home/alice")


def _good():
    return {"status": json.dumps(daemon_status()).encode(), "serve-config": json.dumps(EXACT).encode()}


UNKNOWN = {
    "no_daemon": {"status": OSError("no daemon")},
    "timed_out": {"status": TimeoutError()},
    "status_not_json": {"status": b"{not json"},
    "status_duplicate_key": {"status": b'{"TUN":true,"TUN":true}'},
    "status_not_an_object": {"status": b"[]"},
    "status_empty": {"status": b""},
    "no_self": {"status": json.dumps({key: value for key, value in daemon_status().items()
                                      if key != "Self"}).encode()},
    "tun_missing": {"status": json.dumps({key: value for key, value in daemon_status().items()
                                          if key != "TUN"}).encode()},
    "version_not_text": {"status": json.dumps(daemon_status(Version=1104)).encode()},
    "node_key_missing": {"status": json.dumps(daemon_status(Self={"PublicKey": None})).encode()},
    "cert_domains_not_a_list": {"status": json.dumps(daemon_status(CertDomains="x")).encode()},
    "serve_unreadable": {"serve-config": OSError("refused")},
    "serve_not_json": {"serve-config": b"{"},
    "serve_duplicate_key": {"serve-config": b'{"TCP":{},"TCP":{}}'},
}


@pytest.mark.parametrize("name", sorted(UNKNOWN))
def test_what_cannot_be_read_is_unknown(name, monkeypatch):
    verdict, _reads = host_verdict(monkeypatch, {**_good(), **UNKNOWN[name]})
    assert verdict == "unknown"


def test_the_actions_own_signals_still_end_it(monkeypatch):
    """SIGTERM, SIGINT and the budget alarm raise the action's own Refusal: the read
    passes it on rather than answer for a process that was told to stop."""
    host = payload()
    signalled = host.Refusal("unavailable", "unavailable", "inspect", 6)
    with pytest.raises(host.Refusal) as raised:
        host_verdict(monkeypatch, {"status": signalled}, host=host)
    assert raised.value is signalled


def test_off_linux_the_read_is_unknown_and_reads_nothing(monkeypatch):
    verdict, reads = host_verdict(monkeypatch, _good(), system="Darwin")
    assert (verdict, reads) == ("unknown", [])


def test_another_node_key_differs(monkeypatch):
    verdict, reads = host_verdict(monkeypatch, _good(), node_key="nodekey:" + "d" * 64)
    assert (verdict, reads) == ("differs", ["status"])


# The reader itself, against a stand-in daemon on a real Unix socket.

def _http(body, status="200 OK"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return (b"HTTP/1.1 " + status.encode() + b"\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(raw)).encode() + b"\r\n\r\n" + raw)


class StandInDaemon:
    """Answers each local API GET with `answers[endpoint]` (None: never answers) and keeps each request."""

    def __init__(self, answers):
        # A short folder of its own: a Unix socket path has a small limit.
        self.directory = tempfile.mkdtemp(prefix="ts-")
        self.path = os.path.join(self.directory, "tailscaled.sock")
        self.answers, self.requests, self.stop = answers, [], threading.Event()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(4)
        self.server.settimeout(0.2)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.stop.is_set():
            try:
                connection, _address = self.server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(5)
                request = b""
                try:
                    while b"\r\n\r\n" not in request:
                        part = connection.recv(4096)
                        if not part:
                            break
                        request += part
                except OSError:
                    continue
                if not request:
                    continue
                self.requests.append(request.decode("ascii"))
                answer = self.answers.get(request.split(b" ", 2)[1].decode().rsplit("/", 1)[-1])
                if answer is None:
                    self.stop.wait(10)
                    continue
                try:
                    connection.sendall(answer)
                except OSError:
                    continue

    def close(self):
        self.stop.set()
        self.thread.join(15)
        self.server.close()
        os.unlink(self.path)
        os.rmdir(self.directory)


@pytest.fixture
def daemon(monkeypatch):
    """The read pointed at a stand-in daemon; `trusted` admits its socket and peer as root's."""
    started = []

    def start(answers, *, trusted=True, socket_trusted=True, folders_trusted=False):
        host = payload()
        monkeypatch.setattr(host.platform, "system", lambda: "Linux")
        stand_in = StandInDaemon(answers)
        started.append(stand_in)
        if socket_trusted:
            monkeypatch.setattr(host, "_native_socket", lambda: Path(stand_in.path))
        else:
            monkeypatch.setattr(host, "TAILSCALE_SOCKET", stand_in.path)
            if folders_trusted:
                monkeypatch.setattr(host, "_root_folders", lambda path: None)
        if trusted:
            monkeypatch.setattr(host, "_root_daemon", lambda connection: None)
        return host, stand_in
    yield start
    for stand_in in started:
        stand_in.close()


def test_the_reader_asks_the_local_api_as_the_root_card_does(daemon):
    host, stand_in = daemon({"status": _http(daemon_status()), "serve-config": _http(EXACT)})
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "exact"
    lines = [request.split("\r\n") for request in stand_in.requests]
    assert [line[0] for line in lines] == ["GET /localapi/v0/status HTTP/1.1", "GET /localapi/v0/serve-config HTTP/1.1"]
    assert all("Host: local-tailscaled.sock" in line for line in lines)
    assert sorted(os.listdir(stand_in.directory)) == ["tailscaled.sock"]


def test_a_daemon_not_run_by_root_is_unknown_before_any_request(daemon):
    if os.geteuid() == 0:
        pytest.skip("the stand-in daemon runs as root here")
    host, stand_in = daemon({"status": _http(daemon_status()), "serve-config": _http(EXACT)}, trusted=False)
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "unknown"
    assert stand_in.requests == []


def test_a_socket_under_folders_that_are_not_roots_is_unknown_before_any_request(daemon):
    if os.geteuid() == 0:
        pytest.skip("the stand-in socket is root's here")
    host, stand_in = daemon({"status": _http(daemon_status()), "serve-config": _http(EXACT)}, socket_trusted=False)
    with pytest.raises(OSError):
        host._root_folders(Path(stand_in.path).resolve())
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "unknown"
    assert stand_in.requests == []
    host.TAILSCALE_SOCKET = stand_in.path + "-missing"
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "unknown"


def test_a_socket_that_is_not_roots_is_unknown_under_trusted_folders(daemon):
    """The socket's own owner check, reached on its own."""
    if os.geteuid() == 0:
        pytest.skip("the stand-in socket is root's here")
    host, stand_in = daemon({"status": _http(daemon_status()), "serve-config": _http(EXACT)}, socket_trusted=False,
                            folders_trusted=True)
    with pytest.raises(OSError, match="untrusted socket$"):
        host._native_socket()
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "unknown"
    assert stand_in.requests == []


@pytest.mark.parametrize("case", ["refused", "oversize", "not_http"])
def test_a_daemon_answer_the_root_card_would_not_take_is_unknown(daemon, case):
    status = {"refused": _http({"error": "denied"}, "403 Forbidden"),
              "oversize": _http(b" " * (payload().LOCAL_API_LIMIT + 1)), "not_http": b"not an answer\r\n\r\n"}[case]
    host, stand_in = daemon({"status": status, "serve-config": _http(EXACT)})
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "unknown"
    assert len(stand_in.requests) == 1


def test_a_silent_daemon_is_unknown_within_the_reads_own_time(daemon, monkeypatch):
    host, stand_in = daemon({"status": None})
    monkeypatch.setattr(host, "INGRESS_SECONDS", 0.5)
    started = time.monotonic()
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 15) == "unknown"
    assert time.monotonic() - started < 3
    assert len(stand_in.requests) == 1


def test_the_read_ends_before_the_actions_own_budget(daemon):
    """With 5 s or less of the action's budget left, the read asks nothing."""
    host, stand_in = daemon({"status": _http(daemon_status()), "serve-config": _http(EXACT)})
    assert host.ingress(NODE_KEY, Path(HOME), time.monotonic() + 5) == "unknown"
    assert stand_in.requests == []


# The action itself, on a prepared computer: the update driver with a stand-in reader.

STAND_IN_READER = '''served=json.loads(sys.argv[6]);reads=Path(sys.argv[7])
def tailscale_get(endpoint,end):
 with reads.open("a") as stream: stream.write(endpoint+"\\n")
 if endpoint not in served: raise OSError("unreadable")
 return json.dumps(served[endpoint]).encode()
m.tailscale_get=tailscale_get
'''
EXIT = "raise SystemExit(m.update_main(action))\n"
DRIVER = UPDATE_DRIVER.replace(EXIT, STAND_IN_READER + EXIT)


def invoke(tmp_path, home, request, served):
    assert DRIVER != UPDATE_DRIVER
    driver, log, reads = tmp_path / "ingress-driver.py", tmp_path / "children.jsonl", tmp_path / "tailscale-reads"
    driver.write_text(DRIVER)
    log.write_text("")
    reads.write_text("")
    result = subprocess.run([sys._base_executable, "-I", "-B", str(driver), str(SCRIPT), str(home), "update-status",
                             "{}", str(log), json.dumps(served), str(reads)], input=request,
                            env=constructed_env(PATH="/nonexistent", HOME=str(tmp_path / "untrusted-home"),
                                                PLANE_EMIT_DISABLED="1"), capture_output=True, timeout=120)
    assert result.stdout.count(b"\n") == 1 and len(result.stdout) <= 8192, (result.stdout, result.stderr)
    return result, json.loads(result.stdout), reads.read_text().split()


def _without_ingress(answer):
    return (json.dumps({key: value for key, value in answer.items() if key != "ingress"}, sort_keys=True,
                       separators=(",", ":")) + "\n").encode()


def _served(serve):
    return {"status": daemon_status(), "serve-config": serve}


def _exact_for(home):
    return root_card.payload().ingress_after(None, DNS, str(home))


def test_update_status_answers_ingress_only_when_asked_and_writes_nothing(tmp_path, local_install_source):
    _remote, _wheels, revision = local_install_source
    home, _family = _prepared(tmp_path, local_install_source, setup="started")
    exact = _exact_for(home)
    before = snapshot(home)
    # Without the key: today's answer, and nothing asks the daemon.
    result, plain, reads = invoke(tmp_path, home, update_frame(), _served(exact))
    assert result.returncode == 0 and set(plain) == STATUS_KEYS and reads == [], plain
    today = result.stdout
    # With it: today's bytes plus ingress, read last.
    result, answer, reads = invoke(tmp_path, home, update_frame(ingress_node_key=NODE_KEY), _served(exact))
    assert result.returncode == 0 and answer["ingress"] == "exact" and reads == ["status", "serve-config"], answer
    assert set(answer) == STATUS_KEYS | {"ingress"} and _without_ingress(answer) == today
    # Beside compare_revision, each answers.
    result, answer, _reads = invoke(tmp_path, home, update_frame(compare_revision=revision, ingress_node_key=NODE_KEY),
                                    _served(exact))
    assert result.returncode == 0 and (answer["relation"], answer["ingress"]) == ("same", "exact"), answer
    # A difference, and a daemon that cannot be read: answered, never refused.
    result, answer, _reads = invoke(tmp_path, home, update_frame(ingress_node_key=NODE_KEY),
                                    _served({**exact, "AllowFunnel": {TARGET: True}}))
    assert result.returncode == 0 and answer["ingress"] == "differs" and answer["state"] == "recorded", answer
    result, answer, reads = invoke(tmp_path, home, update_frame(ingress_node_key=NODE_KEY), {})
    assert result.returncode == 0 and answer["ingress"] == "unknown" and reads == ["status"], answer
    assert _without_ingress(answer) == today
    assert snapshot(home) == before


def test_update_status_on_an_absent_computer_answers_ingress_too(tmp_path):
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    before = snapshot(home)
    result, plain, reads = invoke(tmp_path, home, update_frame(), _served(_exact_for(home)))
    assert result.returncode == 0 and plain["state"] == "absent" and set(plain) == STATUS_KEYS and reads == []
    today = result.stdout
    result, answer, reads = invoke(tmp_path, home, update_frame(ingress_node_key=NODE_KEY),
                                   _served(_exact_for(home)))
    assert result.returncode == 0 and answer["ingress"] == "exact" and reads == ["status", "serve-config"], answer
    assert _without_ingress(answer) == today
    assert snapshot(home) == before


def _read(module, raw, action):
    reader, writer = os.pipe()
    os.write(writer, raw)
    os.close(writer)
    try:
        return module.request(reader, time.monotonic() + 10, action)
    finally:
        os.close(reader)


@pytest.mark.parametrize("key", ["nodekey:" + "A" * 64, "nodekey:" + "a" * 63, "nodekey:" + "a" * 65,
                                 "mkey:" + "a" * 64, "a" * 64, "", None, 1])
def test_update_status_refuses_a_malformed_node_key(key):
    module = payload()
    with pytest.raises(module.Refusal) as refused:
        _read(module, update_frame(ingress_node_key=key), "update-status")
    assert (refused.value.code, refused.value.exit_code) == ("invalid_request", 2)


@pytest.mark.parametrize("action, value", [
    ("update", {"expected_installed_revision": "a" * 40, "target_revision": "b" * 40}),
    ("update-continue", {"request_id": REQUEST_ID})])
def test_update_and_update_continue_refuse_the_ingress_key(action, value):
    """Their own frames are read; the same frame with the key is invalid_request."""
    module = payload()
    assert _read(module, update_frame(**value), action) == {"schema_version": 1, "expected_uid": os.getuid(), **value}
    with pytest.raises(module.Refusal) as refused:
        _read(module, update_frame(**value, ingress_node_key=NODE_KEY), action)
    assert (refused.value.code, refused.value.exit_code) == ("invalid_request", 2)


def test_the_carrier_document_names_the_ingress_key_and_its_answers():
    carrier = (ROOT / "documentation/architecture/owner-ssh-carrier.md").read_text()
    section = carrier.split("## Updating a started computer\n", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith("| `update-status`"))
    assert "`ingress_node_key` (optional)" in row and "`ingress`" in row
    for word in ("exact", "differs", "unknown"):
        assert f"`{word}`" in row, word
