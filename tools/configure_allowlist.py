#!/usr/bin/env python3
"""List or update the hardened install's root-owned read allowlist.

Entries are scopes: ``area:<name or id>``, ``project:<name or id>``,
``tag:<name or id>``, or ``unfiled``. Changes are staged in a private
temporary directory and installed with ``sudo install`` so the file stays
root:wheel 600 with a read-only ACL for the current user.
"""

from __future__ import annotations

import argparse
import os
import pwd
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


PRODUCT_ROOT = Path("/Library/Application Support/GrokBotThings")
HEADER = "# Root-owned read allowlist. Managed by configure_allowlist.py.\n"
SCOPE_KINDS = ("area", "project", "tag")


def allowlist_path() -> Path:
    return PRODUCT_ROOT / "users" / str(os.getuid()) / "config" / "allowed_scopes.txt"


def validate_entry(value: str) -> str:
    """Normalize and validate one scope entry (mirrors helper.parse_scope_entry)."""
    entry = value.strip()
    if not entry or len(entry) > 210 or any(ord(char) < 32 for char in entry):
        raise ValueError("entry must be 1..210 printable characters")
    if entry.casefold() == "unfiled":
        return "unfiled"
    kind, sep, name = entry.partition(":")
    kind = kind.strip().casefold()
    name = name.strip()
    if not sep or kind not in SCOPE_KINDS or not name:
        raise ValueError("entry must be area:<name>, project:<name>, tag:<name>, or unfiled")
    return f"{kind}:{name}"


def read_entries(path: Path) -> list[str]:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise RuntimeError(f"hardened allowlist missing: {path}") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
    ):
        raise RuntimeError(f"hardened allowlist is not a protected root-owned file: {path}")
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _verify_destination(path: Path) -> None:
    """Refuse to write anywhere but the expected, symlink-free location."""
    if path != allowlist_path():
        raise RuntimeError("refusing an unexpected policy destination")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        if current.is_symlink():
            raise RuntimeError(f"refusing symlinked policy path component: {current}")
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0:
        raise RuntimeError("allowlist parent must be a root-owned directory")


def install_entries(path: Path, entries: list[str]) -> None:
    _verify_destination(path)
    staging = Path(tempfile.mkdtemp(prefix="grokbot-things-allowlist-"))
    try:
        staged = staging / "allowed_scopes.txt"
        unique = sorted({entry.casefold(): entry for entry in entries}.values(), key=str.casefold)
        staged.write_text(HEADER + "".join(f"{entry}\n" for entry in unique), encoding="utf-8")
        subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/install", "-o", "root", "-g", "wheel", "-m", "600",
             str(staged), str(path)],
            check=True,
        )
        _verify_destination(path)
        subprocess.run(["/usr/bin/sudo", "/bin/chmod", "-N", str(path)], check=True)
        subprocess.run(
            ["/usr/bin/sudo", "/bin/chmod", "+a",
             f"user:{pwd.getpwuid(os.getuid()).pw_name} allow read", str(path)],
            check=True,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("add", "remove", "list"))
    parser.add_argument("entry", nargs="?", help='e.g. "area:Work", "tag:Errands", unfiled')
    args = parser.parse_args()
    path = allowlist_path()
    entries = read_entries(path)

    if args.action == "list":
        if args.entry is not None:
            parser.error("list does not accept an entry")
        print("\n".join(entries) if entries else "(empty: nothing is readable)")
        return 0
    if args.entry is None:
        parser.error(f"{args.action} requires an entry")

    try:
        entry = validate_entry(args.entry)
    except ValueError as exc:
        parser.error(str(exc))
    if args.action == "add":
        entries.append(entry)
    else:
        remaining = [existing for existing in entries if existing.casefold() != entry.casefold()]
        if len(remaining) == len(entries):
            print(f"not present: {entry}")
            return 1
        entries = remaining
    install_entries(path, entries)
    print(f"{args.action} complete: {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
