"""Private-UDS verifier tests; synthetic schema grounded in a local WhoIs capture.

The capture had integer UserProfile.ID == Node.User, two string Addresses,
Tags/Expired absent and official daemon ControlURL. No captured identifiers,
IPs, login names, or other private metadata are included here. These tests do
not establish installed Serve header replacement or real UDS reachability.
"""

import asyncio
import copy
import os
import signal
from pathlib import Path
import sys

import pytest

from claudlobby.plane import owner_ingress
from claudlobby.plane.owner_access import AccessDenied, AccessUnavailable, PrincipalRef
from claudlobby.plane.owner_ingress import ServePrincipalVerifier
from tests.conftest import constructed_env

SOURCE = '100.64.0.42'
PREFS = {'ControlURL': 'https://controlplane.tailscale.com'}
WHOIS = {'Node': {'User': 123456789, 'Addresses': ['100.64.0.42/32', 'fd7a:115c:a1e0::42/128']},
         'UserProfile': {'ID': 123456789}, 'CapMap': {}}


def scope(*, client=None, headers=None):
    return {'type': 'http', 'client': client,
            'headers': headers if headers is not None else [(b'x-forwarded-for', SOURCE.encode())]}


def verifier(monkeypatch, replies=None):
    result = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    replies = list(replies if replies is not None else [PREFS, WHOIS, PREFS])
    calls = []
    async def command(*args):
        calls.append(args)
        return copy.deepcopy(replies.pop(0))
    monkeypatch.setattr(result, '_command', command)
    return result, calls


def test_verified_numeric_human_uses_fixed_namespace_and_checks_realm_each_admission(monkeypatch):
    verify, calls = verifier(monkeypatch, [PREFS, WHOIS, PREFS] * 2)
    async def run():
        assert await verify(scope()) == PrincipalRef('tailscale:controlplane.tailscale.com', '123456789')
        assert await verify(scope()) == PrincipalRef('tailscale:controlplane.tailscale.com', '123456789')
    asyncio.run(run())
    assert calls == [('debug', 'prefs'), ('whois', '--json', '--proto=tcp', SOURCE), ('debug', 'prefs')] * 2


@pytest.mark.parametrize('client', [('127.0.0.1', 1234), ('::1', 1234), '', '/tmp/peer.sock'])
def test_tcp_or_ambiguous_asgi_peer_is_denied_before_cli(monkeypatch, client):
    verify, calls = verifier(monkeypatch)
    with pytest.raises(AccessDenied, match='owner_ingress_not_unix'):
        asyncio.run(verify(scope(client=client)))
    assert calls == []


@pytest.mark.parametrize('headers', [[], [(b'x-forwarded-for', b'')],
    [(b'x-forwarded-for', SOURCE.encode()), (b'X-Forwarded-For', SOURCE.encode())],
    [(b'x-forwarded-for', b'100.64.0.42,100.64.0.43')],
    [(b'x-forwarded-for', b' 100.64.0.42')], [(b'x-forwarded-for', b'100.64.0.42:1234')],
    [(b'x-forwarded-for', b'[fd7a:115c:a1e0::42]')], [(b'x-forwarded-for', b'fe80::1%lo0')],
    [(b'x-forwarded-for', b'\xff')], [(b'x-forwarded-for', b'a' * 46)],
    [(b'x-forwarded-for', SOURCE.encode()), (b'tailscale-funnel-request', b'1')],
])
def test_untrusted_source_shapes_and_funnel_are_denied_before_cli(monkeypatch, headers):
    verify, calls = verifier(monkeypatch)
    with pytest.raises(AccessDenied):
        asyncio.run(verify(scope(headers=headers)))
    assert calls == []


def test_identity_headers_cannot_override_numeric_whois_identity(monkeypatch):
    verify, _ = verifier(monkeypatch)
    headers = [(b'x-forwarded-for', SOURCE.encode()), (b'tailscale-user-login', b'other@example.test'),
               (b'tailscale-user-name', b'Other Human'), (b'authorization', b'Bearer fake')]
    assert asyncio.run(verify(scope(headers=headers))).subject == '123456789'


@pytest.mark.parametrize('mutation', [
    {'UserProfile': None}, {'Node': None}, {'UserProfile': {'ID': True}},
    {'UserProfile': {'ID': '123456789'}}, {'UserProfile': {'ID': 123456789.0}},
    {'UserProfile': {'ID': 0}}, {'UserProfile': {'ID': -1}}, {'UserProfile': {'ID': 2**63}},
    {'Node': {**WHOIS['Node'], 'User': 99}}, {'Node': {**WHOIS['Node'], 'User': True}},
    {'Node': {**WHOIS['Node'], 'Tags': ['tag:service']}}, {'Node': {**WHOIS['Node'], 'Tags': None}},
    {'Node': {**WHOIS['Node'], 'Expired': True}}, {'Node': {**WHOIS['Node'], 'Expired': 0}},
    {'Node': {**WHOIS['Node'], 'Addresses': ['100.64.0.43/32']}},
    {'Node': {**WHOIS['Node'], 'Addresses': ['100.64.0.42/24']}},
    {'Node': {**WHOIS['Node'], 'Addresses': ['bad']}},
    {'Node': {**WHOIS['Node'], 'Addresses': []}},
])
def test_nonhuman_or_wrong_whois_attribution_is_policy_denied(monkeypatch, mutation):
    verify, calls = verifier(monkeypatch, [PREFS, {**WHOIS, **mutation}])
    with pytest.raises(AccessDenied):
        asyncio.run(verify(scope()))
    assert len(calls) == 2


def test_ipv6_is_passed_as_single_unscoped_ip(monkeypatch):
    verify, calls = verifier(monkeypatch)
    assert asyncio.run(verify(scope(headers=[(b'x-forwarded-for', b'fd7a:115c:a1e0::42')]))).subject == '123456789'
    assert calls[1][-1] == 'fd7a:115c:a1e0::42'


@pytest.mark.parametrize('url', [None, '', 'http://controlplane.tailscale.com',
    'https://controlplane.tailscale.com/', 'https://other.example.test',
    'https://user:password@controlplane.tailscale.com'])
def test_unsupported_or_ambiguous_control_realm_never_reuses_namespace(monkeypatch, url):
    verify, calls = verifier(monkeypatch, [{'ControlURL': url}])
    with pytest.raises(AccessDenied, match='owner_identity_control_refused'):
        asyncio.run(verify(scope()))
    assert calls == [('debug', 'prefs')]


def test_realm_change_during_whois_refuses_admission(monkeypatch):
    verify, _ = verifier(monkeypatch, [PREFS, WHOIS, {'ControlURL': 'https://other.example.test'}])
    with pytest.raises(AccessDenied, match='owner_identity_control_refused'):
        asyncio.run(verify(scope()))


@pytest.mark.parametrize('raw', [b'not json private-detail', b'[]', b'null', b'\xff',
    b'{"ControlURL":"https://controlplane.tailscale.com","ControlURL":"https://other.example.test"}',
    b'{"value":NaN}'])
def test_malformed_cli_output_is_unavailable_without_disclosure(raw):
    with pytest.raises(AccessUnavailable) as error:
        owner_ingress._json_object(raw)
    assert str(error.value) == 'owner_identity_response_unavailable'


def test_absolute_configured_executable_required(tmp_path):
    for binary in [Path('tailscale'), tmp_path / 'missing', tmp_path]:
        with pytest.raises(ValueError, match='absolute configured native'):
            ServePrincipalVerifier(tailscale_binary=binary)


def test_five_request_burst_succeeds_with_four_active_verifications(monkeypatch):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    active = peak = entered = 0
    async def run():
        release, full = asyncio.Event(), asyncio.Event()
        async def admit(_source):
            nonlocal active, peak, entered
            active += 1
            entered += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
            try:
                await release.wait()
                return PrincipalRef('test', 'human')
            finally:
                active -= 1
        monkeypatch.setattr(verify, '_admit', admit)
        tasks = [asyncio.create_task(verify(scope())) for _ in range(5)]
        await asyncio.wait_for(full.wait(), 1)
        assert verify._pending == 5 and entered == 4
        release.set()
        assert await asyncio.gather(*tasks) == [PrincipalRef('test', 'human')] * 5
        assert peak == 4 and entered == 5
        assert verify._pending == 0 and verify._slots._value == 4
    asyncio.run(run())


def test_active_plus_queued_capacity_is_32_and_33rd_refuses_without_work(monkeypatch):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    async def run():
        entered = 0
        full = asyncio.Event()
        async def admit(_source):
            nonlocal entered
            entered += 1
            if entered == 4:
                full.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(verify, '_admit', admit)
        tasks = [asyncio.create_task(verify(scope())) for _ in range(32)]
        try:
            await asyncio.wait_for(full.wait(), 1)
            assert verify._pending == 32 and entered == 4
            with pytest.raises(AccessUnavailable, match='owner_identity_busy'):
                await verify(scope())
            assert verify._pending == 32 and entered == 4
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert verify._pending == 0 and verify._slots._value == 4
    asyncio.run(run())


@pytest.mark.parametrize('end', ['timeout', 'cancel'])
def test_queued_timeout_or_cancellation_releases_reservation_without_admitting(monkeypatch, end):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    async def run():
        entered = 0
        full = asyncio.Event()
        async def admit(_source):
            nonlocal entered
            entered += 1
            if entered == 4:
                full.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(verify, '_admit', admit)
        active = [asyncio.create_task(verify(scope())) for _ in range(4)]
        try:
            await asyncio.wait_for(full.wait(), 1)
            monkeypatch.setattr(owner_ingress, '_TIMEOUT_SECONDS', .03)
            queued = asyncio.create_task(verify(scope()))
            await asyncio.sleep(0)
            assert verify._pending == 5
            if end == 'cancel':
                queued.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await queued
            else:
                with pytest.raises(AccessUnavailable, match='owner_identity_lookup_unavailable'):
                    await queued
            assert entered == 4 and verify._pending == 4
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
        assert verify._pending == 0 and verify._slots._value == 4
    asyncio.run(run())


def test_queue_wait_and_verification_share_one_deadline(monkeypatch):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    async def run():
        entered = 0
        release, full, fifth = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def admit(_source):
            nonlocal entered
            entered += 1
            if entered <= 4:
                if entered == 4:
                    full.set()
                await release.wait()
                return PrincipalRef('test', 'human')
            fifth.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(verify, '_admit', admit)
        active = [asyncio.create_task(verify(scope())) for _ in range(4)]
        await asyncio.wait_for(full.wait(), 1)
        monkeypatch.setattr(owner_ingress, '_TIMEOUT_SECONDS', .2)
        queued = asyncio.create_task(verify(scope()))
        await asyncio.sleep(.1)
        release.set()
        await asyncio.gather(*active)
        await asyncio.wait_for(fifth.wait(), 1)
        with pytest.raises(AccessUnavailable):
            await asyncio.wait_for(asyncio.shield(queued), .15)
        assert verify._pending == 0 and verify._slots._value == 4
    asyncio.run(run())


def test_admission_error_releases_reservation_and_active_slot(monkeypatch):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    async def refuse(_source):
        raise AccessDenied('fixture')
    monkeypatch.setattr(verify, '_admit', refuse)
    with pytest.raises(AccessDenied):
        asyncio.run(verify(scope()))
    assert verify._pending == 0 and verify._slots._value == 4


def subprocess_helper(monkeypatch, tmp_path, program):
    # Launch only our synthetic Python helper, never the configured native CLI.
    # The production argv/environment are captured before the test substitutes
    # an executable and an explicitly constructed scratch child environment.
    original = asyncio.create_subprocess_exec
    env = constructed_env(HOME=tmp_path / 'home', TMPDIR=tmp_path / 'tmp')
    for key in ['HOME', 'TMPDIR']:
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    calls, processes = [], []
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs.copy()))
        kwargs['env'] = env
        proc = await original(sys.executable, '-c', program, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    return calls, processes


def test_cli_argv_environment_and_outputs_are_bounded_without_shell(monkeypatch, tmp_path):
    monkeypatch.setenv('TERM', 'ambient-terminal-must-not-be-inherited')
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    calls, processes = subprocess_helper(monkeypatch, tmp_path,
        'import sys; sys.stdout.write(\'{"ControlURL":"https://controlplane.tailscale.com"}\'); sys.stderr.write("private diagnostic")')
    assert asyncio.run(verify._command('debug', 'prefs')) == PREFS
    args, kwargs = calls[0]
    assert args == (sys.executable, 'debug', 'prefs')
    assert kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'TERM': 'dumb'}
    assert kwargs['start_new_session'] is True and kwargs['stdin'] == asyncio.subprocess.DEVNULL
    assert 'shell' not in kwargs
    assert processes[0].returncode == 0


@pytest.mark.parametrize('program', [
    'import sys; sys.stderr.write("private secret"); sys.exit(1)',
    'import sys; sys.stdout.write("x" * 65537)',
    'import sys; [sys.stdout.write("x" * 4096) for _ in range(10000)]',
    'import sys; sys.stderr.write("x" * 8193)',
    'import sys; sys.stdout.write("private malformed data")',
])
def test_failed_or_overlarge_cli_is_unavailable_and_reaped(monkeypatch, tmp_path, program):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    _, processes = subprocess_helper(monkeypatch, tmp_path, program)
    with pytest.raises(AccessUnavailable) as error:
        asyncio.run(verify._command('debug', 'prefs'))
    assert 'private' not in str(error.value) and 'secret' not in str(error.value)
    assert processes[0].returncode is not None


def test_stalled_cli_timeout_kills_and_reaps_subprocess(monkeypatch, tmp_path):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    _, processes = subprocess_helper(monkeypatch, tmp_path, 'import time; time.sleep(60)')
    monkeypatch.setattr(owner_ingress, '_TIMEOUT_SECONDS', .1)
    with pytest.raises(AccessUnavailable, match='owner_identity_lookup_unavailable'):
        asyncio.run(verify(scope()))
    assert processes[0].returncode is not None
    assert verify._slots._value == owner_ingress._CONCURRENCY
    assert verify._pending == 0


def test_request_cancellation_kills_and_reaps_subprocess(monkeypatch, tmp_path):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    _, processes = subprocess_helper(monkeypatch, tmp_path, 'import time; time.sleep(60)')
    async def run():
        task = asyncio.create_task(verify(scope()))
        while not processes:
            await asyncio.sleep(.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert processes[0].returncode is not None
        assert verify._slots._value == owner_ingress._CONCURRENCY
        assert verify._pending == 0
    asyncio.run(run())


def test_timeout_kills_cli_descendant_after_parent_exits(monkeypatch, tmp_path):
    verify = ServePrincipalVerifier(tailscale_binary=Path(sys.executable))
    child_file = tmp_path / 'synthetic-child.pid'
    program = f"""import os,time
pid = os.fork()
if pid:
    os._exit(0)
with open({str(child_file)!r}, 'w') as stream:
    stream.write(str(os.getpid()))
time.sleep(60)
"""
    _, processes = subprocess_helper(monkeypatch, tmp_path, program)
    killed = []
    original_killpg = os.killpg
    def killpg(pid, sig):
        killed.append((pid, sig))
        original_killpg(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    monkeypatch.setattr(owner_ingress, '_TIMEOUT_SECONDS', .5)
    try:
        with pytest.raises(AccessUnavailable):
            asyncio.run(verify(scope()))
        assert child_file.exists(), 'synthetic descendant did not start'
        assert processes[0].returncode == 0  # Parent already exited normally.
        assert killed == [(processes[0].pid, signal.SIGKILL)]
    finally:
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
