"""Load bin/helper.py as a module, the way the wrapper runs it, for tests."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER_PATH = REPO_ROOT / "bin" / "helper.py"

# Point the bridge at a throwaway directory before import so nothing a test
# does can touch a real installation in the repository checkout.
_IMPORT_BRIDGE = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="things-bridge-import-")))
os.chmod(_IMPORT_BRIDGE, 0o700)
os.environ["THINGS_BRIDGE_DIR"] = str(_IMPORT_BRIDGE)
os.environ.pop("THINGS_READ_POLICY", None)
os.environ.pop("THINGS_READ_ALLOWLIST", None)
os.environ.pop("THINGS_REQUIRE_ROOT_POLICY", None)

spec = importlib.util.spec_from_file_location("grokbot_things_helper", HELPER_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"could not load helper from {HELPER_PATH}")
helper = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = helper
spec.loader.exec_module(helper)
