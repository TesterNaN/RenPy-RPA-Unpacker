"""Runtime shims that stand in for the Ren'Py internals the extracted code expects.

The AST extractor pulls real functions and classes out of a game's
``renpy/loader.py``.  Some of the names those definitions reference are supplied
by *other* Ren'Py modules and therefore cannot be extracted from ``loader.py``:

* ``RWopsIO``  -- imported from ``renpy.pygame.rwobject``.  It is a
  ``io.RawIOBase`` facade over a ``(base, length)`` window of a real file.  This
  is what makes RPAv3 split-segment archives (where the file head is embedded in
  the index pickle) work.
* ``loads``    -- imported from ``renpy.compat.pickle``.  In Ren'Py this is a
  ``pickle.loads`` wrapper whose main job is Python 2/3 byte-string
  compatibility.  We replace it with a hardened loader, because an RPA index is
  attacker-controlled pickle data.
* ``renpy``    -- the global game object.  The extracted ``index_archives`` only
  touches ``renpy.config.archives``.

Names that *are* definable from ``loader.py`` are never sourced from here: the
extractor prefers the real thing.  These shims are pruned per build so the
generated module contains only what the extracted code actually needs.
"""

from __future__ import annotations

import builtins
import hashlib
import io
import pickle

__all__ = ["RWopsIO", "make_loads", "RenpyConfig", "make_renpy", "pickle_loads"]


#: Python 2 spelled the builtins module ``__builtin__`` (and kept ``builtin`` as
#: an alias), so archives built by Ren'Py 6 pickle their objects under those
#: names.  Without mapping them, ``__builtin__.bytes`` is rejected as unknown.
_PY2_MODULE_ALIASES = {
    "__builtin__": "builtins",
    "builtin": "builtins",
    "copy_reg": "copyreg",
}


class UnsupportedPickleError(pickle.UnpicklingError):
    """Raised when an archive index tries to unpickle a disallowed type."""


#: Everything a legitimate RPA index can possibly contain.  An index maps a
#: filename (``str``) to a list of ``(offset, length)`` or
#: ``(offset, length, head_bytes)`` tuples -- nothing more exotic than that.
_SAFE_GLOBALS = frozenset(
    {
        "builtins.dict",
        "builtins.list",
        "builtins.tuple",
        "builtins.set",
        "builtins.frozenset",
        "builtins.bytearray",
        "builtins.bytes",
        "builtins.str",
        "builtins.int",
        "builtins.float",
        "builtins.bool",
        "builtins.complex",
        "collections.OrderedDict",
        "collections.defaultdict",
    }
)

#: Codec helpers that pickle itself emits when a `bytes` object is round-tripped
#: through protocol 2.  Verified against real archives: Python 2 indexes store
#: their keys as ``str``, which pickle reconstructs with
#: ``_codecs.encode(value, "latin1")``, and RPAv3 split entries store each file's
#: head the same way.  Without these a valid archive is rejected.
#:
#: Allowed deliberately and narrowly: they resolve a codec by name and return
#: bytes.  They cannot reach the filesystem, spawn a process, or import an
#: arbitrary module -- the codec name comes from the pickle, but the lookup goes
#: through the codec registry, so an unknown name is merely an error.
_CODEC_GLOBALS = frozenset(
    {
        "_codecs.encode",
        "_codecs.decode",
        "codecs.encode",
        "codecs.decode",
    }
)

_SAFE_GLOBALS = _SAFE_GLOBALS | _CODEC_GLOBALS

# Python 2 archives are still out there, and ``encoding="bytes"`` asks pickle to
# hand back the raw Python 2 ``str`` as ``bytes`` rather than guessing a codec.
_PICKLE_ENCODINGS = ("bytes", "latin-1")


def pickle_loads(data, *, unsafe: bool = False):
    """Unpickle *data*, refusing to import arbitrary objects unless *unsafe*.

    A malicious ``.rpa`` can ship a pickle whose opcodes construct a dangerous
    object (think ``os.system`` via ``__reduce__``).  Plain ``pickle.loads``
    would happily execute that during what the user believes is a read-only
    unpack.  The restricted unpickler below only permits plain containers.
    """
    if unsafe:
        return _plain_loads(data)

    last_error = None
    for encoding in _PICKLE_ENCODINGS:
        try:
            return _RestrictedUnpickler(io.BytesIO(data), encoding=encoding).load()
        except UnsupportedPickleError:
            raise
        except Exception as exc:  # wrong codec for this archive; try the next
            last_error = exc
    if last_error is not None:
        raise last_error
    return None  # pragma: no cover - _PICKLE_ENCODINGS is non-empty


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        # Normalise Python 2 module spellings before consulting the allowlist, so
        # a legacy archive resolves to the Python 3 equivalent instead of being
        # refused as an unknown global.
        module = _PY2_MODULE_ALIASES.get(module, module)
        qualified = f"{module}.{name}"
        if qualified in _SAFE_GLOBALS:
            try:
                return super().find_class(module, name)
            except (ImportError, AttributeError) as exc:
                # e.g. copyreg._reconstructor is gone in Python 3.12+.
                raise UnsupportedPickleError(
                    f"{qualified!r} is not available in this Python: {exc}"
                ) from None
        raise UnsupportedPickleError(
            f"refusing to unpickle {qualified!r} while reading an archive index; "
            f"pass --unsafe-pickle if you trust this archive"
        )

    # Reject the out-of-band buffer protocol too: it lets a malicious index feed
    # arbitrary bytes into an object's constructor.
    def persistent_load(self, pid):
        raise UnsupportedPickleError(
            "refusing persistent_load() while reading an archive index"
        )


def _plain_loads(data):
    last_error = None
    for encoding in _PICKLE_ENCODINGS:
        try:
            return pickle.loads(data, encoding=encoding)
        except Exception as exc:
            last_error = exc
    raise last_error if last_error is not None else ValueError("empty index")


def make_loads(unsafe: bool = False):
    """Build the ``loads`` replacement injected into the generated module."""

    def loads(data):
        return pickle_loads(data, unsafe=unsafe)

    loads.__doc__ = "Replacement for renpy.compat.pickle.loads."
    return loads


class RenpyConfig:
    """Stands in for ``renpy.config``, limited to what the reading path touches.

    ``index_archives`` only needs ``archives``.  Reading a *loose* file goes through
    ``loader.load()``, which resolves the name via ``transfn`` -- and that needs
    ``basedir`` and ``searchpath`` to find anything on disk.
    """

    def __init__(self, basedir: str = "", searchpath: list[str] | None = None):
        self.archives = []
        self.basedir = basedir
        self.searchpath = list(searchpath) if searchpath is not None else ["game"]
        self.search_prefixes = [""]
        self.tl_directory = "tl"
        self.reject_backslash = False
        # load_from_archive reads this; False means loose files win, which is
        # Ren'Py's normal behaviour and what we want to mirror.
        self.force_archives = False


class _RenpyObject:
    """Stands in for ``renpy.object``, which loader.py uses for sentinels."""

    class Sentinel:
        def __init__(self, name: str = "sentinel"):
            self.name = name

        def __repr__(self) -> str:
            return f"<{self.name}>"


class _Renpy:
    """Stands in for the ``renpy`` module object."""

    def __init__(self):
        self.config = RenpyConfig()
        self.object = _RenpyObject()
        self.emscripten = False
        # transfn() -> add_auto() reads this.  It must stay False: enabling it
        # would make add_auto start file-watching threads.
        self.autoreload = False


def make_renpy():
    return _Renpy()


class RWopsIO(io.RawIOBase):
    """A read-only window over ``[base, base + length)`` of a file.

    Replaces ``renpy.pygame.rwobject.RWopsIO``.  Ren'Py 6.x shipped a version of
    this class in ``loader.py`` itself; the extractor uses that one when it is
    present and only falls back here otherwise, so no code path needed to make
    the original work is re-implemented.
    """

    def __init__(self, filename, mode="rb", base=0, length=None):
        super().__init__()
        self.filename = filename
        self.mode = mode
        self.base = base
        self.length = length
        self.file = None
        self.position = 0
        self.total_size = None

    @staticmethod
    def from_buffer(data, name=""):
        return io.BytesIO(data)

    @staticmethod
    def from_split(a, b, name=""):
        return io.BytesIO(a.read() + b.read())

    def _open_file(self):
        if self.file is None:
            self.file = open(self.filename, self.mode)
            if self.base:
                self.file.seek(self.base)
            if self.length is None:
                current = self.file.tell()
                self.file.seek(0, 2)
                self.total_size = self.file.tell() - self.base
                self.file.seek(current)
            else:
                self.total_size = self.length
        return self.file

    def readable(self):
        return True

    def writable(self):
        return False

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        handle = self._open_file()

        if whence == 0:
            new_pos = offset
        elif whence == 1:
            new_pos = self.position + offset
        elif whence == 2:
            new_pos = (self.total_size or 0) + offset
        else:
            raise ValueError(f"invalid whence value: {whence!r}")

        if new_pos < 0:
            new_pos = 0
        if self.total_size is not None and new_pos > self.total_size:
            new_pos = self.total_size

        self.position = new_pos
        handle.seek(self.base + new_pos)
        return self.position

    def read(self, size=-1):
        handle = self._open_file()

        if self.total_size is not None:
            remaining = self.total_size - self.position
            if remaining <= 0:
                return b""
            if size is None or size < 0 or size > remaining:
                size = remaining
        elif size is None:
            size = -1

        if size == 0:
            return b""

        data = handle.read(size)
        self.position += len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        count = len(data)
        buffer[:count] = data
        return count

    def close(self):
        try:
            if self.file is not None:
                self.file.close()
                self.file = None
        finally:
            super().close()


def shim_fingerprint():
    """Short digest of this module's source, used to name generated modules."""
    try:
        with open(__file__, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:12]
    except OSError:  # pragma: no cover - source-less install
        return "unknown"


if __name__ == "__main__":  # pragma: no cover - manual smoke check
    import sys
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        tmp.write(b"0123456789abcdefghij")
        path = tmp.name

    window = io.BufferedReader(RWopsIO(path, "rb", base=5, length=7), buffering=4096)
    assert window.read() == b"56789ab", window.read()
    assert list(_RestrictedUnpickler.__mro__)
    print("runtime_shims ok", shim_fingerprint(), file=sys.stderr)
