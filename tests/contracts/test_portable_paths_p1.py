"""P1 portability: source and evals must not depend on one machine layout."""
from pathlib import Path

from tests.path_config import PLUGIN_MAIN, REPO_ROOT


FORBIDDEN = (
    "/opt/" + "dududa20-prototype",
    "/root/data/plugins/" + "dududa20",
    "C:" + r"\Users\王\dududa20-prototype",
)


def test_runtime_and_tests_have_no_machine_specific_paths():
    files = list((REPO_ROOT / "tests").rglob("*.py"))
    files.extend((REPO_ROOT / "packages" / "dududa-agent" / "src").rglob("*.py"))
    files.append(PLUGIN_MAIN)
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if any(value in text for value in FORBIDDEN):
            offenders.append(str(path))
    assert offenders == []


def test_plugin_runtime_path_is_configured_not_embedded():
    text = PLUGIN_MAIN.read_text(encoding="utf-8")
    assert 'os.environ.get("DUDUDA_AGENT_SRC"' in text
