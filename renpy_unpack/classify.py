"""Evidence-based classification of a game folder.

The rest of the tool asks precise questions -- is there a ``renpy/loader.py``, are there
``.rpa`` archives -- and refuses to guess when the answer is no.  That is right for the
extractors, but it makes the *diagnosis* useless: a build that deleted ``renpy/`` and
renamed its archives (HuangmeiC) fails every check and the user is told "found no
archives", which describes the symptom instead of the build.

This module answers the earlier question instead: what kind of Ren'Py build is this, and
which backends can work on it?  It looks for evidence rather than required files, sniffs
archive content instead of extensions, and returns a reason for every backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .preflight import find_archives, looks_like_archive

#: directories that hold Ren'Py's own runtime, whatever the game chose to keep
_RUNTIME_GLOBS = ("lib/py3-windows-x86_64", "lib/py3-linux-x86_64", "lib/windows-i686",
                  "lib/linux-i686", "lib/py3-mac-x86_64", "lib/mac-x86_64")

#: archive extensions in the wild -- stock, and ones we have seen renamed
_ARCHIVE_SUFFIXES = (".rpa", ".rpi", ".blend", ".dll", ".dat", ".arc", ".pak", ".bin")


@dataclass
class Backend:
    name: str
    applicable: str          # "yes" | "no" | "unknown"
    reason: str


@dataclass
class GameProfile:
    root: Path
    game_dir: Path | None
    engine: str                       # source | pyc | compiled-pyd | runtime-only | unknown
    evidence: list[str] = field(default_factory=list)
    archives: list[Path] = field(default_factory=list)
    archive_candidates: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    backends: list[Backend] = field(default_factory=list)

    def backend(self, name: str) -> Backend | None:
        for entry in self.backends:
            if entry.name == name:
                return entry
        return None


def _runtime_dirs(root: Path) -> list[Path]:
    return [root / rel for rel in _RUNTIME_GLOBS if (root / rel).is_dir()]


def _python_dlls(root: Path) -> list[Path]:
    found = []
    for rel in _RUNTIME_GLOBS:
        base = root / rel
        if not base.is_dir():
            continue
        for pattern in ("libpython3*.dll", "python3*.dll", "libpython3*.so"):
            found.extend(base.glob(pattern))
    return found


def _count(folder: Path, pattern: str) -> int:
    return sum(1 for _ in folder.rglob(pattern))


def _looks_like_game_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    signs = 0
    if (path / "game").is_dir():
        signs += 1
    if (path / "lib").is_dir():
        signs += 1
    if (path / "renpy").is_dir() or (path / "renpy.pyd").is_file():
        signs += 2
    if any(path.glob("*.exe")) or any(path.glob("*.py")):
        signs += 1
    return signs >= 2


def _descend(root: Path) -> Path:
    """A game shipped inside a single wrapping folder ("LOVE YURI/LoveYuri")."""
    if _looks_like_game_dir(root):
        return root
    children = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if len(children) == 1 and _looks_like_game_dir(children[0]):
        return children[0]
    return root


def classify(start: Path) -> GameProfile:
    root = _descend(Path(start).resolve())
    probe = GameProfile(root=root, game_dir=(root / "game") if (root / "game").is_dir() else None,
                        engine="unknown")

    # --- engine shape -------------------------------------------------------
    loader = root / "renpy" / "loader.py"
    loader_pyc = _count(root / "renpy", "loader.pyc") if (root / "renpy").is_dir() else 0
    renpy_pyc = _count(root / "renpy", "*.pyc") if (root / "renpy").is_dir() else 0
    pyd = root / "renpy.pyd"
    librenpython = [p for rel in _RUNTIME_GLOBS
                    for p in (root / rel).glob("librenpython*")] if (root / "lib").is_dir() else []
    pythons = _python_dlls(root)

    if loader.is_file():
        probe.engine = "source"
        probe.evidence.append("renpy/loader.py (source engine)")
    elif pyd.is_file():
        probe.engine = "compiled-pyd"
        probe.evidence.append("renpy.pyd %.1f MB (compiled engine)"
                              % (pyd.stat().st_size / 1048576))
    elif renpy_pyc:
        probe.engine = "pyc"
        probe.evidence.append("renpy/ with %d .pyc (bytecode only)" % renpy_pyc)
    elif pythons or librenpython:
        probe.engine = "runtime-only"
        probe.evidence.append("Ren'Py runtime without renpy/ or renpy.pyd")
    if librenpython:
        probe.evidence.append("librenpython.dll present")
    if pythons:
        probe.evidence.append(", ".join(sorted({p.name for p in pythons})[:3]))

    # --- archives: content first, extensions as a weaker fallback ----------
    search_root = probe.game_dir or root
    probe.archives = find_archives(search_root)
    if probe.archives:
        probe.evidence.append("%d archive(s) recognised by content" % len(probe.archives))
    seen = {p.resolve() for p in probe.archives}
    for path in search_root.rglob("*"):
        if not path.is_file() or path.resolve() in seen:
            continue
        if path.suffix.lower() in _ARCHIVE_SUFFIXES and path.stat().st_size >= 4096:
            probe.archive_candidates.append(path)
    if probe.archive_candidates:
        probe.notes.append(
            "%d file(s) look like archives by extension only -- a renamed or custom "
            "format: %s" % (len(probe.archive_candidates),
                            ", ".join(sorted({p.suffix.lower() for p in probe.archive_candidates}))))

    # --- what each backend needs -------------------------------------------
    launchers = [p for p in root.glob("*.py") if p.name.lower() not in
                 ("renpy.py", "setup.py")]
    exe_stems = {p.stem.lower() for p in root.glob("*.exe")}
    launchers.sort(key=lambda p: (p.stem.lower() != root.name.lower(),
                                  p.stem.lower() not in exe_stems, p.name))
    launcher_py = launchers
    renpy_py = (root / "renpy.py").is_file()

    if probe.engine == "source":
        probe.backends.append(Backend("static", "yes",
                                      "the game ships renpy/loader.py; that file is the "
                                      "authority on its own archive format"))
    else:
        probe.backends.append(Backend(
            "static", "no",
            "no renpy/loader.py -- the archive format only exists inside the "
            "compiled engine" if probe.engine == "compiled-pyd"
            else "no readable loader.py to run"))

    if launcher_py and pythons:
        probe.backends.append(Backend("runtime", "yes",
                                      "launcher %s.py plus a bundled interpreter"
                                      % launcher_py[0].stem))
    elif launcher_py:
        probe.backends.append(Backend("runtime", "unknown",
                                      "launcher found but no bundled python to drive it"))
    else:
        probe.backends.append(Backend("runtime", "no",
                                      "no <Game>.py launcher: the engine is started by the "
                                      "exe itself"))

    if probe.engine == "source" and probe.game_dir:
        probe.backends.append(Backend("inject", "unknown",
                                      "needs the game to compile a loose .rpy, which a "
                                      "guarded build may skip or reject"))
    else:
        probe.backends.append(Backend("inject", "no",
                                      "requires a loose-.rpy compile step from a source engine"))

    if renpy_py:
        probe.backends.append(Backend("env", "yes", "renpy.py present; RENPY_SEARCHPATH is "
                                                    "read there"))
    else:
        probe.backends.append(Backend("env", "no",
                                      "no renpy.py at the game root -- RENPY_SEARCHPATH is "
                                      "only read by that launcher script, so this approach "
                                      "cannot apply"))

    # The hook is for builds where nothing on disk describes the format: a compiled
    # engine.  A source build with renamed archives is still covered by its own
    # loader.py, so the hook is only a fallback there.
    hook_needed = probe.engine in ("compiled-pyd", "runtime-only")
    probe.backends.append(Backend(
        "hook", "yes" if hook_needed else "optional",
        "the engine must be started normally and our code runs inside it (Windows only, "
        "opt-in)" if hook_needed else
        "not needed: a file-based backend already covers this build"))

    return probe


def format_profile(profile: GameProfile) -> str:
    lines = [f"root    : {profile.root}"]
    lines.append(f"game/   : {profile.game_dir if profile.game_dir else '(none)'}")
    lines.append(f"engine  : {profile.engine}")
    if profile.evidence:
        lines.append("evidence: " + "; ".join(profile.evidence))
    lines.append(f"archives: {len(profile.archives)} recognised by content, "
                 f"{len(profile.archive_candidates)} by extension only")
    for note in profile.notes:
        lines.append("  note  : " + note)
    lines.append("backends:")
    for backend in profile.backends:
        lines.append("  %-8s %-8s %s" % (backend.name, backend.applicable, backend.reason))
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - manual inspection helper
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    print(format_profile(classify(target)))
