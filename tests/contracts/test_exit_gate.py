# -*- coding: utf-8 -*-
"""exit_gate_check.sh：2.5.11 P0/P1/P2 退出门禁证据脚本测试。"""
import pathlib, subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
GATE = ROOT / "ops" / "exit_gate_check.sh"


def test_syntax():
    r = subprocess.run(["bash", "-n", str(GATE)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_fast_mode_all_pass():
    r = subprocess.run(["bash", str(GATE), "--fast"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "== P0 gate" in r.stdout
    assert "== P1 gate" in r.stdout
    assert "== P2 gate" in r.stdout
    assert "FAIL=0" in r.stdout
    assert "P0 门禁测试存在: test_migration_p0" in r.stdout
    assert "P1 DUDUDA_ROUTER 开关" in r.stdout
    assert "P2 manage.sh backup/restore/rollback" in r.stdout
