#!/usr/bin/env python3
"""Diagnose a Grok Bot Things helper installation without reading any to-dos."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
from typing import Any

LABEL = "com.mcassano.grokbot-things"
SKILL_NAME = "things-grok-bot"
WRAPPER = "grokbot-things-helper"
CONFIRM = "grokbot-things-confirm"
THINGS_APPS = (
    pathlib.Path("/Applications/Things3.app"),
    pathlib.Path.home() / "Applications" / "Things3.app",
)


def check(status: str, detail: str) -> dict[str, str]:
    return {"status": status, "detail": detail}


def mode(path: pathlib.Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def absolute(path: pathlib.Path) -> pathlib.Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else pathlib.Path.cwd() / expanded


def has_symlink_component(path: pathlib.Path) -> bool:
    current = pathlib.Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            return True
    return False


def describe(path: pathlib.Path) -> str:
    if not path.exists():
        return f"{path} missing"
    metadata = path.lstat()
    return f"{path} uid={metadata.st_uid} mode={oct(stat.S_IMODE(metadata.st_mode))}"


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def inspect_install(args: argparse.Namespace) -> dict[str, Any]:
    bridge = absolute(args.bridge)
    code_root = absolute(args.code_root or bridge)
    hardened = code_root != bridge
    uid = os.getuid()
    expected_code_uid = 0 if hardened else uid
    checks: dict[str, dict[str, str]] = {}

    def private_dir(path: pathlib.Path) -> bool:
        return (
            path.is_dir()
            and not has_symlink_component(path)
            and path.stat().st_uid == uid
            and mode(path) & 0o077 == 0
        )

    checks["bridge_root"] = check("pass" if private_dir(bridge) else "fail", describe(bridge))
    for name, path in {
        "requests_dir": bridge / "control" / "requests",
        "responses_dir": bridge / "control" / "responses",
        "policy_dir": bridge / "policy",
    }.items():
        checks[name] = check("pass" if private_dir(path) else "fail", describe(path))

    code_ok = (
        code_root.is_dir()
        and not has_symlink_component(code_root)
        and code_root.stat().st_uid == expected_code_uid
        and mode(code_root) & 0o022 == 0
    )
    checks["code_root"] = check("pass" if code_ok else "fail", describe(code_root))

    executables = {
        "fda_wrapper": code_root / "bin" / WRAPPER,
        "confirmation_helper": code_root / "bin" / CONFIRM,
    }
    for name, path in executables.items():
        ok = (
            path.is_file()
            and not has_symlink_component(path)
            and path.stat().st_uid == expected_code_uid
            and mode(path) == (0o555 if hardened else 0o700)
            and os.access(path, os.X_OK)
        )
        checks[name] = check("pass" if ok else "fail", describe(path))

    files = {
        "helper_source": (code_root / "bin" / "helper.py", 0o444 if hardened else 0o500, expected_code_uid),
        "write_gate_source": (code_root / "bin" / "write_gate.py", 0o444 if hardened else 0o400, expected_code_uid),
        "log": (bridge / "control" / "log.txt", 0o600, uid),
        "read_policy": (bridge / "policy" / "read_policy.txt", 0o600, uid),
        "write_policy": (bridge / "policy" / "write_policy.txt", 0o600, uid),
        "blocklist": (bridge / "policy" / "blocked_scopes.txt", 0o600, uid),
    }
    for name, (path, expected_mode, expected_uid) in files.items():
        ok = (
            path.is_file()
            and not has_symlink_component(path)
            and path.stat().st_uid == expected_uid
            and mode(path) == expected_mode
        )
        checks[name] = check(
            "pass" if ok else "fail", f"{describe(path)} expected={oct(expected_mode)}"
        )

    allowlist = (
        code_root.parent / "config" / "allowed_scopes.txt"
        if hardened
        else bridge / "policy" / "allowed_scopes.txt"
    )
    allowlist_ok = (
        allowlist.is_file()
        and not has_symlink_component(allowlist)
        and allowlist.stat().st_uid == (0 if hardened else uid)
        and mode(allowlist) == 0o600
        and os.access(allowlist, os.R_OK)
    )
    checks["read_allowlist"] = check("pass" if allowlist_ok else "fail", describe(allowlist))

    if not args.skip_codesign:
        for name, path in executables.items():
            result = run(["/usr/bin/codesign", "--verify", "--strict", str(path)])
            checks[f"{name}_signature"] = check(
                "pass" if result.returncode == 0 else "fail",
                (result.stderr or result.stdout or "signature valid").strip(),
            )

    if not args.skip_launchd:
        launchctl = shutil.which("launchctl")
        if launchctl is None:
            checks["launchd"] = check("fail", "launchctl not found")
        else:
            result = run([launchctl, "print", f"gui/{uid}/{LABEL}"])
            checks["launchd"] = check(
                "pass" if result.returncode == 0 else "fail",
                f"{LABEL} loaded" if result.returncode == 0 else f"{LABEL} not loaded",
            )

    app = next((path for path in THINGS_APPS if path.is_dir()), None)
    checks["things_app"] = check(
        "pass" if app else "fail", str(app) if app else "Things3.app not found in /Applications"
    )
    # This process's own access says nothing about the wrapper's TCC grant.
    checks["full_disk_access"] = check(
        "warn",
        "cannot be verified from this process; send a 'status' request and confirm "
        "checks.things_db_readable is true (see docs/SMOKE_TEST.md)",
    )

    if not args.skip_grok:
        grok = shutil.which("grok")
        if grok is None:
            checks["grok_skill"] = check("warn", "grok CLI not found; skill discovery not verified")
        else:
            result = run([grok, "inspect"])
            found = result.returncode == 0 and SKILL_NAME in result.stdout
            checks["grok_skill"] = check(
                "pass" if found else "fail",
                "skill discovered" if found else f"grok inspect did not report {SKILL_NAME}",
            )

    return {
        "ok": all(value["status"] != "fail" for value in checks.values()),
        "architecture": "hardened" if hardened else "standard",
        "bridge": str(bridge),
        "code_root": str(code_root),
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", required=True, type=pathlib.Path)
    parser.add_argument("--code-root", type=pathlib.Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--skip-grok", action="store_true")
    parser.add_argument("--skip-launchd", action="store_true")
    parser.add_argument("--skip-codesign", action="store_true")
    args = parser.parse_args()

    report = inspect_install(args)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for name, result in report["checks"].items():
            print(f"[{result['status'].upper():4}] {name}: {result['detail']}")
        warnings = any(result["status"] == "warn" for result in report["checks"].values())
        overall = (
            "healthy with warnings" if report["ok"] and warnings
            else "healthy" if report["ok"] else "attention required"
        )
        print(f"\nOverall: {overall} ({report['architecture']} install)")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
