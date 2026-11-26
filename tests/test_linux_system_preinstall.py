"""Standalone root candidate: native effects are always task-owned fixtures."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import types

import pytest

from tests.conftest import constructed_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "claudlobby/preinstall/linux-system-v1.sh"


def payload():
    assert SCRIPT.is_file(), "bounded root payload has not been implemented"
    body = SCRIPT.read_text().split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
    module = types.ModuleType("linux_system_fixture")
    exec(compile(body, str(SCRIPT), "exec"), module.__dict__)
    return module


def selection():
    return {"account": "fixture", "uid": 1001, "gid": 1001, "home": "/home/user",
            "node_key": "nodekey:" + "a" * 64, "profile_id": "p001"}


def minimal_selection():
    return {key:value for key,value in selection().items() if key in ("account","uid","node_key")}


def request(**changes):
    value = {"schema_version": 1, "request_id": "11111111-1111-4111-8111-111111111111",
             "artifact_sha256": "b" * 64, "selection": minimal_selection(), "kind": "system"}
    value.update(changes)
    return value


def test_unimplemented_payload_is_a_missing_contract_not_native_execution():
    module = payload()
    assert module.validate_request("inspect", request()) == request()


@pytest.mark.parametrize("change", [{"schema_version": True}, {"kind": "shell"}, {"extra": "argv"},
    {"request_id": "../escape"}, {"artifact_sha256": "main"}, {"selection": {**minimal_selection(), "uid": 0}},
    {"selection": {**selection(), "home": "/home/user/../root"}},
    {"selection": {**minimal_selection(), "account": "-other"}}, {"selection": {**selection(), "gid": True}}])
def test_wire_refuses_arbitrary_or_substituted_identity(change):
    module = payload()
    with pytest.raises(module.Refusal):
        module.validate_request("inspect", request(**change))


def test_apply_cannot_supply_replacement_plan():
    module = payload()
    value = request(selection=selection()); value.pop("kind")
    value.update(digest="c" * 64, decision="approve")
    assert module.validate_request("apply", value) == value
    with pytest.raises(module.Refusal):
        module.validate_request("apply", {**value, "packages": ["sudo"]})


def test_unknown_shell_verb_refuses_without_native_commands():
    result = subprocess.run(["/bin/sh", str(SCRIPT), "shell"], input=b"{}\n",
                            env=constructed_env(), capture_output=True, timeout=5)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "invalid_request"
    assert not result.stderr


def test_apt_plan_discloses_exact_install_and_upgrade():
    module = payload()
    rows = module.parse_simulation("Inst git (1:2.43 Ubuntu:24.04/noble [amd64])\n"
                                   "Inst libthing [1.0] (1.1 Ubuntu:24.04/noble [amd64])\n"
                                   "Conf git (1:2.43 Ubuntu:24.04/noble [amd64])\n", "amd64")
    assert rows == [{"package": "git", "old": "-", "new": "1:2.43", "architecture": "amd64"},
                    {"package": "libthing", "old": "1.0", "new": "1.1", "architecture": "amd64"}]


@pytest.mark.parametrize("line", ["Remv unwanted [1.0]", "Inst systemd (255 Ubuntu [amd64])",
    "Inst libc6 (2.39 Ubuntu [amd64])", "Inst tailscale (1.0 Ubuntu [amd64])",
    "Inst linux-image-test (1.0 Ubuntu [amd64])", "Inst git (1.0 Ubuntu [arm64])",
    "Inst git malformed", "Purg unwanted [1.0]"])
def test_apt_refuses_removal_critical_foreign_and_unparseable_actions(line):
    module = payload()
    with pytest.raises(module.Refusal):
        module.parse_simulation(line + "\n", "amd64")


def hook_text(version="3", new="1:2.43", action="/var/cache/apt/archives/git.deb"):
    return f"VERSION {version}\nAPT::Architecture=amd64\n\ngit - - none < {new} amd64 none {action}\ngit - - none < {new} amd64 none **CONFIGURE**\n"


def test_apt3_hook_checks_exact_package_version_architecture_and_actions():
    module = payload()
    expected = [{"package": "git", "old": "-", "new": "1:2.43", "architecture": "amd64"}]
    assert module.verify_hook(hook_text(), expected) == {"unpack": 1, "configure": 1}
    for text in [hook_text(version="2"), hook_text(new="1:2.44"), hook_text(action="**REMOVE**"),
                 hook_text().replace("amd64 none", "arm64 none"), hook_text().split("git -", 1)[0],
                 hook_text() + "jq - - none < 1 amd64 none /var/cache/apt/archives/jq.deb\n"]:
        with pytest.raises(module.Refusal):
            module.verify_hook(text, expected)


def test_null_serve_and_other_routes_are_preserved_with_fixed_owner_socket():
    module = payload()
    expected = {"TCP": {"443": {"HTTPS": True}}, "Web": {"fixture.example.ts.net:443": {
        "Handlers": {"/": {"Proxy": "unix:/home/user/.local/share/claudlobby-browser/data/state/owner/owner.sock"}}}}}
    assert module.ingress_after(None, "fixture.example.ts.net", "/home/user") == expected
    old = {"TCP": {"8443": {"HTTPS": True}}, "Web": {"other.example.ts.net:8443": {
        "Handlers": {"/": {"Text": "other"}}}}, "AllowFunnel": {"other.example.ts.net:8443": True},
        "Services": {"svc:other": {"Tun": True}}}
    before = copy.deepcopy(old)
    after = module.ingress_after(old, "fixture.example.ts.net", "/home/user")
    assert old == before
    assert after["AllowFunnel"] == old["AllowFunnel"] and after["Services"] == old["Services"]
    assert after["TCP"]["8443"] == old["TCP"]["8443"]
    assert after["Web"]["other.example.ts.net:8443"] == old["Web"]["other.example.ts.net:8443"]


@pytest.mark.parametrize("old", [{"FutureField": {}}, {"Foreground": {"session": {}}},
    {"AllowFunnel": {"fixture.example.ts.net:443": True}}, {"TCP": {"443": {"TCPForward": "localhost:22"}}},
    {"Web": {"other.example.ts.net:443": {"Handlers": {"/": {"Text": "occupied"}}}}},
    {"Web": {"fixture.example.ts.net:443": {"Handlers": {"/other": {"Text": "occupied"}}}}},
    {"Services": {"svc:other": {"Future": True}}}])
def test_serve_refuses_unknown_schema_foreground_funnel_and_443_conflicts(old):
    module = payload()
    with pytest.raises(module.Refusal):
        module.ingress_after(old, "fixture.example.ts.net", "/home/user")


def root_fixture(module, tmp_path, monkeypatch, kind="system"):
    import hashlib
    store = tmp_path / "store"; store.mkdir(mode=0o700)
    monkeypatch.setattr(module, "STORE", store)
    monkeypatch.setattr(module, "OWNER", os.geteuid())
    # The test account cannot create root-owned ancestors. Keep file/lstat,
    # atomic fsync/replace and flock real; replace only the root ancestor gate.
    monkeypatch.setattr(module, "trusted_parents", lambda path: None)
    source = tmp_path / "incoming.sh"; source.write_bytes(SCRIPT.read_bytes()); source.chmod(0o500)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    value = request(artifact_sha256=sha, kind=kind)
    monkeypatch.setattr(module, "boot_id", lambda: "22222222-2222-4222-8222-222222222222")
    monkeypatch.setattr(module, "binding", lambda selected, api: {"selection": selected, "dns": "fixture.example.ts.net"})
    plan = {"kind": kind, "binding": {"selection": selection(), "dns": "fixture.example.ts.net"}}
    if kind == "system":
        plan.update(packages={"actions": [], "seeds": []}, manager={"linger": False, "units": {}, "ordinary_user_bus": "unproved"})
    monkeypatch.setattr(module, "inspect_plan", lambda selected_kind, selected, api: copy.deepcopy(plan))
    monkeypatch.setattr(module, "package_health", lambda: {})
    monkeypatch.setattr(module, "derive_selection", lambda selected, api: selection())
    monkeypatch.setattr(module, "manager_runtime", lambda selected: {})
    return source, value, plan


def approval(value, record):
    result = {k: v for k, v in value.items() if k != "kind"}
    return {**result, "selection": record["selection"], "digest": record["digest"], "decision": "approve"}


def test_dispatch_receipt_is_durable_before_unknown_submission_and_never_replayed(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    record = module.execute("inspect", value, source, None)
    assert record["state"] == "planned"
    submissions = []
    def native(argv, effect=False):
        assert not effect
        durable = module.load(value["request_id"])
        assert durable["state"] == "dispatched" and durable["dispatch"]["boot_id"]
        submissions.append(argv)
        raise subprocess.TimeoutExpired(argv, 15)
    monkeypatch.setattr(module, "native", native)
    monkeypatch.setattr(module, "unit_state", lambda unit: {"ActiveState": "inactive", "MainPID": "0"})
    result = module.execute("apply", approval(value, record), source, None)
    assert result["state"] == "dispatched" and result["submission"] == "unknown"
    result = module.execute("apply", approval(value, record), source, None)
    assert result["observation"]["state"] == "unknown" and len(submissions) == 1
    argv = submissions[0]
    assert "--service-type=exec" in argv and "--property=Restart=no" in argv
    assert "--property=RuntimeMaxSec=infinity" in argv and "--wait" not in argv and "--collect" not in argv
    assert argv[-3:] == [str(module.artifact_path(value["artifact_sha256"])), "_worker", value["request_id"]]


def test_expired_changed_or_edited_plan_refuses_before_dispatch(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    record = module.execute("inspect", value, source, None)
    monkeypatch.setattr(module, "native", lambda *a, **k: pytest.fail("effect before original approval"))
    with pytest.raises(module.Refusal):
        module.execute("apply", {**approval(value, record), "digest": "d" * 64}, source, None)
    plan["new_drift"] = "changed"
    with pytest.raises(module.Refusal):
        module.execute("apply", approval(value, record), source, None)
    assert module.load(value["request_id"])["state"] == "planned"
    record["expires_at"] = 1; module.save(record)
    with pytest.raises(module.Refusal):
        module.execute("apply", approval(value, record), source, None)


def test_unknown_prior_identity_blocks_new_uuid_without_repair(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    record = module.execute("inspect", value, source, None); record["state"] = "unknown"; module.save(record)
    with pytest.raises(module.Refusal) as error:
        module.execute("inspect", {**value, "request_id": "33333333-3333-4333-8333-333333333333"}, source, None)
    assert error.value.code == "prior_operation_uncertain"
    assert module.load(value["request_id"])["state"] == "unknown"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "oversize"])
def test_private_receipt_files_refuse_redirected_or_foreign_storage(tmp_path, monkeypatch, kind):
    module = payload(); monkeypatch.setattr(module, "OWNER", os.geteuid())
    monkeypatch.setattr(module, "trusted_parents", lambda path: None)
    path = tmp_path / "record"; path.write_bytes(b"{}"); path.chmod(0o600)
    if kind == "symlink":
        target = tmp_path / "elsewhere"; path.rename(target); path.symlink_to(target)
    elif kind == "hardlink":
        os.link(path, tmp_path / "other")
    elif kind == "mode":
        path.chmod(0o644)
    else:
        path.write_bytes(b"x" * 262145)
    with pytest.raises((module.Refusal, OSError)):
        module.read_file(path)


def test_ingress_one_original_etag_post_with_whole_after_compare_and_no_socket_probe(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch, "ingress")
    after = module.ingress_after(None, "fixture.example.ts.net", selection()["home"])
    plan.update(before=None, etag="original-etag", after=after)
    calls = []
    def api(method, endpoint, body=None, etag=None):
        calls.append((method, endpoint, copy.deepcopy(body), etag))
        return (200, "after-etag", after if method == "GET" else None)
    record = module.execute("inspect", value, source, api)
    result = module.execute("apply", approval(value, record), source, api)
    assert result["state"] == "completed"
    assert calls == [("POST", "serve-config", after, "original-etag"), ("GET", "serve-config", None, None)]
    module.execute("apply", approval(value, record), source, api)
    assert sum(method == "POST" for method, *_ in calls) == 1


@pytest.mark.parametrize("outcome", ["412", "lost", "changed"])
def test_ingress_conflict_unknown_and_whole_state_drift_are_never_retried(tmp_path, monkeypatch, outcome):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch, "ingress")
    after = module.ingress_after(None, "fixture.example.ts.net", selection()["home"])
    plan.update(before=None, etag="approved-original", after=after)
    posts = []
    def api(method, endpoint, body=None, etag=None):
        if method == "POST":
            posts.append(etag)
            if outcome == "lost": raise OSError("lost reply")
            return (412 if outcome == "412" else 200, None, None)
        return (200, "later", {**after, "AllowFunnel": {"elsewhere.example.ts.net:8443": True}})
    record = module.execute("inspect", value, source, api)
    result = module.execute("apply", approval(value, record), source, api)
    assert result["state"] == ("conflict" if outcome == "412" else "unknown")
    module.execute("apply", approval(value, record), source, api)
    assert posts == ["approved-original"]


def test_private_worker_requires_current_process_unit_identity_and_records_real_linger(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    record = module.execute("inspect", value, source, None)
    record["state"] = "dispatched"; record["dispatch"] = {"boot_id": module.boot_id()}; module.save(record)
    invocation = "1" * 32
    monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setattr(module, "private_record", lambda rid, path: module.load(rid))
    monkeypatch.setattr(module, "unit_state", lambda name: {"InvocationID": invocation, "MainPID": str(os.getpid())})
    monkeypatch.setattr(module, "package_plan", lambda: plan["packages"])
    manager = copy.deepcopy(plan["manager"])
    monkeypatch.setattr(module, "manager_plan", lambda selected: copy.deepcopy(manager))
    effects = []
    def native(argv, effect=False):
        assert effect and argv == ["/usr/bin/loginctl", "enable-linger", "fixture"]
        assert module.load(value["request_id"])["state"] == "running"
        manager["linger"] = True; effects.append(argv); return 0
    monkeypatch.setattr(module, "native", native)
    module.private_worker(value["request_id"], module.artifact_path(value["artifact_sha256"]), None)
    result = module.load(value["request_id"])
    assert result["state"] == "completed" and result["invocation_id"] == invocation
    assert result["manager_observation"]["ordinary_user_bus"] == "unproved" and len(effects) == 1
    with pytest.raises(module.Refusal):
        module.private_worker(value["request_id"], module.artifact_path(value["artifact_sha256"]), None)


def test_no_package_success_when_native_skips_verifier(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    plan["packages"]["actions"] = [{"package": "git", "old": "-", "new": "1:2.43", "architecture": "amd64"}]
    plan["packages"]["seeds"] = ["git"]
    record = module.execute("inspect", value, source, None); record["state"] = "dispatched"
    record["dispatch"] = {"boot_id": module.boot_id()}; module.save(record)
    invocation = "2" * 32; monkeypatch.setenv("INVOCATION_ID", invocation)
    monkeypatch.setattr(module, "private_record", lambda rid, path: module.load(rid))
    monkeypatch.setattr(module, "unit_state", lambda name: {"InvocationID": invocation, "MainPID": str(os.getpid())})
    monkeypatch.setattr(module, "package_plan", lambda: plan["packages"])
    monkeypatch.setattr(module, "manager_plan", lambda selected: plan["manager"])
    effects = []
    def native(argv, effect=False):
        effects.append(argv); return 0
    monkeypatch.setattr(module, "native", native)
    module.private_worker(value["request_id"], module.artifact_path(value["artifact_sha256"]), None)
    result = module.load(value["request_id"])
    assert result["state"] == "failed_partial" and result["code"] == "apt_verifier_not_observed"
    assert all(argv[0] != "/usr/bin/loginctl" for argv in effects)
    apt = effects[0]
    artifact = str(module.artifact_path(value["artifact_sha256"]))
    assert "DPkg::Pre-Install-Pkgs::=" + artifact + " _hook " + value["request_id"] in apt
    assert "DPkg::Tools::Options::" + artifact + "::Version=3" in apt
    assert all("DPkg::Tools::Options::/bin/sh" not in arg for arg in apt)



def test_transition_lock_can_wait_for_frontend_without_replaying_effect(tmp_path, monkeypatch):
    import threading
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    module.execute("inspect", value, source, None)
    frontend = module.lock_store(); acquired = threading.Event()
    def worker():
        fd = module.lock_store(blocking=True)
        acquired.set(); os.close(fd)
    thread = threading.Thread(target=worker); thread.start()
    assert not acquired.wait(0.05)
    os.close(frontend); thread.join(2)
    assert acquired.is_set() and not thread.is_alive()


def test_status_after_reboot_preserves_running_receipt_and_reports_unknown(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    record = module.execute("inspect", value, source, None); record["state"] = "running"
    record["dispatch"] = {"boot_id": "33333333-3333-4333-8333-333333333333"}; record["invocation_id"] = "4" * 32
    module.save(record)
    monkeypatch.setattr(module, "unit_state", lambda name: {"InvocationID": "5" * 32, "ActiveState": "active", "MainPID": "987"})
    value.pop("kind")
    result = module.execute("status", value, source, None)
    assert result["record"]["state"] == "running" and result["observation"]["state"] == "unknown"
    assert module.load(value["request_id"])["dispatch"]["boot_id"] == "33333333-3333-4333-8333-333333333333"


def test_private_worker_refuses_artifact_substitution_before_native_effect(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    module.execute("inspect", value, source, None)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    with pytest.raises(module.Refusal) as error:
        module.private_record(value["request_id"], source)
    assert error.value.code == "private_artifact_required"
    artifact = module.artifact_path(value["artifact_sha256"])
    artifact.chmod(0o700)
    with pytest.raises(module.Refusal):
        module.private_record(value["request_id"], artifact)


def test_ingress_existing_exact_route_is_observed_without_another_post(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch, "ingress")
    after = module.ingress_after(None, "fixture.example.ts.net", selection()["home"])
    plan.update(before=after, after=after, etag="unchanged", no_op=True)
    def api(method, endpoint, body=None, etag=None):
        assert method == "GET", "an exact existing route must never be rewritten"
        return 200, "unchanged", after
    record = module.execute("inspect", value, source, api)
    result = module.execute("apply", approval(value, record), source, api)
    assert result["state"] == "completed" and result["effect"] == "none_existing_exact_route"


def test_index_refresh_proposal_binds_and_discloses_configured_update_hooks(tmp_path, monkeypatch):
    module = payload(); source, value, _plan = root_fixture(module, tmp_path, monkeypatch, "index-refresh")
    # Exercise production inspection, replacing only native root facts/calls.
    del source, value
    body = SCRIPT.read_text().split("# BEGIN EMBEDDED PYTHON\n", 1)[1].split("# END EMBEDDED PYTHON\n", 1)[0]
    real = types.ModuleType("fresh_inspection"); exec(compile(body, str(SCRIPT), "exec"), real.__dict__)
    monkeypatch.setattr(real, "binding", lambda selected, api: {"selection": selected})
    monkeypatch.setattr(real, "manager_plan", lambda selected: {"linger": False})
    monkeypatch.setattr(real, "package_health", lambda: {})
    config = 'APT::Update::Post-Invoke:: "/root/fixed-operator-hook";\n'
    def native(argv, effect=False):
        assert not effect
        return "apt 2.8.3\n" if argv == ["/usr/bin/apt-get", "--version"] else config if argv == ["/usr/bin/apt-config", "dump"] else pytest.fail(f"unexpected native {argv}")
    monkeypatch.setattr(real, "native", native)
    result = real.inspect_plan("index-refresh", selection(), None)
    assert result["apt"]["hooks"] == ['APT::Update::Post-Invoke:: "/root/fixed-operator-hook";']
    assert len(result["apt"]["config_digest"]) == 64


def test_missing_seed_plan_uses_only_fixed_missing_seeds_without_index_refresh(monkeypatch):
    module = payload(); calls = []
    monkeypatch.setattr(module, "package_health", lambda: {"git": {"version": "2", "architecture": "amd64"},
        "python3-venv": {"version": "3", "architecture": "amd64"}, "ca-certificates": {"version": "1", "architecture": "all"},
        "tmux": {"version": "3", "architecture": "amd64"}})
    def native(argv, effect=False):
        assert not effect; calls.append(argv)
        if argv == ["/usr/bin/apt-get", "--version"]: return "apt 2.8.3\n"
        if argv == ["/usr/bin/dpkg", "--print-architecture"]: return "amd64\n"
        if argv == ["/usr/bin/dpkg", "--print-foreign-architectures"]: return ""
        if argv == ["/usr/bin/apt-config", "dump"]: return 'APT::Architecture "amd64";\n'
        if argv == ["/usr/bin/apt-mark", "showhold"]: return ""
        assert argv[-4:] == ["--simulate", "install", "--", "jq"]
        assert "--no-install-recommends" in argv and "--no-remove" in argv
        return "Inst jq (1.7 Ubuntu [amd64])\nConf jq (1.7 Ubuntu [amd64])\n"
    monkeypatch.setattr(module, "native", native)
    result = module.package_plan()
    assert result["seeds"] == ["jq"] and result["actions"] == [{"package": "jq", "old": "-", "new": "1.7", "architecture": "amd64"}]
    assert all("update" not in argv and "upgrade" not in argv for argv in calls)


def test_masked_manager_refuses_without_enabling_linger(monkeypatch):
    module = payload()
    monkeypatch.setattr(module.Path, "is_dir", lambda path: True)
    monkeypatch.setattr(module, "native", lambda argv, effect=False: "systemd 255 (255.4)\n" if argv == ["/usr/bin/systemctl", "--version"] else pytest.fail("unexpected effect"))
    monkeypatch.setattr(module, "unit_state", lambda name: {"LoadState": "masked", "DropInPaths": ""})
    with pytest.raises(module.Refusal) as error:
        module.manager_plan(selection())
    assert error.value.code == "manager_masked_or_custom"


@pytest.mark.parametrize("fragment", ["/etc/systemd/system/user@.service",
    "/etc/systemd/system/user@1001.service", "/run/systemd/generator/user@.service",
    "/usr/lib/systemd/system/user@1001.service", "/lib/systemd/system/user@.service"])
def test_loaded_custom_manager_fragment_refuses_without_effect(monkeypatch, fragment):
    module = payload()
    monkeypatch.setattr(module.Path, "is_dir", lambda path: True)
    monkeypatch.setattr(module, "native", lambda argv, effect=False: "systemd 255 (255.4)\n" if argv == ["/usr/bin/systemctl", "--version"] else pytest.fail("unexpected native operation"))
    monkeypatch.setattr(module, "unit_state", lambda name: {"LoadState": "loaded", "DropInPaths": "", "FragmentPath": fragment})
    with pytest.raises(module.Refusal) as error:
        module.manager_plan(selection())
    assert error.value.code == "manager_masked_or_custom"


def manager_vendor_fixture(monkeypatch):
    import stat
    from types import SimpleNamespace
    module = payload(); checked = []
    monkeypatch.setattr(module.Path, "is_dir", lambda path: True)
    monkeypatch.setattr(module, "native", lambda argv, effect=False: "systemd 255 (255.4)\n" if argv == ["/usr/bin/systemctl", "--version"] else pytest.fail("unexpected native operation"))
    monkeypatch.setattr(module, "trusted_parents", lambda path: checked.append(str(path)))
    monkeypatch.setattr(module.Path, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1))
    states = {name + "1001.service": {"LoadState": "loaded", "FragmentPath": "/usr/lib/systemd/system/" + name + ".service", "DropInPaths": ""}
              for name in ("user@", "user-runtime-dir@")}
    monkeypatch.setattr(module, "unit_state", lambda name: states[name])
    return module, states, checked


@pytest.mark.parametrize("drops", [[], ["10-login-barrier.conf"], ["timeout.conf"], ["10-login-barrier.conf", "timeout.conf"]])
def test_vendor_manager_templates_and_known_dropins_are_admitted(monkeypatch, drops):
    module, states, checked = manager_vendor_fixture(monkeypatch)
    paths = ["/usr/lib/systemd/system/user@.service.d/" + suffix for suffix in drops]
    states["user@1001.service"]["DropInPaths"] = " ".join(paths)
    result = module.manager_plan(selection())
    assert result["units"] == states and result["ordinary_user_bus"] == "unproved"
    assert checked == ["/usr/lib/systemd/system/user@.service", *paths,
                       "/usr/lib/systemd/system/user-runtime-dir@.service"]


@pytest.mark.parametrize("unit,drop", [("user@1001.service", "/etc/systemd/system/user@.service.d/timeout.conf"),
    ("user@1001.service", "/usr/lib/systemd/system/user@.service.d/custom.conf"),
    ("user-runtime-dir@1001.service", "/usr/lib/systemd/system/user@.service.d/timeout.conf"),
    ("user@1001.service", " ".join(["/usr/lib/systemd/system/user@.service.d/timeout.conf"] * 2))])
def test_custom_or_duplicate_manager_dropin_refuses(monkeypatch, unit, drop):
    module, states, checked = manager_vendor_fixture(monkeypatch)
    states[unit]["DropInPaths"] = drop
    with pytest.raises(module.Refusal) as error:
        module.manager_plan(selection())
    assert error.value.code == "manager_masked_or_custom"


@pytest.mark.parametrize("metadata", [{"st_uid": 1001}, {"st_mode": 0o100664},
    {"st_mode": 0o120777}, {"st_nlink": 2}])
def test_vendor_manager_file_must_be_root_regular_and_not_writable(monkeypatch, metadata):
    from types import SimpleNamespace
    module, states, checked = manager_vendor_fixture(monkeypatch)
    monkeypatch.setattr(module.Path, "lstat", lambda path: SimpleNamespace(**{
        **{"st_mode": 0o100644, "st_uid": 0, "st_nlink": 1}, **metadata}))
    with pytest.raises(module.Refusal) as error:
        module.manager_plan(selection())
    assert error.value.code == "manager_masked_or_custom"


def test_native_binding_rechecks_selected_account_node_profile_and_daemon(monkeypatch):
    from types import SimpleNamespace
    module = payload()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=1001, pw_gid=1001, pw_dir="/home/user", pw_shell="/bin/bash"))
    monkeypatch.setattr(module.Path, "read_text", lambda path: 'ID=ubuntu\nVERSION_ID="24.04"\n' if str(path)=="/etc/os-release" else pytest.fail("unexpected file"))
    monkeypatch.setattr(module.Path, "read_bytes", lambda path: b"synthetic-machine" if str(path)=="/etc/machine-id" else pytest.fail("unexpected file"))
    class API:
        identity = {"pid": 123, "start": "456", "binary_digest": "c" * 64, "socket_inode": 789}
        drift = None
        version = "1.104.7-tfb3b9b296-g0651cc45e"
        def __call__(self, method, endpoint, body=None, etag=None):
            assert method == "GET"
            if endpoint == "profiles/current":
                if self.drift == "daemon": self.identity = {**self.identity, "pid": 124}
                return 200, None, {"ID": "wrong" if self.drift == "profile" else "p001"}
            assert endpoint == "status"
            return 200, None, {"Version": self.version, "BackendState": "Running", "TUN": True,
                "Self": {"PublicKey": "nodekey:" + ("d" if self.drift == "node" else "a") * 64,
                         "DNSName": "fixture.example.ts.net."}, "CertDomains": ["fixture.example.ts.net"]}
    api = API(); result = module.binding(selection(), api)
    assert result["daemon"] == api.identity
    for drift in ("daemon", "profile", "node"):
        api = API(); api.drift = drift
        with pytest.raises(module.Refusal): module.binding(selection(), api)
    with pytest.raises(module.Refusal):
        module.binding({**selection(), "uid": 1002}, API())
    for version in ("1.104.7", "1.105.0", "1.104.7-tfb3b9b296-g0651cc45e-dev", "1.104.7-tcbc74fe48"):
        api = API(); api.version = version
        with pytest.raises(module.Refusal) as error: module.binding(selection(), api)
        assert error.value.code == "native_version_unqualified"


@pytest.mark.parametrize("mutation", ["trailing", "duplicate", "noncanonical", "float", "oversize", "multi", "nonascii"])
def test_public_reader_requires_one_bounded_canonical_ascii_frame(mutation):
    module = payload(); value = request(); raw = module.canonical(value) + b"\n"
    if mutation == "trailing": raw += b"x"
    elif mutation == "duplicate": raw = raw.replace(b'"schema_version":1', b'"schema_version":1,"schema_version":1')
    elif mutation == "noncanonical": raw = b" " + raw
    elif mutation == "float": raw = raw.replace(b'"schema_version":1', b'"schema_version":1.0')
    elif mutation == "oversize": raw = b"x" * 4097
    elif mutation == "multi": raw += raw
    else: raw = b"\xff\n"
    reader, writer = os.pipe()
    try:
        os.write(writer, raw); os.close(writer)
        with pytest.raises(module.Refusal): module.read_request("inspect", reader)
    finally:
        os.close(reader)


def test_public_reader_accepts_original_finite_frame_and_ordinary_execution_refuses():
    module = payload(); value = request(); reader, writer = os.pipe()
    try:
        os.write(writer, module.canonical(value) + b"\n"); os.close(writer)
        assert module.read_request("inspect", reader) == value
    finally:
        os.close(reader)
    if os.geteuid() != 0:
        result = subprocess.run(["/bin/sh", str(SCRIPT), "inspect"], input=module.canonical(value) + b"\n",
                                env=constructed_env(), capture_output=True, timeout=5)
        assert result.returncode == 4 and json.loads(result.stdout)["code"] == "root_required"
        assert not result.stderr


def test_missing_original_etag_refuses_before_producing_ingress_proposal(monkeypatch):
    module = payload(); monkeypatch.setattr(module, "binding", lambda selected, api: {"dns": "fixture.example.ts.net"})
    for etag in (None, "", "bad\rheader"):
        with pytest.raises(module.Refusal) as error:
            module.inspect_plan("ingress", selection(), lambda method, endpoint: (200, etag, None))
        assert error.value.code == "missing_etag"


def test_partial_package_state_blocks_new_ingress_identity_without_repair(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    record = module.execute("inspect", value, source, None)
    record["state"] = "failed_partial"; record["dispatch"] = {"boot_id": module.boot_id()}; module.save(record)
    monkeypatch.setattr(module, "unit_state", lambda name: {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"})
    def dirty(): raise module.Refusal("package_intervention_required")
    monkeypatch.setattr(module, "package_health", dirty)
    with pytest.raises(module.Refusal) as error:
        module.execute("inspect", {**value, "kind": "ingress", "request_id": "44444444-4444-4444-8444-444444444444"}, source, None)
    assert error.value.code == "package_intervention_required"
    assert module.load(value["request_id"])["state"] == "failed_partial"


@pytest.mark.parametrize("config", ['APT::Get::Fix-Broken "true";\n', 'APT::Get::Fix-Missing "1";\n',
    'DPkg::Options:: "--force-confnew";\n', 'DPkg::Options:: "--force-all";\n'])
def test_apt_configuration_cannot_enable_implicit_repair_or_replace_local_conffiles(monkeypatch, config):
    module = payload()
    monkeypatch.setattr(module, "native", lambda argv, effect=False: "apt 2.8.4\n" if argv[-1] == "--version" else config)
    with pytest.raises(module.Refusal): module.apt_configuration()


@pytest.mark.parametrize("word", ["yes", "with", "on", "enable"])
def test_native_apt_truth_forms_and_force_variants_refuse(monkeypatch, word):
    module = payload()
    configs = [f'APT::Get::Fix-Broken "{word}";\n', f'Acquire::AllowDowngradeToInsecureRepositories "{word}";\n',
               'DPkg::Options:: "--force=all";\n', 'DPkg::Options:: "--force-depends";\n']
    for config in configs:
        monkeypatch.setattr(module, "native", lambda argv, effect=False: "apt 2.8.4\n" if argv[-1] == "--version" else config)
        with pytest.raises(module.Refusal): module.apt_configuration()


def test_native_apt_fixed_false_policies_cover_all_effect_doors():
    module = payload(); argv = module.apt_base()
    for key in ("APT::Get::Fix-Broken", "APT::Get::Fix-Missing", "APT::Get::AllowUnauthenticated",
                "Acquire::AllowInsecureRepositories", "Acquire::AllowWeakRepositories",
                "Acquire::AllowDowngradeToInsecureRepositories", "APT::Get::allow-Downgrades",
                "APT::Get::allow-Change-Held-Packages"):
        assert key + "=false" in argv


def test_manager_proposal_ignores_session_runtime_churn(monkeypatch):
    module, states, checked = manager_vendor_fixture(monkeypatch)
    first = copy.deepcopy(module.manager_plan(selection()))
    for state in states.values():
        state.update(ActiveState="active", SubState="running", InvocationID="f"*32, MainPID="1999", Result="success", ExecMainStatus="0")
    assert module.manager_plan(selection()) == first


@pytest.mark.parametrize("different_boot,load,active,allowed", [(False,"loaded","inactive",True),
    (False,"not-found","inactive",False), (True,"not-found","inactive",True),
    (True,"loaded","active",False)])
def test_unknown_prior_operation_can_only_advance_with_positive_inactivity(tmp_path, monkeypatch, different_boot, load, active, allowed):
    module = payload(); source,value,plan = root_fixture(module,tmp_path,monkeypatch)
    old = module.execute("inspect",value,source,None); old.update(state="unknown",dispatch={"boot_id": "3"*36 if different_boot else module.boot_id()}); module.save(old)
    monkeypatch.setattr(module,"unit_state",lambda name:{"LoadState":load,"ActiveState":active,"MainPID":"0"})
    new = {**value,"request_id":"55555555-5555-4555-8555-555555555555"}
    if allowed:
        assert module.execute("inspect",new,source,None)["state"] == "planned"
    else:
        with pytest.raises(module.Refusal): module.execute("inspect",new,source,None)
    assert module.load(value["request_id"])["state"] == "unknown"


@pytest.mark.parametrize("state,active,expected", [("running","active","running"),("completed","inactive","completed")])
def test_health_failure_preserves_execution_observation(tmp_path,monkeypatch,state,active,expected):
    module=payload(); source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    record=module.execute("inspect",value,source,None); record.update(state=state,invocation_id="1"*32,dispatch={"boot_id":module.boot_id()})
    monkeypatch.setattr(module,"unit_state",lambda name:{"LoadState":"loaded","ActiveState":active,"MainPID":"123" if active=="active" else "0","InvocationID":"1"*32})
    def dirty(): raise module.Refusal("package_intervention_required")
    monkeypatch.setattr(module,"package_health",dirty)
    result=module.observe(record,None)
    assert result["state"] == expected and result["package_health"] == ("in_progress" if expected=="running" else "unknown")


def admitted_worker_fixture(tmp_path,monkeypatch):
    module=payload(); source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    record=module.execute("inspect",value,source,None); record.update(state="dispatched",dispatch={"boot_id":module.boot_id()}); module.save(record)
    invocation="6"*32; monkeypatch.setenv("INVOCATION_ID",invocation)
    monkeypatch.setattr(module,"private_record",lambda rid,path:module.load(rid))
    monkeypatch.setattr(module,"unit_state",lambda name:{"InvocationID":invocation,"MainPID":str(os.getpid())})
    monkeypatch.setattr(module,"package_plan",lambda:plan["packages"])
    monkeypatch.setattr(module,"manager_plan",lambda selected:copy.deepcopy(plan["manager"]))
    return module,source,value,plan,invocation


def test_admitted_worker_binding_failure_is_terminal_without_effect(tmp_path,monkeypatch):
    module,source,value,plan,invocation=admitted_worker_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(module,"binding",lambda *args:{"changed":True})
    monkeypatch.setattr(module,"native",lambda *args,**kwargs:pytest.fail("unexpected effect"))
    module.private_worker(value["request_id"],module.artifact_path(value["artifact_sha256"]),None)
    assert module.load(value["request_id"])["state"] == "failed_partial"


def test_worker_internal_failure_keeps_hook_proof_and_prior_fields(tmp_path,monkeypatch):
    module,source,value,plan,invocation=admitted_worker_fixture(tmp_path,monkeypatch)
    plan["packages"]["actions"]=[{"package":"git","old":"-","new":"2","architecture":"amd64"}]
    plan["packages"]["seeds"]=["git"]
    # Recreate immutable proposal fixture before admission, never edit real approved state.
    record=module.load(value["request_id"]);record["proposal"]=copy.deepcopy(plan);record["digest"]=module.digest(plan);module.save(record)
    def native(argv,effect=False):
        current=module.load(value["request_id"]);current["hook_verified"]={"invocation_id":invocation,"proof":"retained"};current["retained_observation"]="original";module.save(current)
        raise ValueError("must not leak internal details")
    monkeypatch.setattr(module,"native",native)
    module.private_worker(value["request_id"],module.artifact_path(value["artifact_sha256"]),None)
    result=module.load(value["request_id"])
    assert result["state"]=="failed_partial" and result["code"]=="worker_internal_failure"
    assert result["hook_verified"]=={"invocation_id":invocation,"proof":"retained"} and result["retained_observation"]=="original"
    assert "must not leak" not in module.canonical(result).decode()


def test_worker_installs_only_versioned_seeds_and_observes_manager_transition(tmp_path,monkeypatch):
    module,source,value,plan,invocation=admitted_worker_fixture(tmp_path,monkeypatch)
    actions=[{"package":"git","old":"-","new":"2","architecture":"amd64"},
             {"package":"dependency","old":"-","new":"1","architecture":"all"}]
    plan["packages"].update(actions=actions,seeds=["git"])
    record=module.load(value["request_id"]);record["proposal"]=copy.deepcopy(plan);record["digest"]=module.digest(plan);module.save(record)
    states=iter([{"user":{"ActiveState":"activating"}},{"user":{"ActiveState":"activating"}},{"user":{"ActiveState":"active"}}])
    monkeypatch.setattr(module,"manager_runtime",lambda selected:next(states))
    monkeypatch.setattr(module.time,"sleep",lambda seconds:None)
    calls=[]
    def native(argv,effect=False):
        calls.append(argv)
        if argv[0]=="/usr/bin/apt-get":
            assert argv[argv.index("--")+1:]==["git=2"]
            current=module.load(value["request_id"]);current["hook_verified"]={"invocation_id":invocation,"actions_digest":module.digest(actions),"counts":{"unpack":2,"configure":2}};module.save(current)
        else: plan["manager"]["linger"]=True
        return 0
    monkeypatch.setattr(module,"native",native)
    module.private_worker(value["request_id"],module.artifact_path(value["artifact_sha256"]),None)
    assert module.load(value["request_id"])["state"]=="completed" and len(calls)==2


def test_held_installed_package_is_healthy_outside_transaction(monkeypatch):
    module=payload()
    monkeypatch.setattr(module,"native",lambda argv,effect=False:"" if argv[-1]=="--audit" else "hi  held-example 1 amd64\nii  git 2 amd64\n")
    assert "held-example" in module.package_health()


def test_valid_private_staging_remnant_is_preserved_not_a_foreign_receipt(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    remnant=module.STORE/".write-abcdefgh";remnant.write_bytes(b"partial");remnant.chmod(0o600)
    assert module.execute("inspect",value,source,None)["state"]=="planned"
    assert remnant.read_bytes()==b"partial"


def test_relative_source_refuses_before_artifact_read(tmp_path,monkeypatch):
    module=payload(); monkeypatch.setattr(module,"verify_artifact",lambda *args:pytest.fail("relative source read"))
    with pytest.raises(module.Refusal):module.execute("inspect",request(),Path("relative.sh"),None)


def test_large_proposal_refuses_before_durable_admission(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    plan["large"]="x"*(module.LIMIT-4096)
    with pytest.raises(module.Refusal):module.execute("inspect",value,source,None)
    assert not module.receipt_path(value["request_id"]).exists()


def test_ingress_status_separates_exact_config_from_context_continuity(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch,"ingress")
    plan["after"]={"TCP":{"443":{"HTTPS":True}}}
    record=module.execute("inspect",value,source,None);record["state"]="unknown"
    monkeypatch.setattr(module,"binding",lambda *args:{"different":"daemon"})
    result=module.observe(record,lambda *args:(200,"etag",plan["after"]))
    assert result["postcondition_observed"] is True and result["context_continuity_observed"] is False
    assert result["causality"]=="not_proved_by_status"


def test_manager_wait_is_bounded_and_never_repairs(monkeypatch):
    module=payload(); sleeps=[]; probes=[]
    config={"linger":True,"units":{"user":{"FragmentPath":"vendor"}}}
    monkeypatch.setattr(module,"manager_plan",lambda selected:copy.deepcopy(config))
    def runtime(selected):probes.append(True);return {"user":{"ActiveState":"activating"}}
    monkeypatch.setattr(module,"manager_runtime",runtime)
    monkeypatch.setattr(module.time,"sleep",sleeps.append)
    monkeypatch.setattr(module,"native",lambda *args,**kwargs:pytest.fail("wait cannot start or repair services"))
    with pytest.raises(module.Refusal) as error:module.wait_manager(selection(),config)
    assert error.value.code=="manager_postcondition_unproved" and len(probes)==module.MANAGER_POLLS
    assert len(sleeps)==module.MANAGER_POLLS-1


@pytest.mark.parametrize("matching",[False,True])
def test_unknown_ingress_allows_new_identity_only_after_exact_positive_observation(tmp_path,monkeypatch,matching):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch,"ingress")
    plan["after"]={"TCP":{"443":{"HTTPS":True}}}
    old=module.execute("inspect",value,source,None);old["state"]="unknown";module.save(old)
    new={**value,"request_id":"77777777-7777-4777-8777-777777777777"}
    def api(method,endpoint,*args):
        assert method=="GET"
        return 200,"etag",plan["after"] if matching else None
    if matching:assert module.execute("inspect",new,source,api)["state"]=="planned"
    else:
        with pytest.raises(module.Refusal):module.execute("inspect",new,source,api)
    assert module.load(value["request_id"])["state"]=="unknown"


def test_prepost_binding_refusal_is_terminal_and_never_posts(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch,"ingress")
    plan.update(etag="original",after={"TCP":{"443":{"HTTPS":True}}})
    record=module.execute("inspect",value,source,None)
    monkeypatch.setattr(module,"binding",lambda *args:{"changed":True})
    result=module.execute("apply",approval(value,record),source,lambda *args:pytest.fail("must not POST"))
    assert result["state"]=="failed_partial" and result["post_attempted"] is False


def test_index_refresh_uses_fixed_policy_overrides(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch,"index-refresh")
    record=module.execute("inspect",value,source,None);record.update(state="dispatched",dispatch={"boot_id":module.boot_id()});module.save(record)
    invocation="7"*32;monkeypatch.setenv("INVOCATION_ID",invocation)
    monkeypatch.setattr(module,"private_record",lambda rid,path:module.load(rid))
    monkeypatch.setattr(module,"unit_state",lambda name:{"InvocationID":invocation,"MainPID":str(os.getpid())})
    calls=[]
    monkeypatch.setattr(module,"native",lambda argv,effect=False:calls.append(argv) or 0)
    module.private_worker(value["request_id"],module.artifact_path(value["artifact_sha256"]),None)
    assert calls==[module.apt_base()+["update"]] and module.load(value["request_id"])["state"]=="completed"


@pytest.mark.parametrize("force",["--force-confdef","--force-confold","--force=confdef,confold"])
def test_native_apt_keeps_only_known_conffile_force_policy(monkeypatch,force):
    module=payload();config=f'DPkg::Options:: "{force}";\n'
    monkeypatch.setattr(module,"native",lambda argv,effect=False:"apt 2.8.4\n" if argv[-1]=="--version" else config)
    assert module.apt_configuration()["hooks"]==[]


@pytest.mark.parametrize("kind",["symlink","mode","unbounded_count","unknown_name"])
def test_staging_remnant_exception_is_narrow_and_preserves_every_file(tmp_path,monkeypatch,kind):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    name=".write-abcdefgh" if kind!="unknown_name" else ".write-arbitrary"
    path=module.STORE/name;path.write_bytes(b"partial");path.chmod(0o600)
    if kind=="symlink":
        path.unlink();path.symlink_to(source)
    elif kind=="mode":path.chmod(0o644)
    elif kind=="unbounded_count":
        for index in range(17):
            extra=module.STORE/(".write-"+str(index).zfill(8));extra.write_bytes(b"partial");extra.chmod(0o600)
    with pytest.raises((module.Refusal,OSError)):module.execute("inspect",value,source,None)
    assert path.exists() or path.is_symlink()


def test_inspect_and_status_accept_only_minimal_selection_apply_requires_full():
    module=payload();value=request(selection=minimal_selection())
    assert module.validate_request("inspect",value)==value
    status={key:item for key,item in value.items() if key!="kind"}
    assert module.validate_request("status",status)==status
    with pytest.raises(module.Refusal):module.validate_request("inspect",request(selection=selection()))
    with pytest.raises(module.Refusal):module.validate_request("apply",{**status,"digest":"c"*64,"decision":"approve"})
    assert module.validate_request("apply",{**status,"selection":selection(),"digest":"c"*64,"decision":"approve"})["selection"]==selection()


def test_new_minimal_inspection_retains_core_derived_full_selection(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    derived=[]
    monkeypatch.setattr(module,"derive_selection",lambda selected,api:derived.append(selected) or selection(),raising=False)
    value["selection"]=minimal_selection()
    record=module.execute("inspect",value,source,None)
    assert record["selection"]==selection() and derived==[minimal_selection()]
    assert module.load(value["request_id"])["selection"]==selection()


def test_original_inspect_and_lost_response_status_use_retained_context_only(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    record=module.execute("inspect",value,source,None)
    before=module.receipt_path(value["request_id"]).read_bytes()
    monkeypatch.setattr(module,"derive_selection",lambda *args:pytest.fail("must not rederive original context"),raising=False)
    monkeypatch.setattr(module,"inspect_plan",lambda *args:pytest.fail("must not inspect replacement plan"))
    value["selection"]=minimal_selection()
    assert module.execute("inspect",value,source,None)==record
    status={key:item for key,item in value.items() if key!="kind"}
    assert module.execute("status",status,source,None)["record"]==record
    assert module.receipt_path(value["request_id"]).read_bytes()==before


@pytest.mark.parametrize("change",[{"account":"other"},{"uid":1002},{"node_key":"nodekey:"+"b"*64}])
def test_retained_minimal_identity_mismatch_cannot_adopt_existing_uuid(tmp_path,monkeypatch,change):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    module.execute("inspect",value,source,None)
    value["selection"]={**minimal_selection(),**change}
    with pytest.raises(module.Refusal) as error:module.execute("inspect",value,source,None)
    assert error.value.code=="request_identity_conflict"


def test_core_derives_account_gid_home_and_current_profile_then_verifies_full_binding(monkeypatch):
    from types import SimpleNamespace
    module=payload();bound=[];calls=[]
    monkeypatch.setattr(module.pwd,"getpwnam",lambda name:SimpleNamespace(pw_name=name,pw_uid=1001,pw_gid=1002,pw_dir="/home/user",pw_shell="/bin/bash"))
    monkeypatch.setattr(module,"binding",lambda selected,api:bound.append(copy.deepcopy(selected)))
    def api(method,endpoint):
        calls.append((method,endpoint));return 200,None,{"ID":"current-profile"}
    derived=module.derive_selection(minimal_selection(),api)
    assert derived=={**selection(),"gid":1002,"profile_id":"current-profile"} and bound==[derived]
    assert calls==[("GET","profiles/current")]


@pytest.mark.parametrize("field,value",[("pw_uid",1002),("pw_name","other"),("pw_gid",0),
    ("pw_dir","/"),("pw_shell","/usr/sbin/nologin")])
def test_derived_account_must_match_ordinary_selected_identity(monkeypatch,field,value):
    from types import SimpleNamespace
    module=payload();account={"pw_name":"fixture","pw_uid":1001,"pw_gid":1001,"pw_dir":"/home/user","pw_shell":"/bin/bash"}
    account[field]=value
    monkeypatch.setattr(module.pwd,"getpwnam",lambda name:SimpleNamespace(**account))
    monkeypatch.setattr(module,"binding",lambda *args:pytest.fail("invalid derived account admitted"))
    with pytest.raises(module.Refusal):module.derive_selection(minimal_selection(),lambda *args:(200,None,{"ID":"current-profile"}))


def test_status_missing_original_receipt_never_derives_stages_or_launches(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    module.execute("inspect",value,source,None)
    before={path.name:path.read_bytes() for path in module.STORE.iterdir()}
    monkeypatch.setattr(module,"derive_selection",lambda *args:pytest.fail("status cannot derive context"))
    monkeypatch.setattr(module,"write_file",lambda *args,**kwargs:pytest.fail("status cannot stage or save"))
    monkeypatch.setattr(module,"native",lambda *args,**kwargs:pytest.fail("status missing receipt cannot launch"))
    status={key:item for key,item in value.items() if key!="kind"};status["request_id"]="88888888-8888-4888-8888-888888888888"
    with pytest.raises(module.Refusal) as error:module.execute("status",status,source,None)
    assert error.value.code=="original_receipt_absent"
    assert {path.name:path.read_bytes() for path in module.STORE.iterdir()}==before


def test_apply_still_rejects_replacement_full_derived_context(tmp_path,monkeypatch):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    record=module.execute("inspect",value,source,None)
    apply=approval(value,record);apply["selection"]={**record["selection"],"profile_id":"replacement-profile"}
    monkeypatch.setattr(module,"native",lambda *args,**kwargs:pytest.fail("replacement context effect"))
    with pytest.raises(module.Refusal) as error:module.execute("apply",apply,source,None)
    assert error.value.code=="request_identity_conflict"


@pytest.mark.parametrize("state,active,pid,dirty,allowed",[
    ("completed","inactive","0",False,True),
    ("failed_partial","inactive","0",False,True),
    ("unknown","inactive","0",False,False),
    ("completed","active","12",False,False),
    ("completed","inactive","0",True,False)])
def test_collected_terminal_worker_can_advance_but_uncertain_active_or_dirty_cannot(tmp_path,monkeypatch,state,active,pid,dirty,allowed):
    module=payload();source,value,plan=root_fixture(module,tmp_path,monkeypatch)
    old=module.execute("inspect",value,source,None)
    old.update(state=state,dispatch={"boot_id":module.boot_id()})
    if state in module.TERMINAL:old["native_returncode"]=0
    module.save(old);original=module.receipt_path(value["request_id"]).read_bytes()
    monkeypatch.setattr(module,"unit_state",lambda name:{"LoadState":"not-found","ActiveState":active,"MainPID":pid})
    if dirty:
        def health():raise module.Refusal("package_intervention_required")
        monkeypatch.setattr(module,"package_health",health)
    monkeypatch.setattr(module,"inspect_plan",lambda kind,selected,api:{**copy.deepcopy(plan),"kind":kind})
    new={**value,"kind":"ingress","request_id":"99999999-9999-4999-8999-999999999999"}
    if allowed:
        record=module.execute("inspect",new,source,None)
        assert record["state"]=="planned" and record["proposal"]["kind"]=="ingress"
    else:
        with pytest.raises(module.Refusal) as error:module.execute("inspect",new,source,None)
        assert error.value.code==("package_intervention_required" if dirty else "prior_operation_not_inactive")
    assert module.receipt_path(value["request_id"]).read_bytes()==original


# The team account, claudlobby (16-02 C2). Fixtures stand in for native root
# facts only: account databases, root files, units, apt and the daemon.

NODE_KEY = "nodekey:" + "a" * 64
DNS = "fixture.example.ts.net"


class Daemon:
    """The system tailscaled's local API, read-only, as the leaf sees it."""
    identity = {"pid": 812, "start": "4242", "binary_sha256": "f" * 64,
                "binary": [64768, 131, 1700000000000000000, 30000000], "socket": [25, 1020, 1]}

    def __init__(self, version, serve=None):
        self.version, self.serve, self.reads = version, serve, []

    def __call__(self, method, endpoint, body=None, etag=None):
        assert method == "GET", "inspection never writes Serve"
        self.reads.append(endpoint)
        if endpoint == "status":
            return 200, None, {"Version": self.version, "BackendState": "Running", "TUN": True,
                               "Self": {"PublicKey": NODE_KEY, "DNSName": DNS + "."}, "CertDomains": [DNS]}
        if endpoint == "profiles/current":
            return 200, None, {"ID": "p001", "Name": "fixture"}
        assert endpoint == "serve-config"
        return 200, "fixture-etag", copy.deepcopy(self.serve)


NATIVE = {
    ("/usr/bin/systemctl", "--version"): "systemd 255 (255.4-1ubuntu8)\n",
    ("/usr/bin/apt-get", "--version"): "apt 2.8.3 (amd64)\n",
    ("/usr/bin/apt-config", "dump"): 'APT::Architecture "amd64";\nDPkg::Post-Invoke:: "/usr/bin/fixture-hook";\n',
    ("/usr/bin/dpkg", "--audit"): "",
    ("/usr/bin/dpkg", "--print-architecture"): "amd64\n",
    ("/usr/bin/dpkg", "--print-foreign-architectures"): "",
    ("/usr/bin/apt-mark", "showhold"): "",
}
INSTALLED = ("ii  git 1:2.43.0-1ubuntu7 amd64\nii  python3-venv 3.12.3-0ubuntu1 amd64\n"
             "ii  ca-certificates 20240203 all\nii  tmux 3.4-1build1 amd64\n")
SIMULATED = ("Inst libjq1 (1.7.1-3build1 Ubuntu:24.04/noble [amd64])\n"
             "Inst jq (1.7.1-3build1 Ubuntu:24.04/noble [amd64])\n")


def native_root(module, monkeypatch, accounts=None, units=None, effects=None):
    """The real binding, manager, package and proposal code, as root on a
    fixture Ubuntu 24.04. accounts maps a name to its pwd fields; absent names
    raise KeyError as NSS does. units overrides unit states by name. effects,
    when given, answers every effect and systemd-run. Returns every argv."""
    import stat
    from types import SimpleNamespace
    accounts = {"fixture": {"pw_uid": 1001, "pw_gid": 1001, "pw_dir": "/home/user"}} if accounts is None else accounts

    def entry(name):
        fields = {"pw_name": name, "pw_shell": "/bin/bash", "pw_passwd": "x", **accounts[name]}
        return SimpleNamespace(**fields)
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(module.pwd, "getpwnam", entry)
    monkeypatch.setattr(module, "trusted_parents", lambda path: None)
    monkeypatch.setattr(module.Path, "read_text", lambda path: 'ID=ubuntu\nVERSION_ID="24.04"\n'
                        if str(path) == "/etc/os-release" else pytest.fail(f"unexpected read {path}"))
    monkeypatch.setattr(module.Path, "read_bytes", lambda path: b"synthetic-machine"
                        if str(path) == "/etc/machine-id" else pytest.fail(f"unexpected read {path}"))
    is_dir, lstat = module.Path.is_dir, module.Path.lstat
    monkeypatch.setattr(module.Path, "is_dir", lambda path: str(path) == "/run/systemd/system" or is_dir(path))
    monkeypatch.setattr(module.Path, "lstat", lambda path: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0, st_nlink=1)
                        if str(path).startswith("/usr/lib/systemd/system/") else lstat(path))
    states = {} if units is None else units

    def unit_state(name):
        template = name.split("@", 1)[0] + "@.service"
        return {"LoadState": "loaded", "FragmentPath": "/usr/lib/systemd/system/" + template, "DropInPaths": "",
                "ActiveState": "inactive", "SubState": "dead", "MainPID": "0", **states.get(name, {})}
    monkeypatch.setattr(module, "unit_state", unit_state)
    calls = []

    def native(argv, effect=False):
        calls.append(argv)
        if effect or argv[0] == "/usr/bin/systemd-run":
            assert effects, f"unexpected effect {argv}"
            return effects(argv)
        if tuple(argv) in NATIVE:
            return NATIVE[tuple(argv)]
        if argv[0] == "/usr/bin/dpkg-query":
            return INSTALLED
        assert argv[:3] == ["/usr/bin/apt-get", "--no-install-recommends", "--no-remove"]
        assert argv[-4:] == ["--simulate", "install", "--", "jq"]
        return SIMULATED
    monkeypatch.setattr(module, "native", native)
    return calls


GOLDEN = json.loads((ROOT / "tests/fixtures/linux-system-other-account-golden.json").read_text())


@pytest.mark.parametrize("kind", ["system", "index-refresh", "ingress"])
def test_other_accounts_proposals_match_the_golden_copy(monkeypatch, kind):
    """Byte for byte what the leaf proposed for them before the team account."""
    module = payload()
    native_root(module, monkeypatch)
    api = Daemon(module.NATIVE_VERSION)
    derived = module.derive_selection(minimal_selection(), api)
    assert module.canonical(derived).decode() == GOLDEN["selection"]
    assert module.canonical(module.inspect_plan(kind, derived, api)).decode() == GOLDEN["proposals"][kind]
    assert api.reads.count("serve-config") == (kind == "ingress")


RID = "11111111-1111-4111-8111-111111111111"
LATER = "55555555-5555-4555-8555-555555555555"
TEAM_HOME = "/home/claudlobby"
USERADD = ["/usr/sbin/useradd", "--uid", "1002", "--user-group", "--create-home", "--home-dir", TEAM_HOME,
           "--shell", "/bin/bash", "--comment", "Claudlobby team", "--expiredate", "", "--inactive", "-1",
           "--key", "HOME_MODE=0700", "--key", "SUB_UID_COUNT=0", "--key", "SUB_GID_COUNT=0",
           "--key", "PASS_MAX_DAYS=-1", "--", "claudlobby"]
# The record keys the website's decoder admits (lib/linux-system-transport.ts).
RECORD_KEYS = {"schema_version", "request_id", "selection", "artifact_sha256", "proposal", "digest", "created_at",
               "expires_at", "state", "effect", "dispatch", "submission", "response_code", "after_etag", "after_digest",
               "post_attempted", "invocation_id", "hook_verified", "manager_observation", "native_returncode", "code"}
ROOT_FILES = ("passwd", "group", "shadow", "gshadow", "subuid", "subgid", "nsswitch", "sudoers")
# Ubuntu 24.04's own /etc/sudoers, cut to the lines that matter here.
UBUNTU_SUDOERS = ("#\n# This file MUST be edited with the visudo command as root.\n#\nDefaults\tenv_reset\n"
                  "Defaults\tuse_pty\n# (#1011624)\n#Defaults:%sudo env_keep += \"EDITOR\"\n"
                  "root\tALL=(ALL:ALL) ALL\n%admin ALL=(ALL) ALL\n%sudo\tALL=(ALL:ALL) ALL\n\n"
                  "# See sudoers(5) for more information on \"@include\" directives:\n\n@includedir /etc/sudoers.d\n")


def node(kind, mode, uid, size=0):
    from types import SimpleNamespace
    return SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_size=size, st_nlink=1)


def directory_node(mode=0o700, uid=1002):
    import stat
    return node(stat.S_IFDIR, mode, uid)


class Computer:
    """A fixture Ubuntu computer for the team account. NSS answers from the
    same entries its /etc files are written from; the useradd model adds the
    team as Ubuntu does with the leaf's fixed arguments."""

    def __init__(self, module, monkeypatch, tmp_path):
        import hashlib
        from types import SimpleNamespace
        self.module, self.monkeypatch = module, monkeypatch
        self.users = {"root": {"pw_uid": 0, "pw_gid": 0, "pw_dir": "/root"},
                      "fixture": {"pw_uid": 1001, "pw_gid": 1001, "pw_dir": "/home/user"},
                      "nobody": {"pw_uid": 65534, "pw_gid": 65534, "pw_dir": "/nonexistent",
                                 "pw_shell": "/usr/sbin/nologin"}}
        self.groups = {"root": {"gid": 0, "mem": []}, "sudo": {"gid": 27, "mem": ["fixture"]},
                       "fixture": {"gid": 1001, "mem": []}, "nogroup": {"gid": 65534, "mem": []}}
        self.shadow = {"root": "root:*:20000:0:99999:7:::", "fixture": "fixture:$6$salt$hash:20000:0:99999:7:::"}
        self.gshadow = {"root": "root:*::", "sudo": "sudo:*::fixture", "fixture": "fixture:!::"}
        self.extra = {"nsswitch": "passwd:         files systemd\ngroup:          files systemd\nshadow:         files\n",
                      "subuid": "fixture:100000:65536\n", "subgid": "fixture:100000:65536\n",
                      "sudoers": UBUNTU_SUDOERS}
        self.sudoers_d = {"README": "# Files here are read unless a name ends in ~ or holds a dot.\n",
                          "90-cloud-init-users": "# User rules for fixture\nfixture ALL=(ALL) NOPASSWD:ALL\n"}
        self.passwd_tail = ""           # raw lines NSS does not answer for
        self.nss_uids, self.nss_gids = set(), set()  # ids NSS answers for beyond the files
        self.nodes, self.units, self.effects = {}, {}, []
        self.useradd_rc, self.apt_rc, self.made_shell = 0, 0, "/bin/bash"
        self.daemon = Daemon(module.NATIVE_VERSION)
        # The store and staged source first: native_root fences pathlib reads.
        store = tmp_path / "store"
        store.mkdir(mode=0o700)
        monkeypatch.setattr(module, "STORE", store)
        self.source = tmp_path / "incoming.sh"
        self.source.write_bytes(SCRIPT.read_bytes())
        self.source.chmod(0o500)
        self.sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.files = tmp_path / "root"
        (self.files / "proc").mkdir(parents=True)
        self.process(1, uid=0)
        self.process(4242, uid=1001)
        paths = {name: str(self.files / name) for name in ROOT_FILES + ("sudoers.d",)}
        monkeypatch.setattr(module, "PATHS", {**module.PATHS, **paths, "proc": str(self.files / "proc")})
        monkeypatch.setattr(module, "OWNER", os.geteuid())
        native_root(module, monkeypatch, accounts=self.users, units=self.units, effects=self.effect)

        def user(name):
            return SimpleNamespace(**{"pw_name": name, "pw_shell": "/bin/bash", "pw_passwd": "x", **self.users[name]})

        def group(name):
            entry = self.groups[name]
            return SimpleNamespace(gr_name=name, gr_gid=entry["gid"], gr_mem=list(entry["mem"]),
                                   gr_passwd=entry.get("passwd", "x"))

        def by_uid(uid):
            for name, entry in self.users.items():
                if entry["pw_uid"] == uid:
                    return user(name)
            if uid in self.nss_uids:
                return SimpleNamespace(pw_name="dynamic", pw_uid=uid, pw_gid=uid, pw_dir="/", pw_shell="/bin/false")
            raise KeyError(uid)

        def by_gid(gid):
            for name, entry in self.groups.items():
                if entry["gid"] == gid:
                    return group(name)
            if gid in self.nss_gids:
                return SimpleNamespace(gr_name="dynamic", gr_gid=gid, gr_mem=[], gr_passwd="x")
            raise KeyError(gid)
        monkeypatch.setattr(module.pwd, "getpwnam", user)
        monkeypatch.setattr(module.pwd, "getpwuid", by_uid)
        monkeypatch.setattr(module.pwd, "getpwall", lambda: [user(name) for name in self.users])
        monkeypatch.setattr(module.grp, "getgrnam", group)
        monkeypatch.setattr(module.grp, "getgrgid", by_gid)
        monkeypatch.setattr(module.grp, "getgrall", lambda: [group(name) for name in self.groups])
        monkeypatch.setattr(module.os, "getgrouplist", lambda name, gid: [gid] + [
            entry["gid"] for entry in self.groups.values() if name in entry["mem"]])
        monkeypatch.setattr(module, "lstat", lambda path: self.nodes.get(str(path)))
        monkeypatch.setattr(module, "boot_id", lambda: "22222222-2222-4222-8222-222222222222")
        # The manager wait has its own tests; here lingering is an effect seen.
        monkeypatch.setattr(module, "wait_manager", lambda selected, expected: {
            "configuration": expected, "runtime": {}, "ordinary_user_bus": "unproved"})
        self.write()

    def write(self):
        passwd = "".join(f"{name}:{entry.get('pw_passwd', 'x')}:{entry['pw_uid']}:{entry['pw_gid']}::"
                         f"{entry['pw_dir']}:{entry.get('pw_shell', '/bin/bash')}\n" for name, entry in self.users.items())
        group = "".join(f"{name}:{entry.get('passwd', 'x')}:{entry['gid']}:{','.join(entry['mem'])}\n"
                        for name, entry in self.groups.items())
        contents = {"passwd": passwd + self.passwd_tail, "group": group,
                    "shadow": "".join(line + "\n" for line in self.shadow.values()),
                    "gshadow": "".join(line + "\n" for line in self.gshadow.values()), **self.extra}
        for name in ROOT_FILES:
            path = self.files / name
            if name in contents:
                path.write_text(contents[name])
            elif path.exists():
                path.unlink()
        folder = self.files / "sudoers.d"
        folder.mkdir(exist_ok=True)
        for path in folder.iterdir():
            path.unlink()
        for name, text in self.sudoers_d.items():
            (folder / name).write_text(text)

    def process(self, pid, uid=None, gid=None):
        uid, gid = (uid, uid if gid is None else gid) if uid is not None else (0, gid)
        (self.files / "proc" / str(pid)).mkdir(exist_ok=True)
        (self.files / "proc" / str(pid) / "status").write_bytes(
            b"Name:\tfixture\nUid:\t%d\t%d\t%d\t%d\nGid:\t%d\t%d\t%d\t%d\n" % ((uid,) * 4 + (gid,) * 4))

    def add_team(self, uid, shell="/bin/bash"):
        """What useradd makes with the leaf's arguments on Ubuntu 24.04."""
        self.users["claudlobby"] = {"pw_uid": uid, "pw_gid": uid, "pw_dir": TEAM_HOME, "pw_shell": shell}
        self.groups["claudlobby"] = {"gid": uid, "mem": []}
        self.shadow["claudlobby"] = "claudlobby:!:20000:0::7:::"
        self.gshadow["claudlobby"] = "claudlobby:!::"
        self.nodes[TEAM_HOME] = directory_node(uid=uid)
        self.write()

    def effect(self, argv):
        self.effects.append(argv)
        if argv[0] == "/usr/bin/systemd-run":
            return ""
        if argv[0] == "/usr/sbin/useradd":
            if self.useradd_rc == 0:
                self.add_team(int(argv[argv.index("--uid") + 1]), self.made_shell)
            return self.useradd_rc
        if argv[:1] == ["/usr/bin/apt-get"] and argv[-1] != "update":
            request_id = next(a for a in argv if a.startswith("DPkg::Pre-Install-Pkgs::=")).rsplit(" ", 1)[1]
            current = self.module.load(request_id)
            actions = current["proposal"]["packages"]["actions"]
            current["hook_verified"] = {"invocation_id": current["invocation_id"], "actions_digest": self.module.digest(actions),
                                        "counts": {"unpack": len(actions), "configure": len(actions)}}
            self.module.save(current)
            return self.apt_rc
        assert argv[0] in ("/usr/bin/apt-get", "/usr/bin/loginctl"), argv
        return 0

    def done(self):
        """The effects that change the computer, in order."""
        return [argv for argv in self.effects if argv[0] != "/usr/bin/systemd-run"]

    def inspect(self, kind="system", uid=1002, request_id=RID, account="claudlobby"):
        value = {"schema_version": 1, "request_id": request_id, "artifact_sha256": self.sha, "kind": kind,
                 "selection": {"account": account, "uid": uid, "node_key": NODE_KEY}}
        return self.module.execute("inspect", value, self.source, self.daemon)

    def apply(self, record):
        value = {"schema_version": 1, "request_id": record["request_id"], "artifact_sha256": self.sha,
                 "selection": record["selection"], "digest": record["digest"], "decision": "approve"}
        return self.module.execute("apply", value, self.source, self.daemon)

    def work(self, request_id=RID):
        invocation = "6" * 32
        self.monkeypatch.setenv("INVOCATION_ID", invocation)
        unit = self.module.unit_name(request_id)
        self.units[unit] = {"InvocationID": invocation, "MainPID": str(os.getpid()), "ActiveState": "active"}
        self.module.private_worker(request_id, self.module.artifact_path(self.sha), self.daemon)
        self.units[unit] = {"InvocationID": "", "MainPID": "0", "ActiveState": "inactive"}
        return self.module.load(request_id)

    def run(self, request_id=RID, uid=1002):
        record = self.inspect(uid=uid, request_id=request_id)
        assert self.apply(record)["submission"] == "exec_acknowledged"
        return self.work(request_id)


@pytest.fixture
def computer(tmp_path, monkeypatch):
    return Computer(payload(), monkeypatch, tmp_path)


def test_an_absent_team_account_gets_a_create_proposal(computer):
    module = computer.module
    assert module.team_state() == "absent"
    record = computer.inspect()
    team = {"account": "claudlobby", "uid": 1002, "gid": 1002, "home": TEAM_HOME, "node_key": NODE_KEY, "profile_id": "p001"}
    assert record["selection"] == team and record["proposal"]["binding"]["selection"] == team
    proposal = record["proposal"]
    assert set(proposal) == {"kind", "binding", "packages", "manager", "disclosure", "login"}
    assert list(proposal["login"]) == ["account", "uid", "gid", "home", "shell", "state"]
    assert proposal["login"] == {"account": "claudlobby", "uid": 1002, "gid": 1002, "home": TEAM_HOME,
                                 "shell": "/bin/bash", "state": "create"}
    assert proposal["disclosure"] == module.DISCLOSURE + " " + module.TEAM_DISCLOSURE
    assert "Setup never removes this account." in proposal["disclosure"]
    assert "serve-config" in computer.daemon.reads and computer.effects == []
    # Only the team's system proposal carries the block.
    refresh = computer.inspect(kind="index-refresh", request_id=LATER)["proposal"]
    assert "login" not in refresh and refresh["binding"]["selection"] == team
    with pytest.raises(module.Refusal) as error:
        computer.inspect(kind="ingress", request_id="66666666-6666-4666-8666-666666666666")
    assert error.value.code == "account_changed"


def test_routes_on_other_ports_and_local_account_sources_are_admitted(computer):
    computer.daemon.serve = {"TCP": {"8443": {"HTTPS": True}},
                             "Web": {"other.example.ts.net:8443": {"Handlers": {"/": {"Text": "other"}}}}}
    # glibc: a name ends at a colon or a space, a line starting with # names
    # no database, and a later line replaces an earlier one; an action on
    # another database does not touch accounts.
    computer.extra["nsswitch"] = ("# local\npasswd: files\ngroup:files\n\tgroup files systemd\n"
                                  "hosts: files mdns4_minimal [NOTFOUND=return] dns\n")
    computer.extra["subuid"] = "fixture:100000:65536\nmalformed\n"
    computer.write()
    assert computer.inspect()["proposal"]["login"]["state"] == "create"


UBUNTU_NSSWITCH = ("passwd:         files systemd\ngroup:          files systemd\nshadow:         files systemd\n"
                   "gshadow:        files systemd\n\nhosts:          files mdns4_minimal [NOTFOUND=return] dns\n"
                   "networks:       files\n\nprotocols:      db files\nservices:       db files\nethers:         db files\n"
                   "rpc:            db files\n\nnetgroup:       nis\n")


@pytest.mark.parametrize("present", [False, True])
def test_ubuntu_s_own_nsswitch_is_admitted(computer, present):
    if present:
        computer.add_team(1500)
    computer.extra["nsswitch"] = UBUNTU_NSSWITCH
    computer.write()
    assert computer.inspect(uid=1500 if present else 1002)["proposal"]["login"]["state"] == (
        "present" if present else "create")


@pytest.mark.parametrize("kind", ["link", "folder"])
def test_a_link_or_folder_in_sudoers_d_refuses_before_any_effect(computer, kind):
    # Stricter than sudo, which follows a link and skips a folder. A link is
    # never opened (main answers its OSError as inspection_unavailable).
    computer.write()
    entry = computer.files / "sudoers.d" / "team"
    entry.symlink_to(computer.files / "sudoers") if kind == "link" else entry.mkdir()
    with pytest.raises((computer.module.Refusal, OSError)) as error:
        computer.inspect()
    assert kind == "link" and isinstance(error.value, OSError) or error.value.code == "unsafe_file"
    assert not computer.module.receipt_path(RID).exists() and computer.effects == []


def test_the_uid_is_the_next_one_by_the_rule_over_the_raw_files(computer):
    # A line NSS does not answer for still counts, as in the probe.
    computer.passwd_tail = "#old:x:2500:2500\n"
    computer.write()
    with pytest.raises(computer.module.Refusal) as error:
        computer.inspect()
    assert error.value.code == "team_uid_changed"
    assert computer.inspect(uid=2501)["proposal"]["login"]["uid"] == 2501


RULE = json.loads((ROOT / "tests/fixtures/next-uid-rule.json").read_text())


@pytest.mark.parametrize("case", RULE["cases"], ids=[case["case"] for case in RULE["cases"]])
def test_the_leaf_computes_the_probes_next_uid_rule(case):
    """The table tests/test_host_readiness_probe.py runs against the probe."""
    module = payload()
    passwd, group = (b"".join(text.encode("latin-1") * count for text, count in case[name]) for name in ("passwd", "group"))
    if case["logins_null"]:
        with pytest.raises(module.Refusal) as error:
            module.next_uid(passwd, group)
        assert error.value.code == "team_uid_changed"
    else:
        assert module.next_uid(passwd, group) == case["next_uid"]


def _set(target, **values):
    return lambda c: getattr(c, target).update(values)


PLAN_REFUSALS = {
    "group-left": ("team_leftovers", lambda c: c.groups.update(claudlobby={"gid": 900, "mem": []})),
    "home-left": ("team_home_exists", lambda c: c.nodes.update({TEAM_HOME: directory_node(0o755, 0)})),
    "home-link": ("team_home_exists", lambda c: c.nodes.update({TEAM_HOME: node(0o120000, 0o777, 0)})),
    "uid-taken-in-files": ("team_uid_changed", lambda c: c.users.update(
        late={"pw_uid": 1002, "pw_gid": 1001, "pw_dir": "/home/alice"})),
    "uid-taken-outside-files": ("team_uid_changed", lambda c: c.nss_uids.add(1002)),
    "gid-taken-outside-files": ("team_uid_changed", lambda c: c.nss_gids.add(1002)),
    "no-free-uid": ("team_uid_changed", lambda c: c.groups.update(top={"gid": 59999, "mem": []})),
    "files-over-512-lines": ("team_uid_changed", lambda c: setattr(c, "passwd_tail", "\n" * 600)),
    "subuid-names-it": ("team_leftovers", lambda c: c.extra.update(subuid="claudlobby:165536:65536\n")),
    "subgid-names-its-id": ("team_leftovers", lambda c: c.extra.update(subgid="1002:165536:65536\n")),
    "subuid-range-holds-it": ("team_leftovers", lambda c: c.extra.update(subuid="fixture:1000:5000\n")),
    "mail-spool-left": ("team_leftovers", lambda c: c.nodes.update({"/var/mail/claudlobby": node(0o100000, 0o660, 0)})),
    "process-uid": ("team_uid_unavailable", lambda c: c.process(5555, uid=1002, gid=1001)),
    "process-gid": ("team_uid_unavailable", lambda c: c.process(5555, uid=1001, gid=1002)),
    "user-manager-active": ("team_uid_unavailable", lambda c: c.units.update({"user@1002.service": {"ActiveState": "active"}})),
    "user-manager-failed": ("team_uid_unavailable", lambda c: c.units.update({"user@1002.service": {"ActiveState": "failed"}})),
    "runtime-dir": ("team_uid_unavailable", lambda c: c.nodes.update({"/run/user/1002": directory_node()})),
    "sss": ("team_directory_service", _set("extra", nsswitch="passwd: files sss\ngroup: files systemd\n")),
    "ldap-group": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files ldap\n")),
    "compat": ("team_directory_service", _set("extra", nsswitch="passwd: compat\ngroup: compat\n")),
    "action": ("team_directory_service", _set("extra", nsswitch="passwd: files [NOTFOUND=return]\ngroup: files\n")),
    "passwd-twice": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files\npasswd: winbind\n")),
    "no-group-line": ("team_directory_service", _set("extra", nsswitch="passwd: files\n")),
    "no-nsswitch": ("team_directory_service", lambda c: c.extra.pop("nsswitch")),
    # Read as glibc reads it: these once passed as files only.
    "no-colon": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files\npasswd sss\n")),
    "later-line-wins": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files\n  group:ldap\n")),
    "bare-name": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files\ngroup\n")),
    "no-sources": ("team_directory_service", _set("extra", nsswitch="passwd:\npasswd: files\ngroup: files\n")),
    "other-name": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files\npasswd_compat: nis\n")),
    "overridden-action": ("team_directory_service", _set("extra", nsswitch=(
        "passwd: files\ngroup: files [!UNAVAIL=return]\ngroup: files\n"))),
    "gshadow-left": ("team_leftovers", _set("gshadow", claudlobby="claudlobby:!::")),
    "sudo-rule-left": ("team_leftovers", _set("sudoers_d", team="claudlobby ALL=(ALL) NOPASSWD:ALL\n")),
    "sudo-include-elsewhere": ("team_leftovers", _set("extra", sudoers=UBUNTU_SUDOERS + "@include /etc/sudoers.local\n")),
    # To sudo, # before digits is a uid, not a comment.
    "sudo-uid-then-name": ("team_leftovers", _set("sudoers_d", team="#1001,claudlobby ALL=(ALL) NOPASSWD: ALL\n")),
    # sudo joins a quoted word across a continued line: the user is claudlobby.
    "sudo-quoted-continued": ("team_leftovers", _set("sudoers_d", team='"claud\\\n  lobby" ALL=(ALL) NOPASSWD: ALL\n')),
    # glibc 2.39 has no inline comment: this line tries a module "#", then sss.
    "inline-hash-source": ("team_directory_service", _set("extra", nsswitch="passwd: files # sss\ngroup: files\n")),
    # sudo carries a continuation from one file into the next.
    "sudo-continued-across-files": ("team_leftovers", _set("sudoers_d", a='"claud\\\n', b='lobby" ALL=(ALL) NOPASSWD: ALL\n')),
    "sudo-quoted-continued-crlf": ("team_leftovers", _set("sudoers_d", team='"claud\\ \t\r\n\tlobby" ALL=(ALL) ALL\r\n')),
    "gshadow-oversize": ("team_leftovers", _set("gshadow", big="big:!::" + "x" * 262144)),
    "initgroups-ldap": ("team_directory_service", _set("extra", nsswitch="passwd: files\ngroup: files\ninitgroups: ldap\n")),
    "443-served": ("occupied_443", lambda c: setattr(c.daemon, "serve", {"TCP": {"443": {"HTTPS": True}}})),
    "443-routed": ("occupied_443", lambda c: setattr(c.daemon, "serve", {"Web": {
        "other.example.ts.net:443": {"Handlers": {"/": {"Proxy": "unix:/home/alice/owner.sock"}}}}})),
}


@pytest.mark.parametrize("name", sorted(PLAN_REFUSALS))
def test_each_team_plan_condition_refuses_on_its_own(computer, name):
    code, change = PLAN_REFUSALS[name]
    change(computer)
    computer.write()
    with pytest.raises(computer.module.Refusal) as error:
        computer.inspect()
    assert error.value.code == code
    assert not computer.module.receipt_path(RID).exists() and computer.effects == []


@pytest.mark.parametrize("shadow", ["claudlobby:!:20000:0::7:::", "claudlobby:*:20000:0:-1:7:-1:-1:",
                                    "claudlobby:!*:20000:0::7:::", "claudlobby:!::0::7:::", "claudlobby:!:010:0::7:::"])
def test_a_conforming_account_is_adopted_without_useradd(computer, shadow):
    computer.add_team(1500)
    computer.shadow["claudlobby"] = shadow
    computer.nodes["/var/mail/claudlobby"] = node(0o100000, 0o660, 1500)
    computer.write()
    assert computer.module.team_state() == "conforming"
    record = computer.inspect(uid=1500)
    assert record["proposal"]["login"] == {"account": "claudlobby", "uid": 1500, "gid": 1500, "home": TEAM_HOME,
                                           "shell": "/bin/bash", "state": "present"}
    assert record["selection"]["gid"] == 1500 and record["selection"]["home"] == TEAM_HOME
    assert computer.apply(record)["submission"] == "exec_acknowledged"
    final = computer.work()
    assert final["state"] == "completed" and set(final) <= RECORD_KEYS
    assert [argv[0] for argv in computer.done()] == ["/usr/bin/apt-get", "/usr/bin/loginctl"]


@pytest.mark.parametrize("gshadow", [None, "claudlobby:::", "claudlobby:*::"])
def test_a_group_with_no_gshadow_line_or_a_locked_one_conforms(computer, gshadow):
    # No line: newgrp falls back to the /etc/group field, x here, which no
    # password matches; an empty field admits no one who is not a member.
    computer.add_team(1500)
    if gshadow is None:
        computer.gshadow.pop("claudlobby")
    else:
        computer.gshadow["claudlobby"] = gshadow
    computer.write()
    assert computer.module.team_state() == "conforming"
    assert computer.inspect(uid=1500)["proposal"]["login"]["state"] == "present"


SUDO_ADMITTED = {
    "commented-out": "# rules for the team live elsewhere\n#alice ALL=(ALL) ALL\n",
    "another-word": "claudlobby_old ALL=(ALL) ALL\n",
    "include-here": "#includedir /etc/sudoers.d\n@includedir   /etc/sudoers.d  \n#includes no other file\n",
}


@pytest.mark.parametrize("present", [False, True])
def test_sudo_rules_that_sudo_never_reads_for_the_team_are_admitted(computer, present):
    for name, text in SUDO_ADMITTED.items():
        computer.sudoers_d[name.replace("-", "")] = text
    # sudo skips these names in sudoers.d, so a rule in them grants nothing.
    for name in ("team~", "team.disabled", "90-team.dpkg-old"):
        computer.sudoers_d[name] = "claudlobby ALL=(ALL) NOPASSWD:ALL\n@include /tmp/elsewhere\n"
    computer.sudoers_d.update({f"rule{n:02d}": "# none\n" for n in range(59)})  # 64 read, the bound
    if present:
        computer.add_team(1500)
    computer.write()
    assert computer.module.team_state() == ("conforming" if present else "absent")
    login = computer.inspect(uid=1500 if present else 1002)["proposal"]["login"]
    assert login["state"] == ("present" if present else "create")


def _team(uid=1500, **fields):
    def change(c):
        c.add_team(uid)
        c.users["claudlobby"].update(fields)
    return change


def _then(first, second):
    return lambda c: (first(c), second(c))


POLICY_REFUSALS = {
    "uid-below-1000": ("team_account_uid", 999, _team(999)),
    "uid-above-59999": ("team_account_uid", 60001, _team(60001)),
    "uid-shared": ("team_account_uid", 1500, _then(_team(), _set(
        "users", alias={"pw_uid": 1500, "pw_gid": 1001, "pw_dir": "/home/example"}))),
    "gid-differs": ("team_account_group", 1500, _team(pw_gid=1001)),
    "group-member": ("team_account_group", 1500, _then(_team(), lambda c: c.groups["claudlobby"]["mem"].append("fixture"))),
    "other-group": ("team_account_group", 1500, _then(_team(), lambda c: c.groups["sudo"]["mem"].append("claudlobby"))),
    "shared-primary-group": ("team_account_group", 1500, _then(_team(), _set(
        "users", mate={"pw_uid": 1600, "pw_gid": 1500, "pw_dir": "/home/olduser"}))),
    "group-renamed": ("team_account_group", 1500, _then(_team(), lambda c: c.groups.update(
        renamed=c.groups.pop("claudlobby")))),
    "shell": ("team_account_shell", 1500, _team(pw_shell="/bin/sh")),
    "home-elsewhere": ("team_account_home", 1500, _team(pw_dir="/home/example")),
    "home-group-open": ("team_account_home", 1500, _then(_team(), lambda c: c.nodes.update({TEAM_HOME: directory_node(0o750, 1500)}))),
    "home-foreign": ("team_account_home", 1500, _then(_team(), lambda c: c.nodes.update({TEAM_HOME: directory_node(0o700, 1001)}))),
    "home-link": ("team_account_home", 1500, _then(_team(), lambda c: c.nodes.update({TEAM_HOME: node(0o120000, 0o777, 1500)}))),
    "home-missing": ("team_account_home", 1500, _then(_team(), lambda c: c.nodes.pop(TEAM_HOME))),
    "mail-not-empty": ("team_account_home", 1500, _then(_team(), lambda c: c.nodes.update(
        {"/var/mail/claudlobby": node(0o100000, 0o660, 1500, size=12)}))),
    "mail-foreign": ("team_account_home", 1500, _then(_team(), lambda c: c.nodes.update(
        {"/var/mail/claudlobby": node(0o100000, 0o660, 1001)}))),
    "password-hash": ("team_account_password", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:$6$s$h:20000:0::7:::"))),
    "locked-hash": ("team_account_password", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!$6$s$h:20000:0::7:::"))),
    "no-password": ("team_account_password", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby::20000:0::7:::"))),
    "passwd-field": ("team_account_password", 1500, _team(pw_passwd="")),
    "no-shadow-line": ("team_account_password", 1500, _then(_team(), lambda c: c.shadow.pop("claudlobby"))),
    "maximum-age": ("team_account_expiry", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!:20000:0:99999:7:::"))),
    "inactivity": ("team_account_expiry", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!:20000:0::7:30::"))),
    "expires": ("team_account_expiry", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!:20000:0::7::20500:"))),
    "must-change": ("team_account_expiry", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!:0:0::7:::"))),
    "subuid": ("team_account_ids", 1500, _then(_team(), _set("extra", subuid="claudlobby:165536:65536\n"))),
    "subgid-range": ("team_account_ids", 1500, _then(_team(), _set("extra", subgid="fixture:1000:1000\n"))),
    "directory-service": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files sss\ngroup: files\n"))),
    "directory-service-no-colon": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\ngroup sss\n"))),
    "must-change-00": ("team_account_expiry", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!:00:0::7:::"))),
    "must-change-signed": ("team_account_expiry", 1500, _then(_team(), _set("shadow", claudlobby="claudlobby:!:-0:0::7:::"))),
    "gshadow-administrator": ("team_account_group", 1500, _then(_team(), _set("gshadow", claudlobby="claudlobby:!:fixture:"))),
    "gshadow-member": ("team_account_group", 1500, _then(_team(), _set("gshadow", claudlobby="claudlobby:!::fixture"))),
    "gshadow-hash": ("team_account_group", 1500, _then(_team(), _set("gshadow", claudlobby="claudlobby:$6$s$h::"))),
    "gshadow-locked-hash": ("team_account_group", 1500, _then(_team(), _set("gshadow", claudlobby="claudlobby:!$6$s$h::"))),
    "gshadow-short": ("team_account_group", 1500, _then(_team(), _set("gshadow", claudlobby="claudlobby:!:"))),
    "gshadow-twice": ("team_account_group", 1500, _then(_team(), _set("gshadow", again="claudlobby:!::"))),
    "group-file-hash": ("team_account_group", 1500, _then(_team(), lambda c: (
        c.gshadow.pop("claudlobby"), c.groups["claudlobby"].update(passwd="$6$s$h")))),
    "sudo-rule": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="claudlobby ALL=(ALL) NOPASSWD:ALL\n"))),
    "sudo-group-rule": ("team_account_sudo", 1500, _then(_team(), _set(
        "extra", sudoers=UBUNTU_SUDOERS.replace("%admin", "%claudlobby")))),
    "sudo-include-elsewhere": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", local="#include /etc/sudoers.local\n"))),
    "sudo-includedir-elsewhere": ("team_account_sudo", 1500, _then(_team(), _set(
        "extra", sudoers=UBUNTU_SUDOERS.replace("@includedir /etc/sudoers.d", "@includedir /etc/sudoers.d/../local")))),
    "sudo-too-many-files": ("team_account_sudo", 1500, _then(_team(), lambda c: c.sudoers_d.update(
        {f"rule{n:02d}": "# none\n" for n in range(63)}))),
    "sudo-file-oversize": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", big="#" * 65536 + "\n"))),
    # sudo reads no comment here, but a line that names the team refuses whatever follows a #.
    "sudo-rule-commented": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="# claudlobby ALL=(ALL) ALL\n"))),
    "sudo-uid-then-name": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="#1001,claudlobby ALL=(ALL) ALL\n"))),
    "sudo-quoted-continued": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", team='"claud\\\n\tlobby" ALL=(ALL) NOPASSWD: ALL\n'))),
    "inline-hash-source": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files # sss\n"))),
    "sudo-continued-across-files": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", a='"claud\\\n', b='lobby" ALL=(ALL) NOPASSWD: ALL\n'))),
    # sudo ends a line at a NUL, so \ then NUL continues it.
    "sudo-nul-continuation": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", team='"claud\\\0\n\tlobby" ALL=(ALL) ALL\n'))),
    # Include forms sudo reads that a plain "@include <path>" pattern misses.
    "sudo-include-quoted": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team='@include"/etc/sudoers.local"\n'))),
    "sudo-include-hash": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="@include#local\n"))),
    "sudo-include-continued": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", team="@include\\\n/etc/sudoers.local\n"))),
    "sudo-include-after-bangs": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", team="!!@include /etc/sudoers.local\n"))),
    "sudo-quoted-continued-crlf": ("team_account_sudo", 1500, _then(_team(), _set(
        "sudoers_d", team='"claud\\\r\nlobby" ALL=(ALL) NOPASSWD: ALL\r\n'))),
    "sudo-alias-after-uid": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team=(
        "User_Alias BOTS = #1001, claudlobby\nBOTS ALL=(ALL) NOPASSWD: ALL\n")))),
    "sudo-escaped": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="claud\\lobby ALL=(ALL) ALL\n"))),
    "sudo-hex-escaped": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="\\x63laudlobby ALL=(ALL) ALL\n"))),
    "sudo-other-case": ("team_account_sudo", 1500, _then(_team(), _set("sudoers_d", team="ClaudLobby ALL=(ALL) ALL\n"))),
    "shadow-ldap": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\nshadow: ldap files\n"))),
    "gshadow-sss": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\ngshadow: files sss\n"))),
    "initgroups-ldap": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\ninitgroups: ldap\n"))),
    "sudoers-sss": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\nsudoers: files sss\n"))),
    # sudo reads its own nsswitch line in any case; glibc reads names exactly,
    # so FILES is a module of that name and Passwd: is no passwd line at all.
    "files-upper-case": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: FILES\ngroup: files\n"))),
    "passwd-name-upper-case": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="Passwd: files\ngroup: files\n"))),
    "sudoers-sss-mixed-case": ("team_directory_service", 1500, _then(_team(), _set(
        "extra", nsswitch="passwd: files\ngroup: files\nSudoers: files sss\n"))),
}


@pytest.mark.parametrize("name", sorted(POLICY_REFUSALS))
def test_each_team_account_mismatch_has_its_own_code(computer, name):
    code, uid, change = POLICY_REFUSALS[name]
    change(computer)
    computer.write()
    with pytest.raises(computer.module.Refusal) as error:
        computer.inspect(uid=uid)
    assert error.value.code == code
    assert computer.effects == [] and computer.module.team_state() == "unexpected"


DRIFT = {
    "process": lambda c: c.process(5555, uid=1002),
    "group-made": lambda c: c.groups.update(claudlobby={"gid": 900, "mem": []}),
    "uid-taken": lambda c: c.users.update(late={"pw_uid": 1002, "pw_gid": 1001, "pw_dir": "/home/alice"}),
    "port-taken": lambda c: setattr(c.daemon, "serve", {"TCP": {"443": {"HTTPS": True}}}),
    "account-made": lambda c: c.add_team(1002),
    "directory-service": lambda c: c.extra.update(nsswitch="passwd: files sss\ngroup: files\n"),
}


@pytest.mark.parametrize("name", sorted(DRIFT))
def test_drift_between_inspect_and_apply_is_proposal_changed_before_dispatch(computer, name):
    record = computer.inspect()
    DRIFT[name](computer)
    computer.write()
    with pytest.raises(computer.module.Refusal) as error:
        computer.apply(record)
    assert error.value.code == "proposal_changed"
    assert computer.module.load(RID)["state"] == "planned" and computer.effects == []


def test_a_present_account_that_changes_before_apply_is_proposal_changed(computer):
    computer.add_team(1500)
    record = computer.inspect(uid=1500)
    computer.shadow["claudlobby"] = "claudlobby:$6$s$h:20000:0::7:::"
    computer.write()
    with pytest.raises(computer.module.Refusal) as error:
        computer.apply(record)
    assert error.value.code == "proposal_changed" and computer.effects == []


def test_an_account_someone_else_makes_with_another_uid_refuses_apply_without_dispatch(computer):
    # Not a team refusal, so it keeps its own code; whatever that code is,
    # nothing is dispatched and useradd never runs.
    record = computer.inspect()
    computer.add_team(1500)
    with pytest.raises(computer.module.Refusal) as error:
        computer.apply(record)
    assert error.value.code == "account_changed"
    kept = computer.module.load(RID)
    assert kept["state"] == "planned" and "dispatch" not in kept and computer.effects == []


def test_other_accounts_keep_their_own_apply_refusals(computer):
    record = computer.inspect(kind="ingress", uid=1001, account="fixture")
    computer.daemon.serve = {"TCP": {"443": {"TCPForward": "localhost:22"}}}
    with pytest.raises(computer.module.Refusal) as error:
        computer.apply(record)
    assert error.value.code == "occupied_443"


def test_the_exact_useradd_runs_once_before_apt_and_lingering(computer):
    final = computer.run()
    done = computer.done()
    assert done[0] == USERADD
    assert [argv[0] for argv in done] == ["/usr/sbin/useradd", "/usr/bin/apt-get", "/usr/bin/loginctl"]
    assert done[1][-1] == "jq=1.7.1-3build1" and done[2] == ["/usr/bin/loginctl", "enable-linger", "claudlobby"]
    assert final["state"] == "completed" and final["native_returncode"] == 0 and set(final) <= RECORD_KEYS
    # The record says what was approved; status reads the account as made.
    again = computer.apply(final)
    assert again["observation"]["team_account"] == "conforming" and again["observation"]["state"] == "completed"
    assert computer.done() == done


@pytest.mark.parametrize("failure,code,returncode", [("exit", "useradd_failed", 9), ("policy", "team_account_shell", 0)])
def test_a_useradd_or_policy_failure_is_failed_partial_without_apt_or_lingering(computer, failure, code, returncode):
    if failure == "exit":
        computer.useradd_rc = 9
    else:
        computer.made_shell = "/bin/sh"  # an operator default useradd honoured
    final = computer.run()
    assert (final["state"], final["code"], final["native_returncode"]) == ("failed_partial", code, returncode)
    assert computer.done() == [USERADD] and set(final) <= RECORD_KEYS


@pytest.mark.parametrize("name,code", [("process", "team_uid_unavailable"), ("account-made", "proposal_changed"),
                                       ("port-taken", "occupied_443")])
def test_the_worker_rechecks_the_account_before_useradd(computer, name, code):
    record = computer.inspect()
    assert computer.apply(record)["submission"] == "exec_acknowledged"
    DRIFT[name](computer)
    computer.write()
    final = computer.work()
    assert (final["state"], final["code"]) == ("failed_partial", code) and computer.done() == []


def test_a_retry_adopts_the_account_now_present(computer):
    computer.apt_rc = 100
    first = computer.run()
    assert (first["state"], first["code"]) == ("failed_partial", "package_effect_failed")
    computer.apt_rc = 0
    record = computer.inspect(request_id=LATER)
    assert record["proposal"]["login"]["state"] == "present"
    assert computer.apply(record)["submission"] == "exec_acknowledged"
    assert computer.work(LATER)["state"] == "completed"
    assert [argv[0] for argv in computer.done()].count("/usr/sbin/useradd") == 1


def test_package_lists_refresh_before_the_team_account_exists(computer):
    record = computer.inspect(kind="index-refresh")
    assert computer.apply(record)["submission"] == "exec_acknowledged"
    assert computer.work()["state"] == "completed"
    assert computer.done() == [computer.module.apt_base() + ["update"]]


def test_the_team_route_needs_a_conforming_account(computer):
    computer.add_team(1500)
    proposal = computer.inspect(kind="ingress", uid=1500)["proposal"]
    assert "login" not in proposal and proposal["after"]["Web"][DNS + ":443"]["Handlers"]["/"]["Proxy"] == (
        "unix:" + TEAM_HOME + "/.local/share/claudlobby-browser/data/state/owner/owner.sock")
    computer.users["claudlobby"]["pw_shell"] = "/bin/sh"
    computer.write()
    with pytest.raises(computer.module.Refusal) as error:
        computer.inspect(kind="ingress", uid=1500, request_id=LATER)
    assert error.value.code == "team_account_shell"


def test_a_new_record_removes_only_expired_planned_receipts(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "unit_state", lambda name: {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0"})
    ids = {name: f"{n}{n}{n}{n}{n}{n}{n}{n}-{n}{n}{n}{n}-4{n}{n}{n}-8{n}{n}{n}-{n * 12}" for n, name in
           zip("23456", ("expired", "fresh", "completed", "failed", "conflict"))}
    for name, request_id in ids.items():
        record = module.execute("inspect", {**value, "request_id": request_id}, source, None)
        if name != "fresh":
            record["expires_at"] = 1
        if name in ("completed", "failed", "conflict"):
            record.update(state={"failed": "failed_partial"}.get(name, name), dispatch={"boot_id": module.boot_id()})
        module.save(record)
    module.execute("inspect", value, source, None)
    kept = {path.name for path in module.STORE.glob("*.json")}
    assert kept == {ids[name] + ".json" for name in ("fresh", "completed", "failed", "conflict")} | {value["request_id"] + ".json"}
    assert (module.STORE / "transition.lock").exists() and module.artifact_path(value["artifact_sha256"]).exists()


def test_a_cleared_id_answers_absent_and_an_old_approval_cannot_dispatch_a_new_plan(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    monkeypatch.setattr(module, "native", lambda *a, **k: pytest.fail("no effect from an old approval"))
    old = module.execute("inspect", value, source, None)
    old["expires_at"] = 1
    module.save(old)
    module.execute("inspect", {**value, "request_id": LATER}, source, None)  # clears the expired one
    status = {key: item for key, item in value.items() if key != "kind"}
    for verb, request in (("apply", approval(value, old)), ("status", status)):
        with pytest.raises(module.Refusal) as error:
            module.execute(verb, request, source, None)
        assert error.value.code == "original_receipt_absent"
    # The id may be inspected again; the old digest binds only the same bytes.
    plan["binding"]["dns"] = "moved.example.ts.net"
    fresh = module.execute("inspect", value, source, None)
    assert fresh["state"] == "planned" and fresh["digest"] != old["digest"]
    with pytest.raises(module.Refusal) as error:
        module.execute("apply", approval(value, old), source, None)
    assert error.value.code == "proposal_conflict"
    assert module.load(value["request_id"])["state"] == "planned"


def test_receipt_hygiene_never_follows_a_link(tmp_path, monkeypatch):
    module = payload(); source, value, plan = root_fixture(module, tmp_path, monkeypatch)
    expired = module.execute("inspect", {**value, "request_id": "77777777-7777-4777-8777-777777777777"}, source, None)
    expired["expires_at"] = 1
    module.save(expired)
    outside = tmp_path / "outside.json"
    (module.STORE / "77777777-7777-4777-8777-777777777777.json").rename(outside)
    link = module.STORE / "77777777-7777-4777-8777-777777777777.json"
    link.symlink_to(outside)
    monkeypatch.setattr(module, "no_pending", lambda api=None: None)  # reach the hygiene pass itself
    with pytest.raises((module.Refusal, OSError)):
        module.execute("inspect", value, source, None)
    assert link.is_symlink() and outside.exists() and not module.receipt_path(value["request_id"]).exists()


def test_the_leaf_and_its_root_command_stay_within_their_bounds():
    import base64
    leaf = SCRIPT.read_bytes()
    entry = (ROOT / "claudlobby/preinstall/linux-system-entry-v1.sh").read_text()
    assert len(leaf) <= 61440
    # The website's carrier sends one command, a single argument to the
    # login shell: sh -c '<entry>' linux-system <verb> <base64 leaf>.
    for verb in ("inspect", "apply", "status"):
        command = ("sh -c '" + entry.replace("'", "'\\''") + "' linux-system " + verb + " "
                   + base64.b64encode(leaf).decode("ascii"))
        assert len(command.encode()) < 131072
