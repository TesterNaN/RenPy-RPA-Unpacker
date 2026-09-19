"""The probe that runs *inside* the game's own Ren'Py runtime.

Invoked by the game's bundled interpreter as::

    <game>/lib/py*/python[.exe] -c "<this file's source>" <game_root> [name...]

It is deliberately free of any ``import renpy_unpack``: it runs in a Ren'Py
process whose ``sys.path`` we do not control, and the protocol is trivial enough
that a small, self-contained script is more robust than a package import.
``runtime.py`` owns the parent side and passes this source through ``-c`` so
**nothing is ever written into the game directory**.

Why this exists at all
----------------------
AST extraction reads ``loader.py`` as source, which is safe and proven, but it
cannot do anything about decryption implemented in a *compiled* module.  One real
game ships ``renpy.aescrypt`` built into a ``.dll`` and uses it for an
AES-256-CTR archive format; the only way to read those archives is to let the
game's own runtime do the decrypting.  This probe is that path.

Protocol
--------
Everything goes over stdout, so no scratch file is needed and the backend works on
a read-only install.  A UTF-8 text header, then length-prefixed binary frames:

* ``RPA-PROBE-1``              protocol banner
* ``ok``                       status; ``error`` is followed by a traceback block
* ``<n>``                      archive count
* ``aescrypt\\t<0|1>``           whether ``renpy.aescrypt`` imported
* ``<attrs>``                  comma-separated names (empty when unavailable)
* ``<n>``                      total index entry count
* ``<archive>\\t<name>\\t<size>`` x n
* ``DATA``                     end of header
* then, per requested file::

      FILE <name> <size>
      <size raw bytes>

  or ``MISSING <name>``
* terminated by ``END``

Header *lines* are escaped so a name containing a newline cannot derail parsing
(Ren'Py filenames cannot contain one, but the payload is attacker-influenced and
the parser is not the place to find out).
"""

from __future__ import annotations

import os
import sys
import traceback
import types
from pathlib import Path

BANNER = "RPA-PROBE-1"
DATA = "DATA"
END = "END"


def _find_launcher(game: Path) -> Path:
    """The bundled launcher .py, which carries Ren'Py's distributor hooks.

    ``renpy.bootstrap`` insists on ``renpy.__main__.path_to_gamedir`` and
    ``path_to_logdir``; those live in the game's own launcher script, so we read it
    from disk instead of guessing.  Its ``main()`` is guarded by ``__name__``, so
    executing it only defines the hooks.

    Selection is by the hooks themselves, not by filename: one real game's folder
    also contains unrelated ``.py`` files left behind by other unpackers, and a
    name-based guess picked one of those and then failed.
    """
    candidates = [
        path
        for path in sorted(game.glob("*.py"))
        if path.name not in ("renpy.py", "_renpy_probe.py")
    ]
    if not candidates:
        raise RuntimeError(f"no launcher .py found in {game}")

    for path in candidates:
        try:
            source = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "def path_to_gamedir" in source or "def path_to_renpy_base" in source:
            return path
    return candidates[0]


def _install_hooks(game: Path) -> str:
    import renpy

    launcher = _find_launcher(game)
    hooks = types.ModuleType("renpy.__main__")
    hooks.__file__ = str(launcher)
    exec(compile(launcher.read_bytes(), str(launcher), "exec"), hooks.__dict__)
    renpy.__main__ = hooks
    return launcher.name


class _ReadyToRead(Exception):
    """Raised to stop the runtime the moment the archive machinery is importable."""


def _stop_at_loader(exc_type):
    """Interrupt bootstrap as soon as ``renpy.loader`` finishes importing.

    Bootstrap calls ``renpy.import_all()`` -- which imports the loader -- and only
    afterwards calls ``renpy.main.main()``, which loads the game's scripts.  That
    script step is the fragile one: one real release cannot start at all, because a
    previous unpack left ``game/extracted/`` beside ``game/rpy/`` and Ren'Py then
    declares every script "defined twice".  Its archives are readable regardless.

    Wrapping ``renpy.main.main`` does *not* work: Ren'Py pickles it for rollback,
    and a closure cannot be pickled (``Cannot pickle renpy.main.main``).  A trace
    hook leaves nothing behind instead -- the exception is raised from inside the
    ``import`` statement, so control simply leaves bootstrap with nothing patched.

    *exc_type* is passed in and used explicitly rather than being looked up as a
    name: this source is run via ``-c``, whose locals are not the globals a trace
    callback sees, so a bare global reference inside the tracer raises
    ``NameError`` at exactly the wrong moment.
    """
    import sys

    def tracer(frame, event, arg):
        if event == "call":
            loader = sys.modules.get("renpy.loader")
            if loader is not None and hasattr(loader, "archive_handlers"):
                raise exc_type()
        return None

    sys.settrace(tracer)
    return tracer


def _boot(game: Path) -> None:
    """Bring the runtime up far enough that ``renpy.loader`` is usable.

    Stops *before* the game's scripts are loaded, so a game that cannot start is
    still readable at archive level -- and so that a game which *can* start is
    never actually played.  Reading someone's archives should not launch their
    game: that shows a window, runs their startup code, and can hang.

    That is not a theoretical concern.  Disabling this mechanism during testing
    (to check whether it was still needed) made the tool boot the game over and
    over, because the change was left in place.  So the probe now *verifies* that
    it regained control, and refuses to continue otherwise: losing the early stop
    must fail loudly rather than silently become "play the game".
    """
    import sys

    import renpy.bootstrap

    took_control = False
    tracer = _stop_at_loader(_ReadyToRead)
    try:
        renpy.bootstrap.bootstrap(str(game))
        # bootstrap() returning normally means the game ran to completion. That is
        # not an acceptable outcome for a read-only tool.
    except _ReadyToRead:
        took_control = True
    except SystemExit:
        # Some builds exit from argument parsing instead; the runtime is still live,
        # but we never got the explicit hand-off. Only a warning, not a failure:
        # this path is normal on some builds.
        pass
    finally:
        sys.settrace(None)
        del tracer

    if not took_control and not _loader_is_ready():
        raise RuntimeError(
            "could not take control of the game's runtime before it started "
            "playing. Refusing to continue: continuing would mean running the "
            "game rather than reading its archives."
        )


def _loader_is_ready() -> bool:
    import sys

    loader = sys.modules.get("renpy.loader")
    return loader is not None and hasattr(loader, "archive_handlers")


def _index(game: Path):
    """Make sure the archives are indexed, using the loader's own extension list.

    ``arc_files`` is normally filled by Ren'Py's directory scan, which asks each
    handler for ``get_supported_extensions()``.  When it is empty we have to supply
    the entries ourselves -- and doing that with a hardcoded ``*.rpa`` glob is wrong
    for exactly the reason this tool exists: one real game calls its archives
    ``.dll``, so a hardcoded scan finds nothing and reports zero entries.

    The handler registry is the authority, and it is live in this process.
    """
    import renpy.loader as loader

    if loader.archives:
        return loader

    extensions: list[str] = []
    # `exts` maps extension -> candidate handlers; its keys are what the handlers
    # actually accept, so it is the authority rather than a hardcoded list.
    try:
        extensions = list(loader.archive_handlers.exts.keys())
    except AttributeError:
        extensions = [".rpa", ".rpi"]

    loader.arc_files[:] = []
    for extension in extensions:
        for archive in sorted(game.glob(f"game/*{extension}"), reverse=True):
            loader.arc_files.append((archive.stem, extension, str(archive)))
    loader.index_archives()
    return loader


class Writer:
    """Line/binary writer over a *binary* stdout, buffered by the parent's pipe."""

    def __init__(self, stream):
        self.stream = stream
        self.text = b""

    def line(self, value: str = "") -> None:
        self.text += value.encode("utf-8", "backslashreplace") + b"\n"

    def flush_text(self) -> None:
        if self.text:
            self.stream.write(self.text)
            self.text = b""

    def raw(self, data: bytes) -> None:
        self.flush_text()
        self.stream.write(data)


def main(argv: list[str]) -> int:
    # Before *anything* imports the game's ``renpy`` package: importing it compiles
    # it, and CPython caches the bytecode into ``renpy/__pycache__/`` inside the
    # installation.  This backend advertises that it writes nothing into the game
    # directory, and for a long time it did not live up to that -- one run left 52
    # ``.pyc`` files in a real Steam install.  ``runtime.py`` also sets
    # ``PYTHONDONTWRITEBYTECODE``, which covers everything up to this line.
    sys.dont_write_bytecode = True

    out = sys.stdout.buffer

    if len(argv) < 1:
        out.write((BANNER + "\nerror\nprobe: usage: <game_root> [name ...]\n").encode())
        out.flush()
        return 2

    game = Path(argv[0]).resolve()
    requested = list(argv[1:])

    writer = Writer(out)
    writer.line(BANNER)

    try:
        sys.path.insert(0, str(game))

        _install_hooks(game)
        _boot(game)
        loader = _index(game)

        writer.line("ok")
        writer.line(str(len(loader.archives)))

        try:
            import renpy.aescrypt as aescrypt

            attrs = [name for name in dir(aescrypt) if not name.startswith("_")]
            writer.line("aescrypt\t1")
            writer.line(",".join(attrs))
        except BaseException:
            writer.line("aescrypt\t0")
            writer.line("")

        entries: list[tuple[str, str, int]] = []
        for archive_path, index in loader.archives:
            base = os.path.basename(archive_path)
            for name, parts in index.items():
                entries.append((base, name, sum(part[1] for part in parts)))

        writer.line(str(len(entries)))
        for archive_name, name, size in entries:
            writer.line(f"{archive_name}\t{name}\t{size}")

        writer.line(DATA)
        writer.flush_text()

        for name in requested:
            handle = loader.load_from_archive(name)
            if handle is None:
                writer.line(f"MISSING\t{name}")
                writer.flush_text()
                continue
            try:
                data = handle.read()
            finally:
                try:
                    handle.close()
                except BaseException:
                    pass
            writer.line(f"FILE\t{name}\t{len(data)}")
            writer.raw(data)

        writer.line(END)
        writer.flush_text()
    except BaseException:
        # Report into the same stream so the parent can surface it verbatim.
        writer.text = b""
        writer.line("error")
        for line in traceback.format_exc().splitlines():
            writer.line(line)
        writer.flush_text()

    try:
        out.flush()
    except BaseException:
        pass
    return 0


if __name__ == "__main__":
    # os._exit: Ren'Py leaves threads and an altered stdout behind, and normal
    # interpreter shutdown in that state can hang or dump to a dead stream.
    code = main(sys.argv[1:])
    try:
        sys.stderr.flush()
    except BaseException:
        pass
    os._exit(code)
