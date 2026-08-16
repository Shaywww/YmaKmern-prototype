#!/usr/bin/env python3
"""Validate a generated shadow candidate without executing candidate content."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def validate(folder: Path) -> list[str]:
    errors: list[str] = []
    skill_path, eval_path = folder / "SKILL.md", folder / "eval_cases.json"
    if not skill_path.is_file():
        errors.append("missing SKILL.md")
    if not eval_path.is_file():
        errors.append("missing eval_cases.json")
    if errors:
        return errors
    text = skill_path.read_text(encoding="utf-8")
    if not re.match(r"^---\nname: [a-z0-9-]+\ndescription: .+\n---\n", text):
        errors.append("invalid SKILL.md frontmatter")
    if "Never install, activate, or deploy it automatically." not in text:
        errors.append("missing no-auto-activation boundary")
    try:
        cases = json.loads(eval_path.read_text(encoding="utf-8"))
    except ValueError:
        errors.append("invalid eval_cases.json")
        return errors
    if not cases.get("evidence_fingerprints"):
        errors.append("missing evidence fingerprints")
    if not cases.get("cases"):
        errors.append("missing regression cases")
    return errors


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_candidate.py CANDIDATE_DIR")
    problems = validate(Path(sys.argv[1]))
    if problems:
        raise SystemExit("; ".join(problems))
    print("candidate valid")
