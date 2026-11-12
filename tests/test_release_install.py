"""The explicit Git bootstrap entry; removed bundle flags stay refused."""
import json
from pathlib import Path

import pytest


def test_git_bootstrap_entry_prepares_only_explicit_inputs(tmp_path, monkeypatch, capsys):
    from claudlobby import bootstrap
    calls = []
    installation, root, interpreter = tmp_path / "installation", tmp_path / "data", tmp_path / "python"
    def prepare(directory, data_root, **inputs):
        calls.append((directory, data_root, inputs))
        return {"installation": str(directory), "data_root": str(data_root), "source_revision": "a" * 40,
                "cli": str(directory / "bin/claudlobby"), "prepared": True, "activated": False}
    monkeypatch.setattr(bootstrap, "prepare_git_installation", prepare)
    assert bootstrap.main(["--installation", str(installation), "--root", str(root),
                           "--remote", "https://example.invalid/repo.git", "--ref", "main",
                           "--interpreter", str(interpreter), "--json"]) == 0
    assert calls == [(installation, root, {"remote": "https://example.invalid/repo.git", "ref": "main",
                                          "interpreter": interpreter})]
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] and result["prepared"] and not result["activated"]
    assert result["source_revision"] == "a" * 40 and result["cli"] == str(installation / "bin/claudlobby")
    assert not root.exists() and not installation.exists()


@pytest.mark.parametrize("removed", ["--guided", "--wheel", "--wheelhouse"])
def test_git_bootstrap_rejects_removed_bundle_entry_flags(tmp_path, monkeypatch, removed):
    from claudlobby import bootstrap
    monkeypatch.setattr(bootstrap, "prepare_git_installation", lambda *_a, **_kw: pytest.fail("preparation started"))
    with pytest.raises(SystemExit) as refused:
        bootstrap.main(["--installation", str(tmp_path / "installation"), "--root", str(tmp_path / "data"),
                        "--remote", "https://example.invalid/repo.git", "--ref", "main",
                        "--interpreter", str(tmp_path / "python"), removed])
    assert refused.value.code == 2


@pytest.mark.parametrize("failure,status,code", [(ValueError("SECRET retained metadata"), 4, "conflict"),
                                                (OSError("SECRET subprocess output"), 6, "unavailable")])
def test_git_bootstrap_failure_never_echoes_private_inputs(tmp_path, monkeypatch, capsys, failure, status, code):
    from claudlobby import bootstrap
    def fail(*_args, **_kwargs):
        raise failure
    monkeypatch.setattr(bootstrap, "prepare_git_installation", fail)
    assert bootstrap.main(["--installation", str(tmp_path / "installation"), "--root", str(tmp_path / "data"),
                           "--remote", "https://SECRET@example.invalid/repo.git", "--ref", "main",
                           "--interpreter", str(tmp_path / "python"), "--json"]) == status
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    result = json.loads(captured.out)
    assert not result["ok"] and result["error"]["code"] == code
