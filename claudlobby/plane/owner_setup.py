"""One locally approved first-team setup; canonical plans/journal own all work.

The connected credential is identity evidence, not setup authority. Optional
setup storage is initialized only by explicit local approval. No HTTP read or
constructor repairs it. Same-UID file mutation remains outside this boundary.
"""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from .owner_access import AccessDenied, AccessUnavailable
from .owner_connected import OwnerConnected
from .owner_source import source_host_uid, fresh_owner_setup

PLAN_SECONDS = 600
GRANT_SECONDS = 86400
_SCHEMA = (
    "CREATE TABLE setup_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)",
    "INSERT INTO setup_metadata VALUES(1,1)",
    "CREATE TABLE setup_grant(singleton INTEGER PRIMARY KEY CHECK(singleton=1),snapshot TEXT NOT NULL,active INTEGER NOT NULL,launch_id TEXT,launch_plan TEXT)",
    "CREATE TABLE setup_previews(plan_id TEXT PRIMARY KEY,generation TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,payload TEXT NOT NULL)",
)

@dataclass(frozen=True)
class SetupGrant:
    binding_snapshot: str
    template: str
    fleet: str
    repository: str
    release_id: str
    release_seal: str
    install_directory: str
    generation: str
    created_at: float
    expires_at: float


def _snapshot(grant):
    return json.dumps(asdict(grant),sort_keys=True,separators=(",", ":"))


def _uuid(value):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",value) is not None


class OwnerSetup:
    def __init__(self, access):
        self.access=access
        self.connected=access.connected()

    @staticmethod
    def _schema(conn):
        rows=conn.execute("SELECT singleton,version FROM setup_metadata").fetchall()
        if len(rows)!=1 or tuple(rows[0])!=(1,1): raise AccessUnavailable()
        conn.execute("SELECT singleton,snapshot,active,launch_id,launch_plan FROM setup_grant LIMIT 0")
        conn.execute("SELECT plan_id,generation,created_at,expires_at,payload FROM setup_previews LIMIT 0")

    def candidate(self, principal, *, template, fleet, repository, release_id, release_seal, install_directory):
        # Reuse the installed template's finite repository/fleet policy, with a
        # harmless validation goal; no authoring, registration or staging here.
        from ..commands.setup import _template_config
        if not isinstance(fleet,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*",fleet): raise AccessDenied("setup_denied")
        _template_config(template,fleet,repository,"Validate selected repository scope")
        directory=Path(install_directory)
        if (not directory.is_absolute() or directory.resolve()!=directory or not directory.is_dir()
                or not re.fullmatch(r"r-[0-9a-f]{64}",release_id) or not re.fullmatch(r"[0-9a-f]{64}",release_seal)):
            raise AccessDenied("setup_denied")
        with self.access._connection() as conn:
            _,binding=self.connected._context(conn,principal)
            now=self.access._now()
            return SetupGrant(OwnerConnected._snapshot(binding),template,fleet,repository,release_id,release_seal,
                str(directory),str(uuid4()),now,now+GRANT_SECONDS)

    def allow(self, expected):
        if type(expected) is not SetupGrant: raise AccessDenied("setup_denied")
        with self.access._connection(write=True) as conn:
            binding=self.access.workspace()._active_binding(conn,self.access._grant(conn).principal)
            if OwnerConnected._snapshot(binding)!=expected.binding_snapshot: raise AccessDenied("setup_denied")
            if not expected.created_at<=self.access._now()<expected.expires_at: raise AccessDenied("setup_denied")
            names={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not names & {"setup_metadata","setup_grant","setup_previews"}:
                for statement in _SCHEMA: conn.execute(statement)
            self._schema(conn)
            if conn.execute("SELECT 1 FROM setup_grant WHERE active=1").fetchone(): raise AccessDenied("setup_already_allowed")
            conn.execute("INSERT OR REPLACE INTO setup_grant VALUES(1,?,1,NULL,NULL)",(_snapshot(expected),))

    def local(self):
        with self.access._connection() as conn:
            self._schema(conn)
            row=conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
            return self._grant(row) if row and row["active"]==1 else None

    @staticmethod
    def _grant(row):
        grant=SetupGrant(**json.loads(row["snapshot"]))
        if (not _uuid(grant.generation) or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in (grant.created_at,grant.expires_at))
                or grant.expires_at-grant.created_at!=GRANT_SECONDS): raise AccessUnavailable()
        return grant

    def revoke(self, expected):
        with self.access._connection(write=True) as conn:
            self._schema(conn)
            binding=self.access.workspace()._active_binding(conn,self.access._grant(conn).principal)
            if OwnerConnected._snapshot(binding)!=expected.binding_snapshot: raise AccessDenied("setup_denied")
            row=conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
            if not row or row["active"]!=1 or self._grant(row)!=expected: raise AccessDenied("setup_denied")
            conn.execute("UPDATE setup_grant SET active=0 WHERE singleton=1")

    def _admit(self, conn, reader, *, expected=None, effects=False):
        host=source_host_uid(self.access.root)
        admission=self.connected._admit(conn,reader.token,reader.principal,host,
            self.access.workspace()._active_binding(conn,reader.principal).operational_origin)
        self._schema(conn)
        row=conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
        if not row or row["active"]!=1: raise AccessDenied("setup_not_allowed")
        grant=self._grant(row)
        if (grant.binding_snapshot!=OwnerConnected._snapshot(admission.binding)
                or not grant.created_at<=self.access._now()<grant.expires_at or expected is not None and grant!=expected
                or effects and row["launch_id"] is not None): raise AccessDenied("setup_denied")
        return grant,row

    def recovery_host(self, principal):
        """Identity recovery only, for this grant's already-reserved activation."""
        with self.access._connection() as conn:
            self._schema(conn)
            binding=self.access.workspace()._active_binding(conn,principal)
            row=conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
            if not row or row["active"]!=1: raise AccessDenied("setup_denied")
            grant=self._grant(row)
            if (grant.binding_snapshot!=OwnerConnected._snapshot(binding)
                    or not grant.created_at<=self.access._now()<grant.expires_at
                    or not _uuid(row["launch_id"]) or not isinstance(row["launch_plan"],str)
                    or not re.fullmatch(r"p-[0-9a-f]{64}",row["launch_plan"])): raise AccessDenied("setup_denied")
            return binding.owner.host_uid

    def preview_capacity(self,reader,grant):
        with self.access._connection() as conn:
            self._admit(conn,reader,expected=grant,effects=True)
            now=self.access._now()
            count=conn.execute("SELECT count(*) FROM setup_previews WHERE generation=? AND created_at<=? AND expires_at>?",(grant.generation,now,now)).fetchone()[0]
            if count>=8: raise AccessUnavailable()

    def retained_request(self, reader):
        with self.access._connection() as conn:
            grant,row=self._admit(conn,reader)
            return ({"request_id":row["launch_id"],"plan_id":row["launch_plan"],"generation":grant.generation}
                if row["launch_id"] is not None else None)

    def owned_declaration(self, reader, grant):
        from ..config_plan import read_plan, path_state
        destination=self.access.root / "local" / grant.fleet / "fleet.yaml"
        with self.access._connection() as conn:
            self._admit(conn,reader,expected=grant,effects=True)
            rows=conn.execute("SELECT plan_id FROM setup_previews WHERE generation=? ORDER BY created_at DESC",(grant.generation,)).fetchall()
        for row in rows:
            plan=read_plan(self.access.root,row["plan_id"])
            recorded=plan.inputs.get(str(destination))
            if recorded and path_state(destination,source=recorded["follow_links"])==recorded["state"]:
                return True
        return False

    def authorize(self, reader, *, expected=None, effects=False):
        with self.access._connection() as conn:
            return self._admit(conn,reader,expected=expected,effects=effects)[0]

    def retain(self, reader, grant, data):
        with self.access._connection(write=True) as conn:
            self._admit(conn,reader,expected=grant,effects=True)
            now=self.access._now()
            conn.execute("DELETE FROM setup_previews WHERE expires_at<=? OR created_at>? OR generation!=?",(now,now,grant.generation))
            row=conn.execute("SELECT payload,expires_at FROM setup_previews WHERE plan_id=?",(data["plan_id"],)).fetchone()
            if row:
                retained=json.loads(row["payload"])
                # Copy metadata may differ, but canonical plan identity must not.
                if retained["release_id"]!=data["release_id"]: raise AccessDenied("setup_denied")
                return retained
            if conn.execute("SELECT count(*) FROM setup_previews").fetchone()[0]>=8: raise AccessUnavailable()
            payload={**data,"expires_at":min(now+PLAN_SECONDS,grant.expires_at),"generation":grant.generation}
            conn.execute("INSERT INTO setup_previews VALUES(?,?,?,?,?)",(data["plan_id"],grant.generation,now,payload["expires_at"],json.dumps(payload)))
            return payload

    def reserve(self, reader, grant, plan_id, request_id):
        if not _uuid(request_id): raise AccessDenied("setup_denied")
        with self.access._connection(write=True) as conn:
            _,row=self._admit(conn,reader,expected=grant)
            if row["launch_id"] is not None:
                if (row["launch_id"],row["launch_plan"])!=(request_id,plan_id): raise AccessDenied("setup_consumed")
                return False
            preview=conn.execute("SELECT * FROM setup_previews WHERE plan_id=? AND generation=?",(plan_id,grant.generation)).fetchone()
            now=self.access._now()
            if preview is None or not preview["created_at"]<=now<preview["expires_at"]: raise AccessDenied("setup_plan_expired")
            conn.execute("UPDATE setup_grant SET launch_id=?,launch_plan=? WHERE singleton=1",(request_id,plan_id))
            return True

    def request(self, reader, grant, plan_id, request_id):
        with self.access._connection() as conn:
            _,row=self._admit(conn,reader,expected=grant)
            if (row["launch_id"],row["launch_plan"])!=(request_id,plan_id): raise AccessDenied("setup_request_unknown")


class SetupEngine:
    def __init__(self, access):
        self.store=OwnerSetup(access)
        self.root=access.root
        from threading import Lock
        self._effects_lock=Lock()

    def _first_team(self,grant):
        from ..context import declared_paths
        from ..resources import get_resources
        from ..paths import _iter_fleet_dirs
        fresh_owner_setup(self.root)
        target=self.root / "local" / grant.fleet
        # No unrelated declaration or local runtime may ride a whole-host plan.
        if (self.root / "local").is_symlink(): raise AccessDenied("setup_other_fleet")
        if (self.root / "local").exists():
            if any(path!=target or path.is_symlink() for path in (self.root / "local").iterdir()): raise AccessDenied("setup_other_fleet")
        if target.exists() and any(path.name!="fleet.yaml" for path in target.iterdir()):
            raise AccessDenied("setup_other_inputs")  # No unapproved overlay/project policy rides the shipped template.
        declarations=declared_paths(self.root,get_resources())
        if any(paths.fleet_dir!=target for paths in declarations): raise AccessDenied("setup_other_fleet")
        for directory in _iter_fleet_dirs(self.root / "local"):
            runtime=directory / "runtime"
            if runtime.is_symlink() or runtime.exists() and (not runtime.is_dir() or next(runtime.iterdir(),None) is not None):
                raise AccessDenied("setup_existing_runtime")

    def _release(self,grant):
        from ..commands.releases import _executing_release,_release
        if _executing_release(self.root)!=grant.release_id: raise AccessDenied("setup_release_changed")
        release=_release(self.root,grant.release_id)
        if release.seal_sha256!=grant.release_seal: raise AccessDenied("setup_release_changed")

    def context(self,reader):
        grant=self.store.authorize(reader)
        self._release(grant)
        return {"version":1,"template":grant.template,"fleet":grant.fleet,"repository":grant.repository,
            "release_id":grant.release_id,"release_seal":grant.release_seal,"generation":grant.generation,
            "expires_at":grant.expires_at,"request":self.store.retained_request(reader)}

    def preview(self,reader,generation,goal):
        if not self._effects_lock.acquire(blocking=False): raise AccessUnavailable()
        try: return self._preview(reader,generation,goal)
        finally: self._effects_lock.release()

    def _preview(self,reader,generation,goal):
        from ..commands.setup import _fleet_setup
        grant=self.store.authorize(reader,effects=True)
        if grant.generation!=generation: raise AccessDenied("setup_denied")
        self._first_team(grant)
        self._release(grant)
        self.store.preview_capacity(reader,grant)
        output=_fleet_setup(SimpleNamespace(root=str(self.root),fleet=grant.fleet,seed=False,template=grant.template,
            config=None,repository=grant.repository,goal=goal,replace_config=self.store.owned_declaration(reader,grant),plan_only=True,install_directory=None))
        if output.data["fleets"] != [grant.fleet]: raise AccessDenied("setup_other_fleet")
        return self.store.retain(reader,grant,{**output.data,"version":1})

    def launch(self,reader,generation,plan_id,request_id):
        if not self._effects_lock.acquire(blocking=False): raise AccessUnavailable()
        try: return self._launch(reader,generation,plan_id,request_id)
        finally: self._effects_lock.release()

    def _launch(self,reader,generation,plan_id,request_id):
        from ..commands.host import _activate,_operator_shell
        from ..config_plan import read_plan
        grant=self.store.authorize(reader)
        if grant.generation!=generation: raise AccessDenied("setup_denied")
        self._release(grant)
        _operator_shell(self.root)
        plan=read_plan(self.root,plan_id)
        if plan.release_id!=grant.release_id or plan.release_seal!=grant.release_seal: raise AccessDenied("setup_denied")
        retained=self.store.retained_request(reader)
        if retained is not None:
            self.store.request(reader,grant,plan_id,request_id)
            return self.receipt(reader,generation,plan_id,request_id)
        self._first_team(grant)
        if plan.fleets != (grant.fleet,): raise AccessDenied("setup_other_fleet")
        plan.check_fresh()
        if self.store.reserve(reader,grant,plan_id,request_id):
            self._first_team(grant)
            if plan.fleets != (grant.fleet,): raise AccessDenied("setup_other_fleet")
            plan.check_fresh()
            self.store.authorize(reader,expected=grant)  # last authority check before native owner entry
            _activate(SimpleNamespace(plan_id=plan_id,install_directory=grant.install_directory,
                activation_id=request_id,adopt_existing=False),self.root)
        return self.receipt(reader,generation,plan_id,request_id)

    def receipt(self,reader,generation,plan_id,request_id):
        from ..commands.host import _recorded
        grant=self.store.authorize(reader)
        if grant.generation!=generation: raise AccessDenied("setup_denied")
        self.store.request(reader,grant,plan_id,request_id)
        record=_recorded(self.root,request_id)
        if record and (record["verification"]!="verified" or record["plan_id"]!=plan_id or record["release_id"]!=grant.release_id): raise AccessUnavailable()
        return {"version":1,"request_id":request_id,"plan_id":plan_id,"generation":generation,
            "state":record["status"] if record else "unknown","recorded_activation":record}
