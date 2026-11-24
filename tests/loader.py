"""Load an extensionless script as a module, with HOME pointed at a sandbox, for unit tests."""

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType


def load_script(path: Path, home: Path, name: str) -> ModuleType:
    """Import `path` as `name`; module-level paths (Path.home()) resolve under `home`."""
    previous = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        loader = importlib.machinery.SourceFileLoader(name, str(path))
        spec = importlib.util.spec_from_loader(name, loader)
        assert spec is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        loader.exec_module(module)
        return module
    finally:
        if previous is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = previous
