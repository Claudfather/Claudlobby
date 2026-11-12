"""One locally approved first-team setup; the canonical Git operation owns all work.

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
import shlex

from .owner_access import AccessDenied, AccessUnavailable
from .owner_connected import OwnerConnected
from .owner_source import source_host_uid, fresh_owner_setup

PLAN_SECONDS = 600
GRANT_SECONDS = 86400
_SCHEMA = (
    "CREATE TABLE setup_metadata(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL)",
    "INSERT INTO setup_metadata VALUES(1,2)",
    # v2 keeps the existing reservation columns: launch_plan is the exact
    # configuration fingerprint, never a fabricated legacy plan identifier.
    "CREATE TABLE setup_grant(singleton INTEGER PRIMARY KEY CHECK(singleton=1),snapshot TEXT NOT NULL,active INTEGER NOT NULL,launch_id TEXT,launch_plan TEXT)",
    "CREATE TABLE setup_previews(plan_id TEXT PRIMARY KEY,generation TEXT NOT NULL,created_at REAL NOT NULL,expires_at REAL NOT NULL,payload TEXT NOT NULL)",
)

@dataclass(frozen=True)
class SetupGrant:
    binding_snapshot: str
    template: str
    fleet: str
    repository: str
    installation: str
    source_revision: str
    install_directory: str
    generation: str
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class RetainedSetupState:
    state: str
    grant: SetupGrant | None = None
    launch_id: str | None = None
    launch_plan: str | None = None
    effective_active: bool = False


def _snapshot(grant):
    return json.dumps(asdict(grant),sort_keys=True,separators=(",", ":"))


def _uuid(value):
    return isinstance(value,str) and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",value) is not None


def managed_setup(root, grant=None, *, current=True):
    from ..resources import get_resources
    package = get_resources()
    installation = package.installation
    if (installation is None or installation.data_root != root
            or not isinstance(package.source_revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", package.source_revision)
            or grant is not None and (str(installation.directory) != grant.installation
                or current and package.source_revision != grant.source_revision)):
        raise AccessDenied("setup_source_changed")
    return package


def _binding(grant, record):
    return {"generation": grant.generation, "request_id": record["request_id"],
            "config_fingerprint": record["config_fingerprint"], "source_revision": grant.source_revision}


def _continuation(root, grant):
    from ..installation import Installation
    cli = Installation(Path(grant.installation), root).cli
    return shlex.join([str(cli), "--root", str(root), "host", "update", "continue"])


class OwnerSetup:
    def __init__(self, access):
        self.access=access
        self.connected=access.connected()

    @staticmethod
    def _schema(conn):
        types=dict(conn.execute("SELECT name,type FROM sqlite_master WHERE name IN ('setup_metadata','setup_grant','setup_previews')"))
        if types!={name:"table" for name in ("setup_metadata","setup_grant","setup_previews")}: raise AccessUnavailable()
        rows=conn.execute("SELECT singleton,version FROM setup_metadata").fetchall()
        if len(rows)==1 and tuple(rows[0])==(1,1):
            raise AccessUnavailable("historical sealed setup requires local recovery; not a Git setup operation")
        if len(rows)!=1 or tuple(rows[0])!=(1,2): raise AccessUnavailable()
        conn.execute("SELECT singleton,snapshot,active,launch_id,launch_plan FROM setup_grant LIMIT 0")
        conn.execute("SELECT plan_id,generation,created_at,expires_at,payload FROM setup_previews LIMIT 0")

    def candidate(self, principal, *, template, fleet, repository, install_directory):
        # Reuse the installed template's finite repository/fleet policy, with a
        # harmless validation goal; no authoring, registration or staging here.
        from ..commands.setup import _template_config
        if not isinstance(fleet,str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*",fleet): raise AccessDenied("setup_denied")
        _template_config(template,fleet,repository,"Validate selected repository scope")
        package=managed_setup(self.access.root)
        directory=Path(install_directory)
        if (not directory.is_absolute() or directory.resolve()!=directory or not directory.is_dir()
                or directory.is_symlink()):
            raise AccessDenied("setup_denied")
        with self.access._connection() as conn:
            _,binding=self.connected._context(conn,principal)
            now=self.access._now()
            return SetupGrant(OwnerConnected._snapshot(binding),template,fleet,repository,str(package.installation.directory),package.source_revision,
                str(directory),str(uuid4()),now,now+GRANT_SECONDS)

    def allow(self, expected, *, expected_retained=None):
        if type(expected) is not SetupGrant: raise AccessDenied("setup_denied")
        with self.access._connection(write=True) as conn:
            binding=self.access.workspace()._active_binding(conn,self.access._grant(conn).principal)
            if OwnerConnected._snapshot(binding)!=expected.binding_snapshot: raise AccessDenied("setup_denied")
            if not expected.created_at<=self.access._now()<expected.expires_at: raise AccessDenied("setup_denied")
            names={row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            if not names & {"setup_metadata","setup_grant","setup_previews"}:
                for statement in _SCHEMA: conn.execute(statement)
            self._schema(conn)
            if expected_retained is not None and self._retained_state(conn) != expected_retained:
                raise AccessDenied("setup_state_changed")
            row=conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
            if row:
                retained=self._grant(row)
                if (row["active"] == 1 and retained.binding_snapshot==OwnerConnected._snapshot(binding)
                        and retained.created_at<=self.access._now()<retained.expires_at):
                    raise AccessDenied("setup_already_allowed")
                if row["launch_id"] is not None or conn.execute("SELECT 1 FROM setup_previews LIMIT 1").fetchone():
                    raise AccessDenied("setup_original_request_retained")
            conn.execute("INSERT OR REPLACE INTO setup_grant VALUES(1,?,1,NULL,NULL)",(_snapshot(expected),))

    def retained_state(self):
        """Local observation including withdrawn/expired grants and original launch IDs."""
        with self.access._connection() as conn:
            return self._retained_state(conn)

    def _retained_state(self, conn):
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        if not names & {"setup_metadata", "setup_grant", "setup_previews"}:
            return RetainedSetupState("absent")
        versions=conn.execute("SELECT singleton,version FROM setup_metadata").fetchall() if "setup_metadata" in names else []
        if len(versions)==1 and tuple(versions[0])==(1,1):
            row=conn.execute("SELECT launch_id,launch_plan FROM setup_grant WHERE singleton=1").fetchone()
            return RetainedSetupState("legacy_recovery_required", launch_id=row[0] if row else None,
                                      launch_plan=row[1] if row else None)
        self._schema(conn)
        row = conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
        if row is None:
            return RetainedSetupState("absent")
        if row["active"] not in (0, 1) or ((row["launch_id"] is None) != (row["launch_plan"] is None)):
            raise AccessUnavailable()
        if row["launch_id"] is not None and (not _uuid(row["launch_id"])
                or not isinstance(row["launch_plan"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["launch_plan"])):
            raise AccessUnavailable()
        grant = self._grant(row)
        now = self.access._now()
        self.access.workspace()._schema(conn)
        binding = self.access.workspace()._binding(conn)
        owner = self.access._grant(conn)
        state = ("revoked" if row["active"] == 0 else "expired" if now >= grant.expires_at
                 else "not_yet_valid" if now < grant.created_at
                 else "stale_binding" if binding is None or self.access.workspace()._state(binding, owner) != "active"
                 or OwnerConnected._snapshot(binding) != grant.binding_snapshot else "allowed")
        return RetainedSetupState(state, grant, row["launch_id"], row["launch_plan"], state == "allowed")

    def local(self):
        with self.access._connection() as conn:
            names={row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
            if not names & {"setup_metadata","setup_grant","setup_previews"}:
                return None
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
            # Local withdrawal uses current owner authority, not a still-active
            # workspace. Exact retained grant equality prevents replacement races.
            self.access._grant(conn)
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
        """Setup-only identity recovery bound to this approval's original operation."""
        with self.access._connection() as conn:
            self._schema(conn)
            binding=self.access.workspace()._active_binding(conn,principal)
            row=conn.execute("SELECT * FROM setup_grant WHERE singleton=1").fetchone()
            if not row or row["active"]!=1: raise AccessDenied("setup_denied")
            grant=self._grant(row)
            if (grant.binding_snapshot!=OwnerConnected._snapshot(binding)
                    or not grant.created_at<=self.access._now()<grant.expires_at): raise AccessDenied("setup_denied")
            if self._request(conn,grant) is None: raise AccessDenied("setup_request_unknown")
            return binding.owner.host_uid

    def authorize(self, reader, *, expected=None, effects=False):
        with self.access._connection() as conn:
            return self._admit(conn,reader,expected=expected,effects=effects)[0]

    def operation(self, grant):
        from ..git_update import read_update
        package=managed_setup(self.access.root,grant,current=False)
        record=read_update(package.installation)
        if record is None: return None
        scope={"directory":grant.install_directory,
               "manifests":[str(self.access.root/"local"/grant.fleet/"fleet.yaml")]}
        if (record.get("initialization_scope")!=scope or record.get("target")!=grant.source_revision
                or not record.get("initialize") or not _uuid(record.get("request_id"))
                or not isinstance(record.get("config_fingerprint"),str)
                or not re.fullmatch(r"[0-9a-f]{64}",record["config_fingerprint"])):
            raise AccessDenied("setup_original_operation_changed")
        return record

    def _request(self, conn, grant):
        row=conn.execute("SELECT payload FROM setup_previews WHERE generation=?",(grant.generation,)).fetchone()
        if row is None: return None
        payload=json.loads(row["payload"])
        request=payload.get("request")
        record=self.operation(grant)
        if record is None:
            if request is not None: return request  # Missing original operation never authorizes a retry.
            raise AccessUnavailable("setup preview outcome unknown; inspect host update status locally")
        actual=_binding(grant,record)
        if request is not None:
            if request!=actual: raise AccessDenied("setup_original_operation_changed")
        else:
            # Recover a lost preview response from the canonical frozen bytes,
            # not from mutable authoring or a replacement operation.
            from ..config_plan import read_rendered
            from ..commands.setup import _template_config
            rendered=read_rendered(self.access.root,record["config_fingerprint"])
            if rendered.revision!=grant.source_revision or rendered.fleets!=(grant.fleet,):
                raise AccessDenied("setup_other_fleet")
            sources=rendered.effects.get("fleet_sources")
            if not isinstance(sources,dict) or set(sources)!={grant.fleet}:
                raise AccessDenied("setup_original_operation_changed")
            item=sources[grant.fleet]
            if not isinstance(item,dict) or set(item)!={"fleet","projects"}:
                raise AccessDenied("setup_original_operation_changed")
            manifest,content=rendered.frozen_input(item["fleet"],required=True)
            projects,project_content=rendered.frozen_input(item["projects"])
            expected=self.access.root/"local"/grant.fleet/"fleet.yaml"
            if (manifest!=expected or projects!=expected.parent/"projects.yaml" or project_content is not None
                    or rendered.effects.get("fleet_manifests",{}).get(grant.fleet)!=str(expected)):
                raise AccessDenied("setup_original_operation_changed")
            if content!=_template_config(grant.template,grant.fleet,grant.repository,payload["goal"]):
                raise AccessDenied("setup_original_operation_changed")
        return actual

    def retained_request(self, reader):
        with self.access._connection() as conn:
            grant,row=self._admit(conn,reader)
            return self._request(conn,grant),row["launch_id"] is not None

    def begin_preview(self, reader, grant, goal):
        with self.access._connection(write=True) as conn:
            self._admit(conn,reader,expected=grant,effects=True)
            if conn.execute("SELECT 1 FROM setup_previews LIMIT 1").fetchone():
                raise AccessDenied("setup_original_request_retained")
            now=self.access._now()
            conn.execute("INSERT INTO setup_previews VALUES(?,?,?,?,?)",(grant.generation,grant.generation,now,
                min(now+PLAN_SECONDS,grant.expires_at),json.dumps({"goal":goal,"request":None})))

    def retain(self, reader, grant, record):
        with self.access._connection(write=True) as conn:
            self._admit(conn,reader,expected=grant,effects=True)
            request=self._request(conn,grant)
            if request!=_binding(grant,record): raise AccessDenied("setup_original_operation_changed")
            row=conn.execute("SELECT payload,expires_at FROM setup_previews WHERE generation=?",(grant.generation,)).fetchone()
            payload=json.loads(row["payload"])
            payload["request"]=request
            conn.execute("UPDATE setup_previews SET payload=? WHERE generation=?",(json.dumps(payload),grant.generation))
            return request,row["expires_at"]

    def reserve(self, reader, grant, request):
        with self.access._connection(write=True) as conn:
            _,row=self._admit(conn,reader,expected=grant)
            if self._request(conn,grant)!=request: raise AccessDenied("setup_request_unknown")
            if row["launch_id"] is not None:
                if (row["launch_id"],row["launch_plan"])!=(request["request_id"],request["config_fingerprint"]):
                    raise AccessDenied("setup_consumed")
                return False
            preview=conn.execute("SELECT created_at,expires_at FROM setup_previews WHERE generation=?",(grant.generation,)).fetchone()
            if not preview[0]<=self.access._now()<preview[1]: raise AccessDenied("setup_preview_expired")
            conn.execute("UPDATE setup_grant SET launch_id=?,launch_plan=? WHERE singleton=1",
                         (request["request_id"],request["config_fingerprint"]))
            return True

    def request(self, reader, grant, request):
        with self.access._connection() as conn:
            self._admit(conn,reader,expected=grant)
            if self._request(conn,grant)!=request: raise AccessDenied("setup_request_unknown")


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
        if (self.root / "local").is_symlink(): raise AccessDenied("setup_other_fleet")
        if (self.root / "local").exists():
            if any(path!=target or path.is_symlink() for path in (self.root / "local").iterdir()): raise AccessDenied("setup_other_fleet")
        if target.exists() and any(path.name!="fleet.yaml" for path in target.iterdir()):
            raise AccessDenied("setup_other_inputs")
        declarations=declared_paths(self.root,get_resources())
        if any(paths.fleet_dir!=target for paths in declarations): raise AccessDenied("setup_other_fleet")
        for directory in _iter_fleet_dirs(self.root / "local"):
            runtime=directory / "runtime"
            if runtime.is_symlink() or runtime.exists() and (not runtime.is_dir() or next(runtime.iterdir(),None) is not None):
                raise AccessDenied("setup_existing_runtime")

    def context(self,reader):
        grant=self.store.authorize(reader)
        managed_setup(self.root,grant,current=False)
        request,reserved=self.store.retained_request(reader)
        return {"version":2,"template":grant.template,"fleet":grant.fleet,"repository":grant.repository,
            "source_revision":grant.source_revision,"generation":grant.generation,"expires_at":grant.expires_at,
            "request":request,"launch_reserved":reserved,
            **({"continuation":_continuation(self.root,grant)} if request and not reserved else {})}

    def preview(self,reader,generation,goal):
        if not self._effects_lock.acquire(blocking=False): raise AccessUnavailable()
        try:
            from ..commands.setup import _fleet_setup, _template_config
            from ..config_plan import read_rendered
            grant=self.store.authorize(reader,effects=True)
            if grant.generation!=generation: raise AccessDenied("setup_denied")
            managed_setup(self.root,grant)
            self._first_team(grant)
            _template_config(grant.template,grant.fleet,grant.repository,goal)
            self.store.begin_preview(reader,grant,goal)
            output=_fleet_setup(SimpleNamespace(root=str(self.root),fleet=grant.fleet,seed=False,template=grant.template,
                config=None,repository=grant.repository,goal=goal,replace_config=False,plan_only=True,
                install_directory=grant.install_directory))
            record=output.data["update"]
            request,expires_at=self.store.retain(reader,grant,record)
            rendered=read_rendered(self.root,request["config_fingerprint"])
            return {"version":2,**request,"expires_at":expires_at,
                "fleets":list(rendered.fleets),"changes":[asdict(change) for change in rendered.changes],
                "effects":rendered.effects,"dispatch":"prepared","phase":record["phase"]}
        finally: self._effects_lock.release()

    def launch(self,reader,generation,request_id,config_fingerprint,source_revision):
        if not self._effects_lock.acquire(blocking=False): raise AccessUnavailable()
        try:
            from ..git_update import apply_or_continue
            from ..config_plan import read_rendered
            grant=self.store.authorize(reader)
            request={"generation":generation,"request_id":request_id,"config_fingerprint":config_fingerprint,
                     "source_revision":source_revision}
            self.store.request(reader,grant,request)
            _,reserved=self.store.retained_request(reader)
            if reserved: return self.receipt(reader,**request)
            package=managed_setup(self.root,grant)
            self._first_team(grant)
            rendered=read_rendered(self.root,config_fingerprint)
            rendered.check_inputs()
            if self.store.reserve(reader,grant,request):
                self.store.authorize(reader,expected=grant)
                apply_or_continue(package.installation,expected_request_id=request_id)
            return self.receipt(reader,**request)
        finally: self._effects_lock.release()

    def receipt(self,reader,generation,request_id,config_fingerprint,source_revision):
        grant=self.store.authorize(reader)
        request={"generation":generation,"request_id":request_id,"config_fingerprint":config_fingerprint,
                 "source_revision":source_revision}
        self.store.request(reader,grant,request)
        record=self.store.operation(grant)
        _,reserved=self.store.retained_request(reader)
        return {"version":2,**request,"state":record["phase"] if record else "unknown",
            "launch_reserved":reserved,"config_committed":bool(record and record.get("config_committed")),
            "health_errors":record.get("health_errors",{}) if record else {},
            **({"continuation":_continuation(self.root,grant)} if record and not reserved else {})}
