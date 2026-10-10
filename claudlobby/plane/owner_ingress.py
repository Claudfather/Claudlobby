"""Opt-in Tailscale Serve identity adapter for a private Unix HTTP socket.

This is NOT a verifier for TCP, arbitrary forwarded headers, or the normal
Plane view. Its caller must contain the backend socket in a private, owned
0700 directory and run uvicorn with proxy_headers=False. Serve must overwrite
X-Forwarded-For. A None ASGI client is a necessary UDS check, not peer
authentication; same-UID/root adversaries remain outside this boundary.

The configured native CLI talks to its local daemon; no HTTP API, login name,
email, browser credential, or long-lived identity/realm cache is used here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import signal
import stat

from .owner_access import AccessDenied, AccessUnavailable, PrincipalRef

_CONTROL_URL = "https://controlplane.tailscale.com"
_NAMESPACE = "tailscale:controlplane.tailscale.com"
_MAX_OUTPUT = 64 * 1024
_MAX_STDERR = 8 * 1024
_TIMEOUT_SECONDS = 5
_CONCURRENCY = 4
# A read-only native macOS probe showed the bundled app selects GUI error
# output without TERM; fixed TERM=dumb returned JSON and official ControlURL.
# Select CLI mode explicitly without inheriting terminal or credential state.
_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TERM": "dumb"}


def _source_ip(scope) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    if scope.get("type") != "http" or "client" not in scope or scope["client"] is not None:
        raise AccessDenied("owner_ingress_not_unix")
    headers = scope.get("headers", [])
    if any(key.lower() == b"tailscale-funnel-request" for key, _ in headers):
        raise AccessDenied("owner_ingress_funnel_refused")
    values = [value for key, value in headers if key.lower() == b"x-forwarded-for"]
    if len(values) != 1 or not 1 <= len(values[0]) <= 45:
        raise AccessDenied("owner_ingress_source_refused")
    try:
        value = values[0].decode("ascii")
        # No chains, ports, bracketed addresses, scopes, or whitespace repair.
        if value != value.strip() or any(char in value for char in ",%[]"):
            raise ValueError
        return ipaddress.ip_address(value)
    except (UnicodeError, ValueError):
        raise AccessDenied("owner_ingress_source_refused") from None


def _json_object(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(data, dict):
            raise ValueError
        return data
    except (UnicodeError, ValueError, RecursionError):
        raise AccessUnavailable("owner_identity_response_unavailable") from None


def _human(whois: dict, source) -> PrincipalRef:
    node, user = whois.get("Node"), whois.get("UserProfile")
    if not isinstance(node, dict) or not isinstance(user, dict):
        raise AccessDenied("owner_identity_not_human")
    uid = user.get("ID")
    if type(uid) is not int or not 0 < uid < 2**63 or type(node.get("User")) is not int or node["User"] != uid:
        raise AccessDenied("owner_identity_not_human")
    # Captured human nodes omit Tags and Expired. Only an explicit empty tag
    # list / false expiry is equivalent; malformed values never become human.
    if node.get("Tags", []) != [] or node.get("Expired", False) is not False:
        raise AccessDenied("owner_identity_not_human")
    addresses = node.get("Addresses")
    if not isinstance(addresses, list) or not 1 <= len(addresses) <= 32:
        raise AccessDenied("owner_identity_source_mismatch")
    try:
        peers = []
        for address in addresses:
            if not isinstance(address, str) or '%' in address:
                raise ValueError
            peer = ipaddress.ip_interface(address)
            if peer.network.prefixlen != peer.max_prefixlen:
                raise ValueError
            peers.append(peer.ip)
        if source not in peers:
            raise ValueError
    except ValueError:
        raise AccessDenied("owner_identity_source_mismatch") from None
    return PrincipalRef(_NAMESPACE, str(uid))


async def _bounded_read(stream, limit: int) -> bytes:
    output = bytearray()
    while True:
        chunk = await stream.read(min(4096, limit + 1 - len(output)))
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > limit:
            raise AccessUnavailable("owner_identity_output_unavailable")


async def _stop(proc, *, kill: bool) -> None:
    # A new process group belongs solely to this invocation. Kill it on timeout,
    # overflow or cancellation, including a child holding stdout/stderr open.
    try:
        if kill:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS can report EPERM for an already-disappeared group.
                # A still-running owned parent must be killed independently.
                if proc.returncode is None:
                    proc.kill()
        if kill:
            # The parent may have exited before a descendant. Discard killed
            # pipes to EOF with bounded memory and a separate cleanup deadline.
            async def drain(stream):
                while await stream.read(4096):
                    pass
            await asyncio.wait_for(asyncio.gather(proc.wait(), drain(proc.stdout), drain(proc.stderr)), 1)
        else:
            await proc.wait()
    except (OSError, asyncio.TimeoutError):
        raise AccessUnavailable("owner_identity_cleanup_unavailable") from None
    finally:
        # asyncio Process has no public close API. Close only this invocation's
        # owned transport while its event loop is alive, including when EOF
        # cannot arrive; no listener or unrelated process is touched.
        proc._transport.close()


class ServePrincipalVerifier:
    """Bounded async verifier, exclusively for a contained Serve UDS backend.

    Absolute native executable configuration is operator authority. This class
    does not discover binaries, configure Serve, allowlist owners, initialize
    grants, or prove daemon/socket reachability. Pairing policy remains separate.
    Each admission checks the daemon's exact official realm before AND after
    WhoIs; unsupported realms never reuse the official principal namespace.
    """

    def __init__(self, *, tailscale_binary: Path):
        try:
            binary = Path(tailscale_binary)
            if not binary.is_absolute() or not stat.S_ISREG(binary.stat().st_mode) or not os.access(binary, os.X_OK):
                raise ValueError
        except (TypeError, ValueError, OSError):
            raise ValueError("an absolute configured native Tailscale executable is required") from None
        self.binary = str(binary)
        self._slots = asyncio.BoundedSemaphore(_CONCURRENCY)

    async def _command(self, *args: str) -> dict:
        proc = None
        tasks = []
        io_complete = False
        try:
            proc = await asyncio.create_subprocess_exec(self.binary, *args,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=_ENV.copy(), start_new_session=True)
            tasks = [asyncio.create_task(_bounded_read(proc.stdout, _MAX_OUTPUT)),
                     asyncio.create_task(_bounded_read(proc.stderr, _MAX_STDERR)),
                     asyncio.create_task(proc.wait())]
            output, _, returncode = await asyncio.gather(*tasks)
            io_complete = True
            if returncode != 0:
                raise AccessUnavailable("owner_identity_lookup_unavailable")
            return _json_object(output)
        except OSError:
            raise AccessUnavailable("owner_identity_lookup_unavailable") from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if proc is not None:
                # Shield cleanup so cancellation cannot leave an owned process
                # running or hold a concurrency slot after this admission exits.
                cleanup = asyncio.create_task(_stop(proc, kill=not io_complete))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise

    async def _realm(self) -> None:
        prefs = await self._command("debug", "prefs")
        if prefs.get("ControlURL") != _CONTROL_URL:
            raise AccessDenied("owner_identity_control_refused")

    async def _admit(self, source) -> PrincipalRef:
        await self._realm()
        identity = _human(await self._command("whois", "--json", "--proto=tcp", str(source)), source)
        await self._realm()
        return identity

    async def __call__(self, scope) -> PrincipalRef:
        source = _source_ip(scope)
        # No unbounded admission queue, even when a daemon stalls. Acquisition
        # does not yield when a slot is free, so this check is atomic per loop.
        if self._slots.locked():
            raise AccessUnavailable("owner_identity_busy")
        async with self._slots:
            try:
                return await asyncio.wait_for(self._admit(source), timeout=_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise AccessUnavailable("owner_identity_lookup_unavailable") from None
