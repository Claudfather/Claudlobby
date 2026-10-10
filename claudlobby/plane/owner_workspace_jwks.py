"""One trusted issuer's bounded public keyset; no discovery or token authority."""
from __future__ import annotations

import asyncio
import time

from .workspace_assertion import AssertionUnavailable, validate_workspace_jwks


class WorkspaceKeyset:
    """Memory-only whole-set cache. Unknown key refresh is a hint, never proof."""
    def __init__(self, issuer: str):
        import httpx
        self._httpx = httpx
        self.url = issuer + "/api/auth/jwks"
        self._lock = asyncio.Lock()
        self._raw = None
        self._kids = frozenset()
        self._fetched = None
        self._attempt = None

    def _fresh(self):
        if self._fetched is None:
            return False
        mono, wall = self._fetched
        return 0 <= time.monotonic() - mono < 300 and 0 <= time.time() - wall < 300

    async def _fetch(self):
        httpx = self._httpx
        async with httpx.AsyncClient(verify=True, trust_env=False, follow_redirects=False,
                timeout=httpx.Timeout(3), limits=httpx.Limits(max_connections=1,
                max_keepalive_connections=0), headers={"Accept": "application/json",
                "Accept-Encoding": "identity"}) as client:
            async with client.stream("GET", self.url) as response:
                if (response.status_code != 200
                        or response.headers.get("content-encoding", "identity") != "identity"
                        or response.headers.get("content-type", "").split(";", 1)[0].lower()
                        != "application/json"):
                    raise AssertionUnavailable()
                raw = bytearray()
                async for chunk in response.aiter_raw():
                    if len(raw) + len(chunk) > 65536:
                        raise AssertionUnavailable()
                    raw.extend(chunk)
                value = bytes(raw)
                kids = validate_workspace_jwks(value)
                self._raw, self._kids = value, kids
                self._fetched = time.monotonic(), time.time()

    async def _get(self, assertion):
        import jwt
        # Header is only a bounded refresh hint. The pure verifier checks every
        # header/claim/signature after retrieval, including unsupported fields.
        kid = None
        if isinstance(assertion, str) and assertion.isascii() and 1 <= len(assertion) <= 8192:
            try:
                header = jwt.get_unverified_header(assertion)
                if header.get("alg") == "EdDSA" and isinstance(header.get("kid"), str):
                    kid = header["kid"]
            except jwt.PyJWTError:
                pass
        async with self._lock:
            fresh = self._fresh()
            if fresh and kid in self._kids:
                return self._raw
            now = time.monotonic()
            if self._attempt is not None and 0 <= now - self._attempt < 30:
                if fresh:
                    return self._raw
                raise AssertionUnavailable()
            self._attempt = now
            await self._fetch()
            return self._raw

    async def get(self, assertion: str) -> bytes:
        try:
            # Includes lock wait, TLS, slow-drip body and complete key parsing.
            return await asyncio.wait_for(self._get(assertion), timeout=8)
        except (TimeoutError, self._httpx.HTTPError, ValueError, ImportError):
            raise AssertionUnavailable() from None
