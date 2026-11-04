"""Real optional approval/render storage; synthetic coordinator/native boundary."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import pytest

from claudlobby.plane.owner_access import OwnerAccess, AccessDenied, AccessUnavailable, VerifiedReader
from claudlobby.plane.owner_setup import OwnerSetup, SetupEngine
from claudlobby.plane.ids import ensure_host_uid
from tests.test_plane_owner_workspace import OWNER, proposed
from tests.test_plane_owner_connected import evidence
from tests.test_setup_commands import team_setup


@pytest.fixture
def setup_authority(team_setup, monkeypatch):
    from claudlobby import git_update, resources
    from claudlobby.config_plan import ConfigPlanBuilder
    args, root, effects = team_setup
    original = git_update._prepare_update_locked
    def prepare(store, **kwargs):
        record = original(store, **kwargs)
        manifest = root / "local/shop/fleet.yaml"
        projects = manifest.parent / "projects.yaml"
        builder = ConfigPlanBuilder(root, fleets=("shop",), effects={})
        _, fleet_digest = builder.input_snapshot(manifest)
        _, projects_digest = builder.input_snapshot(projects)
        builder.effects.update(fleet_sources={"shop": {
            "fleet": {"path": str(manifest), "sha256": fleet_digest},
            "projects": {"path": str(projects), "sha256": projects_digest}}},
            fleet_manifests={"shop": str(manifest)})
        rendered = builder.freeze(record["target"])
        args.retained["record"].update(config_fingerprint=rendered.fingerprint,
            initialization_scope=kwargs["initialization"])
        return dict(args.retained["record"])
    monkeypatch.setattr(git_update, "_prepare_update_locked", prepare)
    package = resources.get_resources()
    package.installation.bin.mkdir(parents=True)
    package.installation.cli.write_text("#!/bin/sh\nexit 0\n")
    package.installation.cli.chmod(0o755)
    ensure_host_uid(root / "state")
    clock = [1800000000.0]
    access = OwnerAccess.initialize(root, clock=lambda:clock[0], before_first_fleet=True)
    pair = access.begin_pairing(OWNER)
    owner = access.confirm_pairing(pair.token,expected_principal=OWNER)
    workspace = access.workspace(); workspace.initialize(expected_owner=owner)
    workspace.confirm(expected_proposal=proposed((access,workspace,owner,clock)))
    connected = access.connected(); connected.initialize(expected_owner=owner)
    session = connected.issue(evidence(connected,clock),OWNER)
    units = root.parent / "units"; units.mkdir()
    store = OwnerSetup(access)
    grant = store.candidate(OWNER,template="revenue-web-app",fleet="shop",repository="example-org/shop",
                            install_directory=str(units))
    return store,access,grant,VerifiedReader(OWNER,session.token),clock,args


def allow(fixture):
    store,access,grant,reader,*_=fixture
    store.allow(grant)
    return SetupEngine(access),grant,reader


def binding(preview):
    return {key:preview[key] for key in ("generation","request_id","config_fingerprint","source_revision")}


def test_no_setup_grant_or_optional_schema_never_repairs(setup_authority):
    store,access,grant,reader,*_=setup_authority
    before=access.path.read_bytes()
    with pytest.raises(AccessUnavailable): store.authorize(reader)
    assert access.path.read_bytes()==before
    store.allow(grant)
    assert store.authorize(reader)==grant
    with pytest.raises(AccessDenied):store.authorize(VerifiedReader(OWNER,access.open_session(OWNER).token))


def test_preview_freezes_one_canonical_original_request_without_dispatch(setup_authority):
    from claudlobby.config_plan import read_rendered
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    assert preview["version"]==2 and preview["fleets"]==["shop"]
    assert preview["dispatch"]=="prepared" and preview["phase"]=="quiesce"
    rendered=read_rendered(engine.root,preview["config_fingerprint"])
    rendered.check_inputs()
    assert rendered.revision==grant.source_revision and not setup_authority[-1].dispatches
    context=engine.context(reader)
    assert context["request"]==binding(preview) and not context["launch_reserved"]
    assert context["continuation"].endswith("host update continue")
    before=(engine.root/"local/shop/fleet.yaml").read_bytes()
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,"A changed goal")
    assert (engine.root/"local/shop/fleet.yaml").read_bytes()==before


@pytest.mark.parametrize("other",["other-fleet","other-runtime","target-library","projects"])
def test_whole_host_plan_cannot_include_unapproved_inputs(setup_authority,other):
    engine,grant,reader=allow(setup_authority)
    path=engine.root/"local"/("other" if other.startswith("other") else "shop")
    path.mkdir(parents=True)
    if other=="other-fleet": (path/"fleet.yaml").write_text("fleet: {name: other}\n")
    elif other=="other-runtime": (path/"runtime").mkdir()
    elif other=="target-library": (path/"library").mkdir()
    else: (path/"projects.yaml").write_text("projects: {}\n")
    with pytest.raises((AccessDenied,AccessUnavailable)):engine.preview(reader,grant.generation,"Improve checkout tests")
    assert not setup_authority[-1].dispatches


def test_changed_frozen_inputs_and_expired_preview_refuse_before_reservation(setup_authority):
    engine,grant,reader=allow(setup_authority)
    preview=engine.preview(reader,grant.generation,"Improve checkout tests")
    request=binding(preview)
    path=engine.root/"local/shop/fleet.yaml"; original=path.read_bytes(); path.write_bytes(original+b"\n")
    with pytest.raises(ValueError):engine.launch(reader,**request)
    assert not engine.context(reader)["launch_reserved"]
    path.write_bytes(original)
    setup_authority[4][0]=preview["expires_at"]
    with pytest.raises(AccessDenied):engine.launch(reader,**request)
    assert not setup_authority[-1].dispatches


def test_launch_uses_original_request_once_receipts_never_fill_unknown(setup_authority,monkeypatch):
    from claudlobby import git_update
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    result=engine.launch(reader,**request)
    assert result["state"]=="quiesce" and result["launch_reserved"]
    assert len(setup_authority[-1].dispatches)==1
    assert engine.launch(reader,**request)==result
    assert engine.receipt(reader,**request)==result
    with pytest.raises(AccessDenied):engine.launch(reader,**{**request,"request_id":str(uuid4())})
    monkeypatch.setattr(git_update,"read_update",lambda _:None)
    assert engine.receipt(reader,**request)["state"]=="unknown"
    assert engine.launch(reader,**request)["state"]=="unknown"
    assert len(setup_authority[-1].dispatches)==1


def test_lost_launch_response_is_reserved_and_never_replayed(setup_authority,monkeypatch):
    from claudlobby import git_update
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests")); calls=[]
    def lost(*_,**kwargs):calls.append(kwargs["expected_request_id"]);raise RuntimeError("lost response")
    monkeypatch.setattr(git_update,"apply_or_continue",lost)
    with pytest.raises(RuntimeError):engine.launch(reader,**request)
    assert engine.launch(reader,**request)["state"]=="quiesce"
    assert calls==[request["request_id"]]


def test_lost_preview_response_recovers_frozen_original_without_new_work(setup_authority,monkeypatch):
    engine,grant,reader=allow(setup_authority)
    monkeypatch.setattr(engine.store,"retain",lambda *_: (_ for _ in ()).throw(RuntimeError("lost preview")))
    with pytest.raises(RuntimeError):engine.preview(reader,grant.generation,"Improve checkout tests")
    context=engine.context(reader)
    assert context["request"]["request_id"]==setup_authority[-1].retained["record"]["request_id"]
    assert not context["launch_reserved"] and context["continuation"]
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,"Another goal")
    assert not setup_authority[-1].dispatches


@pytest.mark.parametrize("field,value",[("target","c"*40),("config_fingerprint","d"*64),
                                        ("initialization_scope",{"directory":"/different","manifests":[]})])
def test_original_request_mismatch_refuses_without_dispatch(setup_authority,field,value):
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    setup_authority[-1].retained["record"][field]=value
    with pytest.raises(AccessDenied):engine.receipt(reader,**request)
    assert not setup_authority[-1].dispatches


def test_partial_initialization_recovery_is_setup_only(setup_authority):
    from claudlobby.plane.owner_source import inspect_source
    from claudlobby.plane.db import db_file
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    engine.store.reserve(reader,grant,request)
    db_file(engine.root).write_bytes(b"not an admitted recorder")
    assert engine.store.recovery_host(OWNER)==setup_authority[1].current_grant().host_uid
    with pytest.raises((AccessDenied,AccessUnavailable)):inspect_source(engine.root)
    with pytest.raises(AccessDenied):engine.preview(reader,grant.generation,"Another goal")


def test_grant_revocation_and_workspace_rebinding_still_gate_receipts(setup_authority):
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    engine.store.revoke(grant)
    with pytest.raises(AccessDenied):engine.receipt(reader,**request)
    with pytest.raises(AccessDenied):engine.store.allow(replace(grant,generation=str(uuid4())))
    assert setup_authority[-1].retained["record"]["request_id"]==request["request_id"]


@pytest.mark.parametrize("malformed",["CREATE TABLE setup_metadata(singleton INTEGER,version INTEGER)",
    "CREATE VIEW setup_metadata AS SELECT 1 AS singleton,1 AS version", "CREATE TABLE setup_metadata(wrong INTEGER)"])
def test_optional_setup_absence_is_empty_but_partial_schema_refuses(setup_authority,malformed):
    store,access,*_=setup_authority
    before=access.path.read_bytes()
    assert store.local() is None and access.path.read_bytes()==before
    with access._connection(write=True) as conn:conn.execute(malformed)
    with pytest.raises(AccessUnavailable):store.local()


def test_historical_sealed_setup_is_retained_never_reinterpreted(setup_authority):
    store,access,grant,*_=setup_authority
    store.allow(grant)
    request=str(uuid4()); plan="p-"+"a"*64
    with access._connection(write=True) as conn:
        conn.execute("UPDATE setup_metadata SET version=1")
        conn.execute("UPDATE setup_grant SET launch_id=?,launch_plan=?",(request,plan))
    before=access.path.read_bytes()
    state=store.retained_state()
    assert state.state=="legacy_recovery_required" and state.grant is None
    assert (state.launch_id,state.launch_plan)==(request,plan)
    with pytest.raises(AccessUnavailable,match="historical sealed"):store.local()
    assert access.path.read_bytes()==before


def test_local_approval_uses_managed_source_and_observed_directory(setup_authority,monkeypatch):
    from claudlobby.__main__ import main
    from claudlobby import supervision_inventory
    from tests.test_host_owner import terminal
    store,access,grant,*_=setup_authority
    monkeypatch.setattr(supervision_inventory,"Adapter",lambda _:SimpleNamespace(read=lambda _:f"manager\tLinux\ndirectory\t{grant.install_directory}\n"))
    flags=["--root",str(access.root),"host","owner","allow-setup","--template",grant.template,
        "--target-fleet",grant.fleet,"--repository",grant.repository,"--install-directory",grant.install_directory]
    terminal(monkeypatch,iter(["NO"]))
    assert main(flags)!=0 and store.local() is None
    terminal(monkeypatch,iter(["ALLOW-SETUP"]))
    assert main(flags)==0 and store.local().source_revision==grant.source_revision
    terminal(monkeypatch,iter(["REVOKE-SETUP"]))
    assert main(["--root",str(access.root),"host","owner","revoke-setup"])==0
    assert store.local() is None


def test_guide_pending_original_returns_local_continuation_without_serving(setup_authority,monkeypatch):
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.command_result import CommandFailure
    engine,grant,reader=allow(setup_authority)
    request=binding(engine.preview(reader,grant.generation,"Improve checkout tests"))
    monkeypatch.setattr(guide,"_serving",lambda *_:pytest.fail("started a server"))
    with pytest.raises(CommandFailure) as caught:guide.run(SimpleNamespace(),SimpleNamespace(root=engine.root),setup_authority[1],None)
    assert caught.value.data["request_id"]==request["request_id"]
    assert "host update continue" in caught.value.error.hint


def test_guide_reuses_matching_git_approval_and_stable_cli_without_renewal(setup_authority,monkeypatch):
    from contextlib import contextmanager
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.context import resolve_paths
    from tests.test_host_owner import terminal
    from tests.test_plane_owner_workspace import ISSUER, ORIGIN
    store,access,grant,*_=setup_authority
    store.allow(grant)
    monkeypatch.setattr(guide,"_native_directories",lambda _: (Path(grant.install_directory),))
    commands=[]
    @contextmanager
    def serving(command,*_):
        commands.append(command)
        class Child:
            def wait(self):raise KeyboardInterrupt
        yield Child()
    monkeypatch.setattr(guide,"_serving",serving)
    args=SimpleNamespace(origin="https://host.example.test",tailscale=Path(sys.executable),tailscale_socket="",
        socket=access.root/"state/owner/owner.sock",workspace_issuer=ISSUER,workspace_origin=ORIGIN,
        target_fleet=grant.fleet,repository=grant.repository,install_directory=Path(grant.install_directory))
    console=terminal(monkeypatch,iter(())); before=access.path.read_bytes()
    result=guide.run(args,resolve_paths(root=access.root),access,console)
    assert result.data["state"]=="serving_stopped" and len(commands)==1
    assert commands[0][0]==str(Path(grant.installation)/"bin/claudlobby")
    assert access.path.read_bytes()==before and store.local()==grant


@pytest.mark.parametrize("platform,xdg,observed,preferred,override", [
    ("darwin", None, ("mac-system", "mac-user"), "mac-user", None),
    ("linux", None, ("control", "transient", "linux-system", "linux-user"), "linux-user", None),
    ("linux", "absolute", ("linux-user", "xdg-user", "linux-system"), "xdg-user", None),
    ("linux", None, ("control", "transient", "linux-system"), None, None),
    ("linux", None, ("linux-system",), None, None),
    ("linux", "absolute", ("linux-user",), None, None),
    ("linux", "relative", ("linux-user", "linux-system"), None, None),
    ("linux", None, ("linux-user", "linux-system"), "linux-user", "linux-system"),
    ("linux", None, ("linux-user",), "linux-user", "linux-system"),
], ids=["mac-user", "linux-user", "linux-xdg", "no-user-match", "only-system",
        "xdg-not-observed", "relative-xdg", "explicit-observed", "explicit-unobserved"])
def test_guide_install_directory_uses_only_observed_persistent_user_default(
        tmp_path, monkeypatch, platform, xdg, observed, preferred, override):
    import io
    import json
    import sys
    from claudlobby.commands import host_owner_setup as guide
    from claudlobby.command_result import CommandFailure

    home = tmp_path / "home"
    root = tmp_path / "data"
    directories = {
        "mac-user": home / "Library/LaunchAgents",
        "mac-system": Path("/Library/LaunchAgents"),
        "linux-user": home / ".config/systemd/user",
        "xdg-user": tmp_path / "config/systemd/user",
        "linux-system": Path("/etc/systemd/user"),
        "control": home / ".config/systemd/user.control",
        "transient": tmp_path / "run/systemd/transient",
    }
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    if xdg:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config") if xdg == "absolute" else "relative-config")
    monkeypatch.setattr(guide.sys, "platform", platform)
    monkeypatch.setattr(guide, "_native_directories", lambda _: tuple(directories[key] for key in observed))
    args = SimpleNamespace(origin="https://host.example.test", tailscale=sys.executable,
        tailscale_socket="", socket=str(root / "state/owner/owner.sock"),
        workspace_issuer="https://accounts.example.test", workspace_origin="https://plane.example.test",
        target_fleet="shop", repository="example-org/shop",
        install_directory=directories[override] if override else None)
    terminal = io.StringIO()
    terminal.readline = lambda size=-1: "\n"
    approval = error = None
    try:
        _, approval = guide._configuration(args, SimpleNamespace(root=root), terminal,
            Path(sys.executable))
    except CommandFailure as exc:
        error = exc
    output = terminal.getvalue()
    if override:
        if override in observed:
            assert error is None
            assert approval.install_directory == directories[override]
        else:
            assert error is not None and "not an observed native search path" in str(error)
    elif preferred:
        assert "Native installation directory [" + json.dumps(str(directories[preferred])) + "]: " in output
        assert error is None
        assert approval.install_directory == directories[preferred]
        assert "Native installation choices:" not in output
    else:
        assert "Native installation directory: " in output
        assert "Native installation directory [" not in output
        assert error is not None and "explicit printable configuration" in str(error)
        assert "Native installation choices:" in output
    assert not (root / "state").exists()
