"""Shared test plumbing.

The test suite builds throwaway game trees on disk, and it deliberately avoids
``tempfile.TemporaryDirectory``: these tests may run under a file sandbox that
only permits writes inside the workspace, where the system temp directory is
read-only.  Scratch directories therefore live next to the tests and are
removed in ``tearDown``.

Part of the suite runs against a real ``renpy/loader.py``.  None is bundled -- it
is Ren'Py's code, not this project's -- so :func:`sdk_loader` finds one on the
machine and the dependent tests skip when there is none.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TESTS_ROOT = Path(__file__).resolve().parent
SCRATCH_ROOT = REPO_ROOT / ".test-scratch"

#: Where an SDK or a game might live.  The patterns are cheap because pathlib
#: prunes on the trailing literal components rather than walking the tree -- the
#: widest one below costs ~20 ms over a whole 625 GB drive.
_SDK_GLOBS = (
    "renpy*/renpy/loader.py",  # <root>/renpy-8.5.2-sdk/renpy/loader.py
    "*/renpy-*/renpy/loader.py",  # <root>/Tools/renpy-8.5.2-sdk/renpy/loader.py
    "*/renpy/loader.py",  # <root>/MyGame/renpy/loader.py
    "*/*/renpy/loader.py",  # <root>/projects/MyGame/renpy/loader.py
)

_cached: list[Path] | None = None


def _search_roots() -> list[Path]:
    """Directory trees worth looking in, cheapest and most likely first."""
    roots = [Path.home(), Path.home() / "Downloads", Path.home() / "Documents"]

    if os.name == "nt":
        import string

        roots.extend(Path(f"{letter}:/") for letter in string.ascii_uppercase)
    else:
        roots.extend(
            [Path("/opt"), Path("/usr/lib"), Path("/usr/local/lib"), Path("/")]
        )

    # A checkout of this tool sitting next to a game is common enough to be worth
    # checking, and costs nothing.
    roots.extend([REPO_ROOT, REPO_ROOT.parent])
    return roots


def _sdk_candidates() -> list[Path]:
    """Every plausible ``renpy/loader.py`` on this machine, best guess first.

    Discovery rather than a hardcoded path: the suite is published, so it cannot
    depend on one developer's directory layout.  Order matters -- an explicit
    request wins, then a copy dropped into the repo, then a scan.

    Set ``RENPY_SDK`` to a Ren'Py SDK directory (or ``RENPY_LOADER`` straight to a
    ``loader.py``) to skip the scan entirely.
    """
    found: list[Path] = []

    # 1. Explicit: one specific loader, or an SDK root.
    for variable in ("RENPY_LOADER", "RENPY_UNPACK_LOADER"):
        value = os.environ.get(variable)
        if value:
            found.append(Path(value))
    for variable in ("RENPY_SDK", "RENPY_SDK_ROOT", "RENPY_BASE"):
        value = os.environ.get(variable)
        if value:
            found.append(Path(value) / "renpy" / "loader.py")

    # 2. A copy dropped into the repo (gitignored, but handy for a full run).
    found.append(REPO_ROOT / "renpy" / "loader.py")
    found.append(REPO_ROOT / "vendor" / "renpy" / "loader.py")

    # 3. Conventional locations.
    for root in _search_roots():
        try:
            if not root.is_dir():
                continue
        except OSError:  # pragma: no cover - unreadable drive
            continue
        for pattern in _SDK_GLOBS:
            try:
                found.extend(sorted(root.glob(pattern)))
            except OSError:  # pragma: no cover - unreadable tree
                continue

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in found:
        try:
            resolved = path.resolve()
        except OSError:  # pragma: no cover - unresolvable path
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def sdk_loaders() -> list[Path]:
    """All usable ``renpy/loader.py`` files, found once per run."""
    global _cached
    if _cached is None:
        _cached = _sdk_candidates()
    return list(_cached)


def sdk_loader() -> Path | None:
    """The Ren'Py ``loader.py`` to test against, or ``None`` if there is none."""
    loaders = sdk_loaders()
    return loaders[0] if loaders else None


class ScratchCase(unittest.TestCase):
    """A test case with a private, workspace-local scratch directory."""

    scratch: Path

    def setUp(self) -> None:
        self.scratch = SCRATCH_ROOT / self._testMethodName
        shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.scratch, ignore_errors=True)

    def path(self, *parts: str) -> Path:
        return self.scratch.joinpath(*parts)
