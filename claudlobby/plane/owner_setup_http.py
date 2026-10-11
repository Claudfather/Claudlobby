"""Finite first-team HTTP door; identity AND local setup approval, never read authority."""
import asyncio
import re
from starlette.responses import JSONResponse
from ..command_result import CommandFailure
from .owner_access import AccessDenied, AccessUnavailable, VerifiedReader
from .owner_connected_actions_http import ConnectedActionsHTTP
from .owner_connected_http import _token
from .owner_workspace_http import _header, _json, _DEADLINE, InvalidRequest
from .owner_setup import SetupEngine, _uuid
from .owner_source import source_host_uid

PREFIX="/api/owner/connected/setup/"
OPERATIONS=frozenset({"context","preview","launch","receipt","repository"})

class SetupHTTP(ConnectedActionsHTTP):
    def __init__(self, access, *, config, authority, verify_principal):
        # Reuse maintained admission deadlines/owned workers, not action engines.
        from .owner_workspace_http import WorkspaceBootstrapHTTP
        WorkspaceBootstrapHTTP.__init__(self,access,config=config,authority=authority,verify_principal=verify_principal)
        self.engine=SetupEngine(access)

    def response_seconds(self,scope):
        # Three clones allow 120s each plus credential-tier reads and closing admission.
        if scope.get("setup_operation")=="repository": return 600
        return 45 if scope.get("setup_operation")=="launch" else 15

    async def _perform_action(self,scope,receive,operation,progress):
        if scope["method"]=="OPTIONS":
            requested=[field.strip().lower() for field in (_header(scope,b"access-control-request-headers") or "").split(",")]
            if (_header(scope,b"authorization") is not None or _header(scope,b"access-control-request-method")!="POST"
                    or len(requested)!=3 or set(requested)!={"authorization","content-type","x-claudlobby-owner"}): raise AccessDenied("setup_denied")
            principal=await self._principal(scope,_DEADLINE.get())
            await self._blocking(source_host_uid,self.access.root)
            binding=await self._blocking(self.workspace.active_binding,principal)
            if binding.issuer!=self.config.issuer or binding.operational_origin!=self.config.operational_origin: raise AccessDenied("setup_denied")
            return None,204
        token=_token(scope)
        if _header(scope,b"x-claudlobby-owner")!="1" or (_header(scope,b"content-type") or "").split(";",1)[0].lower()!="application/json": raise AccessDenied("setup_denied")
        principal=await self._principal(scope,_DEADLINE.get())
        await self._blocking(source_host_uid,self.access.root)
        reader=VerifiedReader(principal,token)
        grant=await self._blocking(self.engine.store.authorize,reader)
        binding=await self._blocking(self.workspace.active_binding,principal)
        if binding.issuer!=self.config.issuer or binding.operational_origin!=self.config.operational_origin: raise AccessDenied("setup_denied")
        payload=await asyncio.wait_for(_json(receive,32768),5)
        expected={"context":set(),"preview":{"generation","goal"},"launch":{"generation","plan_id","request_id"},"receipt":{"generation","plan_id","request_id"},"repository":{"generation","plan_id","request_id"}}[operation]
        if set(payload)!=expected: raise InvalidRequest
        if operation!="context" and (not _uuid(payload["generation"]) or payload["generation"]!=grant.generation): raise InvalidRequest
        if operation=="preview" and (not isinstance(payload["goal"],str) or len(payload["goal"])>2000): raise InvalidRequest
        if operation in {"launch","receipt","repository"}:
            if (not isinstance(payload["plan_id"],str) or not re.fullmatch(r"p-[0-9a-f]{64}",payload["plan_id"]) or not _uuid(payload["request_id"])): raise InvalidRequest
        fn=getattr(self.engine,operation)
        progress[0]=True  # No refusal after engine entry implies no earlier effect.
        try:
            result=await self._blocking(fn,reader,**payload)
        except (AccessDenied, AccessUnavailable):
            raise
        except (CommandFailure, OSError, ValueError, RuntimeError):
            # Canonical refusal may retain a plan or activation. Inspect original
            # journal; no generic exception claims rollback or safe resend.
            raise AccessUnavailable() from None
        current=await self._principal(scope,_DEADLINE.get())
        await self._blocking(source_host_uid,self.access.root)
        if current!=principal: raise AccessDenied("setup_denied")
        await self._blocking(self.engine.store.authorize,VerifiedReader(current,token),expected=grant)
        return result,200

    async def __call__(self,scope,receive,send):
        # The inherited owned-worker carrier has fixed action route constants.
        # Re-map only our finite operation into that carrier, without any engine
        # substitution from request data. Context/prepare/send/receipt are local.
        operation=scope["path"][len(PREFIX):]
        if operation not in OPERATIONS:
            await JSONResponse({"state":"denied"},404,headers={"Cache-Control":"private, no-store"})(scope,receive,send);return
        mapped={"context":"context","preview":"prepare","launch":"send","receipt":"receipt","repository":"send"}[operation]
        # Concurrent requests must never modify an instance callback. Dispatch
        # translation belongs in the protected method using immutable path.
        translated={**scope,"path":"/api/owner/connected/actions/"+mapped,"setup_operation":operation}
        await super().__call__(translated,receive,send)

    async def _serve(self,scope,receive,send,operation,progress,headers):
        return await super()._serve(scope,receive,send,scope["setup_operation"],progress,headers)
