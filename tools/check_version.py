#!/usr/bin/env python3
"""Verify that a release tag matches every shipped version string."""

from __future__ import annotations

import argparse
import ast
import re
from datetime import date
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def helper_constant(name: str) -> str:
    module = ast.parse((REPO_ROOT / "bin" / "helper.py").read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str):
                        return value
    raise RuntimeError(f"{name} not found in bin/helper.py")


def skill_version() -> str:
    lines = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise RuntimeError("YAML frontmatter not found in SKILL.md")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line == "---")
    except StopIteration:
        raise RuntimeError("YAML frontmatter is not closed in SKILL.md") from None
    matches = re.findall(r"^version:[ \t]*([^ \t\r\n]+)[ \t]*$", "\n".join(lines[1:end]), re.MULTILINE)
    if len(matches) != 1:
        raise RuntimeError("expected exactly one frontmatter version in SKILL.md")
    return matches[0]


def changelog_error(version: str) -> str | None:
    content = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = rf"^##[ \t]+{re.escape(version)}[ \t]+-[ \t]+([^\r\n]*?)[ \t]*\r?$"
    matches = re.findall(pattern, content, re.MULTILINE)
    if len(matches) != 1:
        return f"CHANGELOG.md must contain exactly one dated heading for {version}; found {len(matches)}"
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", matches[0]):
            raise ValueError
        date.fromisoformat(matches[0])
    except ValueError:
        return f"CHANGELOG.md has invalid date {matches[0]!r}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, for example v1.0.0")
    args = parser.parse_args()

    expected = args.tag[1:] if args.tag.startswith("v") else args.tag
    versions = {"helper": helper_constant("HELPER_VERSION"), "skill": skill_version()}
    mismatches = {name: value for name, value in versions.items() if value != expected}
    for name, value in mismatches.items():
        print(f"{name} version {value!r} does not match tag {args.tag!r}")
    if mismatches:
        return 1
    error = changelog_error(expected)
    if error:
        print(error)
        return 1
    print(f"release versions match {args.tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
