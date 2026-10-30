"""Per-serving-process finite read ownership; streams and mutations stay separate.

Finite /api/ data GET/HEAD responses share this owner; streams are separate.
Root/static assets retain their existing authorization and response gates.
A response deadline abandons delivery, not accepted synchronous work. The slot
remains held until the shielded ASGI operation (including its threadpool work)
settles. SQL progress deadlines remain an independent cooperative inner bound.
"""
from __future__ import annotations

import asyncio

from starlette.responses import JSONResponse

READ_SECONDS = 15
READ_CAPACITY = 8
_SCOPE_OWNER = "claudlobby.finite_read_owner"


def finite_read(scope):
    return (scope["type"] == "http" and scope["method"] in {"GET", "HEAD"}
            and scope.get("path", "").startswith("/api/")
            and scope.get("path") != "/api/stream")


class FiniteReads:
    def __init__(self):
        self.workers = set()
        self.stopping = False

    async def shutdown(self):
        self.stopping = True
        if self.workers:
            await asyncio.gather(*tuple(self.workers), return_exceptions=True)

    async def run(self, app, scope, receive, send):
        # The marker is server-owned ASGI scope state, never a request header.
        if not finite_read(scope) or scope.get(_SCOPE_OWNER) is self:
            await app(scope, receive, send)
            return
        async def unavailable():
            await JSONResponse({"state": "unavailable"}, 503,
                headers={"Cache-Control": "no-store"})(scope, receive, send)
        if self.stopping or len(self.workers) >= READ_CAPACITY:
            await unavailable()
            return
        abandoned = started = False
        disconnected = asyncio.Event()
        incoming = asyncio.Queue(maxsize=2)

        async def relay():
            ended = False
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                    await incoming.put(message)
                    return
                # A second request after body completion is not a valid ASGI
                # body sequence; do not accept it as another request.
                if ended:
                    return
                ended = not message.get("more_body", False)
                await incoming.put(message)

        async def guarded_send(message):
            nonlocal started
            if abandoned or disconnected.is_set():
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        child_scope = {**scope, _SCOPE_OWNER: self}
        # Reserve/task creation has no await. Only actual settlement releases it.
        job = asyncio.create_task(app(child_scope, incoming.get, guarded_send))
        self.workers.add(job)
        def finished(task):
            self.workers.discard(task)
            if not task.cancelled():
                task.exception()
        job.add_done_callback(finished)
        relay_task = asyncio.create_task(relay())
        gone = asyncio.create_task(disconnected.wait())
        try:
            done, _ = await asyncio.wait({job, gone}, timeout=READ_SECONDS,
                                         return_when=asyncio.FIRST_COMPLETED)
            if job in done:
                job.result()
            else:
                abandoned = True
                if not disconnected.is_set() and not started:
                    await unavailable()
                # Started responses stay incomplete; never substitute a body.
        finally:
            abandoned = True
            # Never cancel job: cancelling an ASGI waiter can detach its native
            # worker. Accepted work owns its reservation through settlement.
            relay_task.cancel()
            if not incoming.full():
                incoming.put_nowait({"type": "http.disconnect"})
            gone.cancel()
            await asyncio.gather(relay_task, gone, return_exceptions=True)


class FiniteReadMiddleware:
    """Unprotected localhost reads use the same owner as protected adapters."""
    def __init__(self, app, *, owner):
        self.app, self.owner = app, owner

    async def __call__(self, scope, receive, send):
        if finite_read(scope):
            await self.owner.run(self.app, scope, receive, send)
        else:
            await self.app(scope, receive, send)
