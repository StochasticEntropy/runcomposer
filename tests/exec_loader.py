"""``runcomposer_exec`` loaded from the source tree.

The consumer is one file that is *copied* into an installed environment
(pyproject's force-include), so ``import runcomposer_exec`` gives whatever was
current when the environment was last installed — in an editable checkout
that can be an older copy than ``src/runcomposer_exec.py``. Tests are about
the file in the tree, so they load exactly that.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

EXEC_FILE = Path(__file__).resolve().parent.parent / "src" / "runcomposer_exec.py"


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("runcomposer_exec_from_source", EXEC_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runcomposer_exec = load()
