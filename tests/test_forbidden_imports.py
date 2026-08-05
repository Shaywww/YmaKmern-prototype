"""Phase 2 —— 核心契约包禁止反向依赖基础设施。

对应文档 2.4.1 / 2.5.1：核心 Package 不得导入 AstrBot、OneBot、NapCat、
Docker、具体 MCP Server、Iris 或 Provider SDK；MCP Server 不得反向导入
Agent Runtime。
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCAN_DIRS = ("packages/core", "packages/runtime")
BANNED_ROOTS = (
    "astrbot",
    "napcat",
    "onebot",
    "aiocqhttp",
    "lagrange",
    "packages.adapters",
    "packages.mcp",
    "packages.router",
    "packages.control_plane",
)
IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([\w.]+)")


def _iter_py_files():
    for d in SCAN_DIRS:
        base = ROOT / d
        for f in sorted(base.rglob("*.py")):
            yield f


def test_core_has_no_banned_imports():
    violations = []
    for f in _iter_py_files():
        for lineno, line in enumerate(f.read_text(encoding="utf-8"), 1):
            m = IMPORT_RE.match(line)
            if not m:
                continue
            mod = m.group(1)
            if mod.startswith(BANNED_ROOTS):
                violations.append(
                    f"{f.relative_to(ROOT)}:{lineno}: {line.strip()}"
                )
    assert not violations, "核心包禁止导入基础设施:\n" + "\n".join(violations)


def test_core_dir_does_not_import_sibling_packages():
    """core 只能导入 core 内部与 stdlib/第三方库。"""
    violations = []
    for f in (ROOT / "packages" / "core").rglob("*.py"):
        for lineno, line in enumerate(f.read_text(encoding="utf-8"), 1):
            m = IMPORT_RE.match(line)
            if not m:
                continue
            mod = m.group(1)
            if mod.startswith("packages.") and not mod.startswith(
                ("packages.core", "packages.runtime")
            ):
                violations.append(
                    f"{f.relative_to(ROOT)}:{lineno}: {line.strip()}"
                )
    assert not violations, "core 不得导入兄弟包:\n" + "\n".join(violations)
