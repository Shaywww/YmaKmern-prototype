"""Portable repository locations shared by tests and eval scripts."""
from __future__ import annotations

import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_SRC = REPO_ROOT / "packages" / "dududa-agent" / "src"


def _plugin_main() -> Path:
    configured_main = os.environ.get("DUDUDA_PLUGIN_MAIN", "").strip()
    if configured_main:
        return Path(configured_main).expanduser().resolve()
    configured = os.environ.get("DUDUDA_PLUGIN_DIR", "").strip()
    if configured:
        return (Path(configured).expanduser() / "main.py").resolve()
    local_copy = REPO_ROOT / "plugin_main.py"
    if local_copy.is_file():
        return local_copy.resolve()
    candidates = (
        REPO_ROOT.parent / "YmaKmern-plugin",
        REPO_ROOT.parent / "dududa20-plugin",
        REPO_ROOT / "plugin",
        Path.home() / "data" / "plugins" / "dududa20",
    )
    directory = next((path.resolve() for path in candidates
                      if (path / "main.py").is_file()), candidates[0].resolve())
    return directory / "main.py"


PLUGIN_MAIN = _plugin_main()
PLUGIN_DIR = PLUGIN_MAIN.parent


def configure_import_paths() -> None:
    """Make direct script execution behave like the pytest configuration."""
    for path in (AGENT_SRC, PLUGIN_DIR):
        rendered = str(path)
        if rendered not in sys.path:
            sys.path.insert(0, rendered)
