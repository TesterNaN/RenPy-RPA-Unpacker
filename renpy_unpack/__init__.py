"""renpy_unpack -- unpack Ren'Py ``.rpa`` archives using the game's own readers.

The interesting part is where the archive-parsing code comes from.  Rather than
re-implementing the RPA formats, this package reads the target game's
``renpy/loader.py``, locates the real ``RPAv1/v2/v3ArchiveHandler`` classes and
the ``index_archives`` / ``load_from_archive`` functions with :mod:`ast`, and
splices them into a self-contained module.  The game's own Ren'Py code does the
parsing, so every format quirk -- split segments, XOR-obfuscated indexes, Python
2 pickles -- stays handled.

Quick start::

    python -m renpy_unpack --game "D:\\Games\\SomeGame" --list
    python -m renpy_unpack --game "D:\\Games\\SomeGame" -o unpacked -j 16

See ``python -m renpy_unpack --help`` for the full option list.
"""

from __future__ import annotations

__version__ = "2.0.0"

__all__ = ["Unpacker", "UnpackError", "UnpackStats", "main", "build_reader", "__version__"]


def __getattr__(name: str):
    # Lazy re-exports: keeps `import renpy_unpack` cheap and lets the modules be
    # used without pulling in argparse machinery.
    if name in ("Unpacker", "UnpackError", "UnpackStats", "main", "build_reader"):
        from . import core

        return getattr(core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
