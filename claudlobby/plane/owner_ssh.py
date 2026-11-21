"""Ordinary-sshd source adapter within the existing trusted-account boundary.

The fixed non-PTY browser exec sends no SSH env requests. Supported sshd supplies
SSH_CONNECTION after its environment construction; trusted account startup must
preserve it through this capture. Same-UID/root rewriting remains out of scope,
as with the existing private UDS adapter. Frames and HTTP headers are not inputs.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re

from .owner_access import AccessDenied
from .owner_ingress import NativePrincipalResolver

# The explicit persisted transport discriminator for a computer reached this way.
TRANSPORT = "mac_openssh"
# The standalone/App Store client's own CLI; an absolute operator-authority path.
MAC_TAILSCALE = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")

_V4 = ipaddress.ip_network('100.64.0.0/10')
_V6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')


def _tailnet(value):
    return value in (_V4 if value.version == 4 else _V6)


@dataclass(frozen=True)
class SSHSessionSource:
    client_ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    client_port: int
    server_ip: ipaddress.IPv4Address | ipaddress.IPv6Address
    server_port: int

    @classmethod
    def capture(cls):
        """Capture once before replacement environment; no SSH_CLIENT fallback."""
        raw = os.environ.get('SSH_CONNECTION')
        try:
            if not isinstance(raw, str) or not 7 <= len(raw) <= 160:
                raise ValueError
            fields = raw.split(' ')
            if len(fields) != 4:
                raise ValueError
            client, server = ipaddress.ip_address(fields[0]), ipaddress.ip_address(fields[2])
            if (str(client) != fields[0] or str(server) != fields[2]
                    or not _tailnet(client) or not _tailnet(server)
                    or any(re.fullmatch(r'[1-9][0-9]{0,4}', fields[i]) is None
                           or not 0 < int(fields[i]) < 65536 for i in (1, 3))):
                raise ValueError
            return cls(client, int(fields[1]), server, int(fields[3]))
        except (ValueError, TypeError):
            raise AccessDenied('owner_ssh_source_refused') from None


class SSHSessionVerifier:
    """A captured server source, fresh native policy, no fabricated UDS scope."""
    def __init__(self, source: SSHSessionSource, resolver: NativePrincipalResolver):
        if type(source) is not SSHSessionSource or not isinstance(resolver, NativePrincipalResolver):
            raise ValueError('explicit captured SSH source and native resolver required')
        self.source, self.resolver = source, resolver
        self._server = None

    async def origin(self):
        value = await self.resolver.server_status()
        own = value.get('Self')
        if not isinstance(own, dict):
            raise AccessDenied('owner_ssh_server_refused')
        dns = own.get('DNSName')
        addresses = own.get('TailscaleIPs')
        if (value.get('BackendState') != 'Running' or own.get('Online') is not True
                or own.get('Expired', False) is not False or not isinstance(addresses, list)
                or str(self.source.server_ip) not in addresses
                or not isinstance(dns, str) or re.fullmatch(
                    r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\.ts\.net\.', dns) is None):
            raise AccessDenied('owner_ssh_server_refused')
        server = (own.get('ID'), own.get('PublicKey'), own.get('UserID'), dns, tuple(addresses))
        if self._server is not None and self._server != server:
            raise AccessDenied('owner_ssh_server_changed')
        self._server = server
        return 'https://' + dns[:-1]  # logical key, never evidence of HTTPS reachability

    async def __call__(self, _scope):
        await self.origin()
        return await self.resolver.resolve(self.source.client_ip)
