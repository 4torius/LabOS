"""Load a SiLA2 server's `src/` package under a private module name.

Both OpentronsSiLA2Server and TecanM200SiLA2Server ship a package literally
named `src`, same as v1's own top-level `src` package. Importing them the
normal way (`sys.path.insert` + `import src`) would collide with whichever
`src` package pytest already cached in sys.modules for this session. Loading
each one under a unique name (`_ot_src`, `_tc_src`) sidesteps the collision
while still resolving the package's internal relative imports correctly.
"""
import importlib.util
import sys
from pathlib import Path

_V1_DIR = Path(__file__).resolve().parents[2]
OT_SRC_DIR = _V1_DIR / "SiLA2" / "OpentronsSiLA2Server" / "src"
TC_SRC_DIR = _V1_DIR / "SiLA2" / "TecanM200SiLA2Server" / "src"


def load_package(module_name: str, src_dir: Path):
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, src_dir / "__init__.py", submodule_search_locations=[str(src_dir)]
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = pkg
    spec.loader.exec_module(pkg)
    return pkg


def load_opentrons_src():
    return load_package("_ot_src", OT_SRC_DIR)


def load_tecan_src():
    return load_package("_tc_src", TC_SRC_DIR)
