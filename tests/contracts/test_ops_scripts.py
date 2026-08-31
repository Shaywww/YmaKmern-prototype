# -*- coding: utf-8 -*-
"""ops.sh 运维脚本测试：语法 + manifest/health 输出 + smoke --fast。"""
import json, os, pathlib, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPS = ROOT / "ops" / "ops.sh"


def _run(*args, out_dir=None):
    env = dict(os.environ)
    # Contract tests validate the portable script itself.  Requiring a running
    # production systemd unit belongs to deployment smoke, not GitHub CI.
    env["DUDUDA_SMOKE_REQUIRE_SERVICE"] = "0"
    if out_dir is not None:
        env["OPS_OUT"] = str(out_dir)
    return subprocess.run(
        ["bash", str(OPS), *args], capture_output=True, text=True, env=env)


def test_cp_status_runs():
    """ADR-0001 CP-P0：ops.sh cp status 可执行且输出关键项。"""
    r = _run("cp", "status")
    assert r.returncode == 0, r.stderr
    assert "cp token configured" in r.stdout
    assert "cp audit" in r.stdout


def test_syntax():
    r = subprocess.run(["bash", "-n", str(OPS)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_manifest_generates_v2(tmp_path):
    r = _run("manifest", out_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    data = json.loads(
        (tmp_path / "supply_chain_manifest.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert len(data["repos"]["prototype"]["commit"]) == 40
    assert len(data["repos"]["plugin"]["commit"]) == 40
    assert isinstance(data["repos"]["prototype"]["working_tree_clean"], bool)
    assert isinstance(data["repos"]["plugin"]["working_tree_clean"], bool)
    assert "image_digest" in data["containers"]["napcat"]
    assert isinstance(data["containers"]["napcat"]["mounts"], list)
    # A source-only CI runner has no NapCat container, so ``ok`` may be false;
    # production exit/eval gates still require a real digest and ``ok=true``.
    assert data["ok"] is bool(
        data["repos"]["prototype"]["commit"]
        and data["repos"]["plugin"]["commit"]
        and data["containers"]["napcat"]["image_digest"])


def test_health_generates_json(tmp_path):
    r = _run("health", out_dir=tmp_path)
    assert r.returncode == 0, r.stderr
    data = json.loads(
        (tmp_path / "health_status.json").read_text(encoding="utf-8"))
    assert data["service"]["name"] == "astrbot"
    assert "banner" in data
    assert "recent_errors" in data
    assert "backup" in data
    assert isinstance(data["ok"], bool)


def test_smoke_fast_passes():
    r = _run("smoke", "--fast")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "smoke:" in r.stdout
    assert "0 failed" in r.stdout


def test_usage_without_args_fails():
    r = _run()
    assert r.returncode != 0
    assert "usage:" in r.stderr
