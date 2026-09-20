"""Fallback implementations for names the target ``loader.py`` may not define.

Normally every definition is recovered from the game's own Ren'Py source, so
extraction is exact.  These fallbacks only matter when a piece is missing
entirely -- an unusual loader, an obfuscated build, or an SDK trimmed down by a
packer.  Each name here mirrors what Ren'Py itself provides, so a partial
loader.py degrades into something usable instead of failing outright.

The bodies are returned by :func:`sources` as source text, which the AST
extractor splices into the generated module and prunes to only what is used.
"""

from __future__ import annotations

import ast
import functools
import inspect
import textwrap
from typing import Callable

__all__ = ["sources"]


def _source_of(func: Callable[..., object]) -> str:
    """Return *func*'s source with its own decorators stripped."""
    try:
        raw = inspect.getsource(func)
    except (OSError, TypeError):  # pragma: no cover - source-less install
        raise RuntimeError(
            f"cannot read the fallback source for {func.__name__!r}; "
            "the renpy_unpack package appears to be installed without .py files"
        ) from None
    lines = raw.splitlines()
    while lines and lines[0].lstrip().startswith("@"):
        lines.pop(0)
    return textwrap.dedent("\n".join(lines))


def _loads() -> str:
    """``renpy.compat.pickle.loads`` -- restricted, see runtime_shims."""
    return _source_of(_loads_impl)


def loads_factory(unsafe: bool = False) -> str:
    """Source for the ``loads`` shim, bound to a specific safety setting."""
    return "\n".join(
        [
            "def loads(data):",
            "    # renpy.compat.pickle.loads, replaced by a restricted unpickler:",
            "    # an archive index is attacker-controlled pickle data.",
            "    import renpy_unpack.runtime_shims as _renpy_unpack_shims",
            "",
            f"    return _renpy_unpack_shims.pickle_loads(data, unsafe={unsafe!r})",
        ]
    )


def _loads_impl(data):
    """Kept for reference; :func:`loads_factory` is what the extractor uses."""
    import renpy_unpack.runtime_shims as _shims

    return _shims.pickle_loads(data)


def _renpy() -> str:
    """The ``renpy`` global, of which only ``config.archives`` is ever touched.

    Emitted as a complete assignment: the bundled Ren'Py source happens to
    provide ``renpy`` as an import/global, so a loader.py without it is the one
    case where this shim matters, and it must leave ``renpy`` bound itself.
    """
    return "\n".join(
        [
            "class _ShimConfig:",
            "    def __init__(self):",
            "        self.archives = []",
            # Only what the *reading* path touches.  `index_archives` needs
            # `archives`; `load()`/`transfn` need `basedir`, `searchpath`,
            # `reject_backslash`, `search_prefixes` and `tl_directory`.  A full
            # renpy.config cannot be synthesised, so the shim stays limited to
            # these -- and anything added here must keep `searchpath` pointing at
            # the real game directory, since that is what locates loose files.
            "        self.basedir = ''",
            "        self.searchpath = ['game']",
            "        self.search_prefixes = ['']",
            "        self.tl_directory = 'tl'",
            "        self.reject_backslash = False",
            "        # load_from_archive reads this; False means loose files win, which",
            "        # is Ren'Py's normal behaviour and what we want to mirror.",
            "        self.force_archives = False",
            "",
            "    def __repr__(self):  # keeps the generated module debuggable",
            '        return "<renpy.config shim>"',
            "",
            "",
            "class _ShimDisplayPredict:",
            "    # load() refuses to open files while Ren'Py predicts; we never predict.",
            "    predicting = False",
            "",
            "",
            "class _ShimDisplay:",
            "    def __init__(self):",
            "        self.predict = _ShimDisplayPredict()",
            "",
            "",
            "class _ShimGamePreferences:",
            "    language = None",
            "",
            "",
            "class _ShimGame:",
            "    def __init__(self):",
            "        self.preferences = _ShimGamePreferences()",
            "",
            "",
            "class _ShimObject:",
            "    # loader.py builds sentinels at import time via renpy.object.Sentinel.",
            "    class Sentinel:",
            "        def __init__(self, name='sentinel'):",
            "            self.name = name",
            "",
            "        def __repr__(self):",
            "            return '<%s>' % self.name",
            "",
            "",
            "class _ShimRenpy:",
            "    def __init__(self):",
            "        self.config = _ShimConfig()",
            "        self.display = _ShimDisplay()",
            "        self.game = _ShimGame()",
            "        self.object = _ShimObject()",
            "        self.emscripten = False",
            "        # transfn() -> add_auto() reads this; autoreload is a developer",
            "        # feature and must stay off, or add_auto would start watching files.",
            "        self.autoreload = False",
            "",
            '    def __repr__(self):',
            '        return "<renpy module shim>"',
            "",
            "",
            "renpy = _ShimRenpy()",
        ]
    )


def _RenpyConfig() -> str:
    """A standalone config class, for loaders that construct one directly."""
    return "\n".join(
        [
            "class RenpyConfig:",
            "    def __init__(self):",
            "        self.archives = []",
        ]
    )


def _RWopsIO() -> str:
    """``renpy.pygame.rwobject.RWopsIO`` -- the windowed file facade.

    Ren'Py 6.x defined this class inside ``loader.py`` (and the extractor picks
    that one up when present); later versions import it from
    ``renpy.pygame.rwobject``.  This binds the standalone replacement.
    """
    return "\n".join(
        [
            "from renpy_unpack.runtime_shims import RWopsIO",
        ]
    )


def _index_archives() -> str:
    """``loader.index_archives``, for loaders where it could not be found."""
    return _source_of(_index_archives_impl)


def _index_archives_impl():
    arc_files.sort(reverse=True)

    archives.clear()
    renpy.config.archives.clear()

    for stem, ext, fn in arc_files:
        with open(fn, "rb") as f:
            peek, handlers = archive_handlers.spec(ext)
            file_header = f.read(peek)

            for header, handler in handlers:
                if not file_header.startswith(header):
                    continue

                f.seek(0, 0)
                index = handler.read_index(f)
                archives.append((fn, index))
                break

        renpy.config.archives.append(stem)


def _load_from_archive() -> str:
    """``loader.load_from_archive``, for loaders where it could not be found."""
    return _source_of(_load_from_archive_impl)


def _load_from_archive_impl():
    for afn, index in archives:
        if name not in index:
            continue

        data = []

        # Direct path.
        if len(index[name]) == 1:
            t = index[name][0]
            if len(t) == 2:
                offset, dlen = t
                start = b""
            else:
                offset, dlen, start = t

            if start is None or len(start) == 0:
                rv = RWopsIO(afn, "rb", base=offset, length=dlen)
                return io.BufferedReader(rv, buffering=65536)
            else:
                a = RWopsIO.from_buffer(start, name=name)
                b = RWopsIO(afn, "rb", base=offset, length=dlen)
                rv = RWopsIO.from_split(a, b, name=name)

        # Compatibility path: one file split across several segments.
        else:
            with open(afn, "rb") as f:
                for offset, dlen in index[name]:
                    f.seek(offset)
                    data.append(f.read(dlen))

                return io.BufferedReader(RWopsIO.from_buffer(b"".join(data), name=name))

    return None


#: The Python-3 branch of ``renpy/compat/__init__.py``, which older loaders pull
#: names out of with ``from renpy.compat import ... unicode ...``.
#:
#: These have to be shimmed rather than imported: the generated module is meant to
#: stand alone, and the game's ``renpy`` package is not importable from here.  The
#: definitions are the obvious Python-3 equivalents of the module's Python-2
#: aliases; ``chr``, ``open``, ``range``, ``round`` and ``str`` also come from that
#: import line but are builtins, so they are never requested.
_COMPAT_SOURCE = '''\
PY2 = False

basestring = str

pystr = str

unicode = str


def bchr(n):
    return bytes([n])


def bord(s):
    if isinstance(s, (bytes, bytearray)):
        return s[0]
    return ord(s)


def tobytes(s):
    if isinstance(s, bytes):
        return s
    return s.encode("utf-8")
'''


def _compat_shims() -> dict[str, Callable[[], str]]:
    """Per-name shims for the ``renpy.compat`` aliases a loader may reference."""
    shims: dict[str, Callable[[], str]] = {}
    for name in ("PY2", "basestring", "pystr", "unicode", "bchr", "bord", "tobytes"):
        shims[name] = functools.partial(_compat_one, name)
    return shims


def _compat_one(name: str) -> str:
    """The slice of :data:`_COMPAT_SOURCE` that defines *name*."""
    module = ast.parse(_COMPAT_SOURCE)
    for node in module.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return ast.unparse(node)
        elif isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise KeyError(name)  # pragma: no cover - the names above are all present


#: name -> zero-argument callable returning source text.
_REGISTRY: dict[str, Callable[[], str]] = {
    "loads": _loads,
    "renpy": _renpy,
    "RenpyConfig": _RenpyConfig,
    "RWopsIO": _RWopsIO,
    "index_archives": _index_archives,
    "load_from_archive": _load_from_archive,
    **_compat_shims(),
}

#: Names the extractor must always source from the shims, never from loader.py.
#: Ren'Py binds these to *other modules* (``renpy.pygame.rwobject``,
#: ``renpy.compat.pickle``) or to a live game runtime.  Picking up a stray local
#: definition of one of these would produce a module that cannot run standalone.
FORCED_SHIMS = frozenset({"loads", "renpy", "RWopsIO", "RenpyConfig"})


def sources(unsafe_pickle: bool = False) -> dict[str, Callable[[], str]]:
    """Return the fallback registry, with ``loads`` bound to *unsafe_pickle*."""
    registry = dict(_REGISTRY)
    registry["loads"] = lambda: loads_factory(unsafe_pickle)
    return registry
