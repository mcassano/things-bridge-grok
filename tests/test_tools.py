from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests._helper_loader import REPO_ROOT, helper
from tests.fixtures import realpath_tempdir


def load_tool(name: str):
    path = REPO_ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"things_tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


configure_allowlist = load_tool("configure_allowlist")
check_version = load_tool("check_version")
doctor = load_tool("doctor")


class AllowlistToolTests(unittest.TestCase):
    def test_entries_mirror_the_helper_parser(self) -> None:
        for raw, expected in (("Area:Work", "area:Work"), (" tag: Errands ", "tag:Errands"),
                              ("UNFILED", "unfiled"), ("project:Taxes: 2026", "project:Taxes: 2026")):
            with self.subTest(raw=raw):
                normalized = configure_allowlist.validate_entry(raw)
                self.assertEqual(normalized, expected)
                helper.parse_scope_entry(normalized)
        for bad in ("Work", "folder:x", "tag:", "area:\x00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                configure_allowlist.validate_entry(bad)

    def test_refuses_unexpected_destinations(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unexpected"):
            configure_allowlist.install_entries(Path("/tmp/allowed_scopes.txt"), ["unfiled"])


class VersionTests(unittest.TestCase):
    def test_versions_agree(self) -> None:
        self.assertEqual(check_version.helper_constant("HELPER_VERSION"), helper.HELPER_VERSION)
        self.assertEqual(check_version.skill_version(), helper.HELPER_VERSION)
        self.assertIsNone(check_version.changelog_error(helper.HELPER_VERSION))

    def test_cli(self) -> None:
        ok = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "check_version.py"),
                             f"v{helper.HELPER_VERSION}"], capture_output=True, text=True)
        self.assertEqual(ok.returncode, 0, ok.stdout)
        bad = subprocess.run([sys.executable, str(REPO_ROOT / "tools" / "check_version.py"), "v0.0.1"],
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)


class DoctorTests(unittest.TestCase):
    def test_reports_a_healthy_standard_layout(self) -> None:
        root = realpath_tempdir("things-doctor-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.chmod(root, 0o700)
        for directory in ("control/requests", "control/responses", "policy", "bin"):
            (root / directory).mkdir(parents=True, mode=0o700)
        os.chmod(root / "control", 0o700)
        for name, mode in (("bin/grokbot-things-helper", 0o700), ("bin/grokbot-things-confirm", 0o700),
                           ("bin/helper.py", 0o500), ("bin/write_gate.py", 0o400),
                           ("control/log.txt", 0o600), ("policy/read_policy.txt", 0o600),
                           ("policy/write_policy.txt", 0o600), ("policy/blocked_scopes.txt", 0o600),
                           ("policy/allowed_scopes.txt", 0o600)):
            (root / name).write_text("")
            os.chmod(root / name, mode)
        args = argparse.Namespace(bridge=root, code_root=None, skip_grok=True,
                                  skip_launchd=True, skip_codesign=True)
        report = doctor.inspect_install(args)
        failures = {k: v for k, v in report["checks"].items() if v["status"] == "fail" and k != "things_app"}
        self.assertEqual(failures, {})
        self.assertEqual(report["architecture"], "standard")

        os.chmod(root / "policy", 0o755)
        report = doctor.inspect_install(args)
        self.assertEqual(report["checks"]["policy_dir"]["status"], "fail")


class RepositoryHygieneTests(unittest.TestCase):
    def test_plist_template_is_valid(self) -> None:
        if not shutil.which("plutil"):
            self.skipTest("plutil not available")
        template = REPO_ROOT / f"{helper.LAUNCHD_LABEL}.plist.template"
        result = subprocess.run(["plutil", "-lint", str(template)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_identity_is_consistent(self) -> None:
        for path in ("install.sh", "install-hardened.sh", "uninstall.sh", "uninstall-hardened.sh",
                     "tools/doctor.py"):
            with self.subTest(path=path):
                self.assertIn(helper.LAUNCHD_LABEL, (REPO_ROOT / path).read_text())
        skill = (REPO_ROOT / "SKILL.md").read_text()
        self.assertIn("name: things-grok-bot", skill)
        for action in helper.ACTIONS:
            with self.subTest(action=action):
                self.assertIn(f"`{action}`", skill)
                self.assertIn(f"`{action}`", (REPO_ROOT / "docs" / "PROTOCOL.md").read_text())


if __name__ == "__main__":
    unittest.main()
