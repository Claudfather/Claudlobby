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
