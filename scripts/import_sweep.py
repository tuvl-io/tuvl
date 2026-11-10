"""Import every tuvl module (and every tuvl_insight module when it is installed).

An install whose dependencies resolved to an incompatible version fails here.
"""

import importlib
import importlib.util
import pkgutil
import sys

import tuvl
from tuvl.extras import MissingExtraError

packages = [tuvl]
if importlib.util.find_spec("tuvl_insight") is not None:
    import tuvl_insight

    packages.append(tuvl_insight)

failed = []
need_extra = []
total = 0
for package in packages:
    for mod in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        total += 1
        try:
            importlib.import_module(mod.name)
        except MissingExtraError as exc:  # a bare install: the module's extra is not installed
            need_extra.append(f"{mod.name}: {exc.extra}")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{mod.name}: {type(exc).__name__}: {str(exc).splitlines()[0][:160]}")
print(f"imported {total - len(need_extra)} of {total} modules, {len(failed)} failed")
for line in need_extra:
    print("  needs an extra:", line)
for line in failed:
    print("  FAIL", line)
sys.exit(1 if failed else 0)
