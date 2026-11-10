"""Build the wheel and the source distribution of the current version into dist/.

    python tools/build_release.py                 # needs the "build" package: pip install build
    python tools/build_release.py --no-isolation  # build with the packages installed here (offline)
    python tools/build_release.py --keep-old      # keep the files of other versions in dist/

Steps:

1. The version is read from ``src/behavior_tree_widget/__init__.py`` (``__version__``).
2. ``build/`` is deleted: setuptools never removes stale files from it, and they would end up in
   the wheel (for example a module that was turned into a package).
3. ``tools/compile_ui.py --check`` makes sure the generated forms match the ``.ui`` files.
4. ``python -m build`` writes ``behavior_tree_widget-<version>.tar.gz`` and
   ``behavior_tree_widget-<version>-py3-none-any.whl`` to ``dist/``.
5. The files of other versions are removed from ``dist/`` (unless ``--keep-old``).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_INIT = ROOT / "src" / "behavior_tree_widget" / "__init__.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
PREFIX = "behavior_tree_widget-"


def read_version() -> str:
    """``__version__`` of the package, read without importing it (which would need Qt)."""
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', PACKAGE_INIT.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise SystemExit(f"no __version__ in {PACKAGE_INIT}")
    return match.group(1)


def is_release_file(path: Path, version: str) -> bool:
    """True for the wheel and the source distribution of ``version``."""
    return path.name == f"{PREFIX}{version}.tar.gz" or path.name.startswith(f"{PREFIX}{version}-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-isolation", action="store_true", help="build with the packages installed here")
    parser.add_argument("--keep-old", action="store_true", help="keep the files of other versions in dist/")
    args = parser.parse_args(argv)

    version = read_version()
    print(f"behavior_tree_widget {version}")
    if BUILD.exists():
        shutil.rmtree(BUILD)
        print(f"deleted {BUILD}")

    check = subprocess.run([sys.executable, str(ROOT / "tools" / "compile_ui.py"), "--check"], cwd=ROOT)
    if check.returncode != 0:
        print("the generated forms are out of date: run tools/compile_ui.py and commit the result", file=sys.stderr)
        return check.returncode

    command = [sys.executable, "-m", "build", "--outdir", str(DIST)]
    if args.no_isolation:
        command.append("--no-isolation")
    built = subprocess.run(command, cwd=ROOT)
    if built.returncode != 0:
        return built.returncode
    if BUILD.exists():
        shutil.rmtree(BUILD)  # leave nothing stale for the next build

    produced = sorted(path for path in DIST.glob(f"{PREFIX}*") if is_release_file(path, version))
    if len(produced) != 2:
        print(f"expected a wheel and a source distribution of {version} in {DIST}, found {produced}", file=sys.stderr)
        return 1
    if not args.keep_old:
        for path in DIST.glob(f"{PREFIX}*"):
            if not is_release_file(path, version):
                path.unlink()
                print(f"removed {path.relative_to(ROOT)}")
    for path in produced:
        print(f"built {path.relative_to(ROOT)} ({path.stat().st_size // 1024} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
