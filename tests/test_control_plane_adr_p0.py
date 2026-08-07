# -*- coding: utf-8 -*-
"""ADR-0001 Control Plane 立项契约测试（文档 2.5.10 附件条款）。

验证 ADR 文档存在且承诺了强制边界（鉴权/权限/审计/脱敏/Scope/Repository/
不进主链），并确认现有 control plane 雏形仍在（后续 CP-P0 安全基线单独门禁）。
"""
from pathlib import Path

import pytest

PROTO = Path("/opt/dududa20-prototype")
ADR = PROTO / "docs" / "adr" / "0001_control_plane.md"

REQUIRED_COMMITMENTS = (
    "DUDUDA_CP_TOKEN",        # 鉴权
    "PermissionEngine",       # 权限
    "Redactor",               # 脱敏
    "MemoryScope",            # Scope 过滤
    "audit",                  # 审计
    "Repository",             # 不绕过 Repository
    "不进同步消息主链",         # 不进主链
    "CapabilityRegistry",     # MCP 不直连
    "CP-P0",                  # 分阶段
    "CP-P1",
    "CP-P2",
)


class TestAdrContract:
    def test_adr_exists(self):
        assert ADR.exists(), f"ADR 缺失: {ADR}"
        assert ADR.stat().st_size > 2000, "ADR 内容过短"

    def test_adr_status_accepted(self):
        text = ADR.read_text(encoding="utf-8")
        assert "状态：已采纳" in text
        assert "ADR-0001" in text

    def test_adr_commits_to_boundaries(self):
        text = ADR.read_text(encoding="utf-8")
        missing = [k for k in REQUIRED_COMMITMENTS if k not in text]
        assert not missing, f"ADR 缺少承诺: {missing}"

    def test_adr_has_gap_table_and_exit_gate(self):
        text = ADR.read_text(encoding="utf-8")
        assert "现状差距表" in text
        assert "退出门禁" in text

    def test_adr_not_in_phase_8_10_exit(self):
        """按文档 2.5.11：CP 不能算作 Phase 8–10 既定退出条件。"""
        text = ADR.read_text(encoding="utf-8")
        assert "不进入 Phase 8–10 退出条件" in text


class TestExistingControlPlaneStillPresent:
    def test_app_module_importable(self):
        import sys
        sys.path.insert(0, str(PROTO))
        from packages.control_plane import create_app
        assert create_app is not None

    def test_dashboard_html_present(self):
        import sys
        sys.path.insert(0, str(PROTO))
        from packages.control_plane.app import DASHBOARD_HTML
        assert "Dududa 2.0" in DASHBOARD_HTML