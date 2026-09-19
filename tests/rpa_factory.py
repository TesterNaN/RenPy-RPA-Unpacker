"""Synthetic RPA archive builders for the test suite.

There is nothing Ren'Py-specific about a ``.rpa`` file: a header naming the index
offset, a blob of member data, and a zlib-compressed pickle index.  Building them
here lets the tests cover RPAv1/2/3, XOR-obfuscated indexes and split segments
without shipping a copyrighted game.  The real Ren'Py readers extracted from the
SDK's ``loader.py`` are then run against them, so the tests validate the whole
pipeline rather than a mock.
"""

from __future__ import annotations

import pickle
import random
import zlib
from pathlib import Path

#: A loader.py carrying an extra, non-standard archive format.
#:
#: Modelled on a real release whose loader registers an AES-encrypted format
#: alongside the stock v1/v2/v3 handlers.  Two things about it matter:
#:
#: * the extra handler is reachable *only* through its `archive_handlers.append`
#:   call, so a dependency walk from `index_archives` never finds it;
#: * the module also calls `file_open_callbacks.append(...)`, which a naive
#:   "any .append() of a class" matcher wrongly counts as a handler.
EXTRA_HANDLER_LOADER = '''
import io
import zlib

loads = __import__("pickle").loads

file_open_callbacks = []


class ArchiveHandlers(object):
    def __init__(self):
        self.exts = {}
        self.peek = {}

    def append(self, handler):
        candidates = []
        header_sizes = []
        for header in handler.get_supported_headers():
            candidates.append((header, handler))
            header_sizes.append(len(header))
        peek = max(header_sizes)
        for ext in handler.get_supported_extensions():
            self.exts.setdefault(ext, []).extend(candidates)
            self.peek[ext] = max(self.peek.get(ext, 0), peek)

    def spec(self, ext):
        return self.peek[ext], self.exts[ext]


archive_handlers = ArchiveHandlers()


class ZXEncryptedArchiveHandler(object):
    """A made-up encrypted format, registered first so it wins the header sniff."""

    @staticmethod
    def get_supported_extensions():
        return [".rpa"]

    @staticmethod
    def get_supported_headers():
        return [b"ZX-1.0"]

    @staticmethod
    def read_index(infile):
        return loads(zlib.decompress(infile.read(40)))


archive_handlers.append(ZXEncryptedArchiveHandler)


class RPAv3ArchiveHandler(object):
    @staticmethod
    def get_supported_extensions():
        return [".rpa"]

    @staticmethod
    def get_supported_headers():
        return [b"RPA-3.0 "]

    @staticmethod
    def read_index(infile):
        l = infile.read(40)
        offset = int(l[8:24], 16)
        key = int(l[25:33], 16)
        infile.seek(offset)
        index = loads(zlib.decompress(infile.read()))
        for k in index.keys():
            index[k] = [(o ^ key, d ^ key) for o, d in index[k]]
        return index


archive_handlers.append(RPAv3ArchiveHandler)


def load_from_filesystem(name):
    return None


file_open_callbacks.append(load_from_filesystem)

arc_files = []
archives = []


def index_archives():
    arc_files.sort(reverse=True)
    archives.clear()

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


def load_from_archive(name):
    for afn, index in archives:
        if name not in index:
            continue
        offset, dlen = index[name][0]
        rv = RWopsIO(afn, "rb", base=offset, length=dlen)
        return io.BufferedReader(rv)
    return None
'''

#: A loader whose archives are renamed to disguise them as system libraries.
#:
#: Modelled on a real release whose ``game/`` directory contains no ``.rpa`` at
#: all: the archives are called ``mfplat.dll``, ``vcruntime140.dll`` and so on,
#: sitting next to genuine ``steam_api.dll`` files.  Two details matter --
#: the handler *declares* ``.dll``, and the index tuples have their fields swapped
#: while unpacking.
DISGUISED_LOADER = '''
import io
import zlib
import pickle

loads = pickle.loads


class ArchiveHandlers(object):
    def __init__(self):
        self.exts = {}
        self.peek = {}

    def append(self, handler):
        candidates = []
        header_sizes = []
        for header in handler.get_supported_headers():
            candidates.append((header, handler))
            header_sizes.append(len(header))
        peek = max(header_sizes)
        for ext in handler.get_supported_extensions():
            self.exts.setdefault(ext, []).extend(candidates)
            self.peek[ext] = max(self.peek.get(ext, 0), peek)

    def spec(self, ext):
        return self.peek[ext], self.exts[ext]


archive_handlers = ArchiveHandlers()


class RPAv3ArchiveHandler(object):
    """Archives look like DLLs and start with ILOVEYOU instead of RPA-3.0."""

    archive_extension = ".dll"

    @staticmethod
    def get_supported_extensions():
        return [".dll"]

    @staticmethod
    def get_supported_headers():
        return [b"ILOVEYOU"]

    @staticmethod
    def read_index(infile):
        l = infile.read(40)
        offset = int(l[8:24], 16)
        key = int(l[25:33], 16)
        infile.seek(offset)
        index = loads(zlib.decompress(infile.read()))

        for k in index.keys():
            # NOTE: the shipping build swaps the two fields while unpacking.
            index[k] = [(offset ^ key, dlen ^ key) for dlen, offset in index[k]]

        return index


archive_handlers.append(RPAv3ArchiveHandler)


class RPAv1ArchiveHandler(object):
    @staticmethod
    def get_supported_extensions():
        return [".rpi"]

    @staticmethod
    def get_supported_headers():
        return [b"x\\x9c"]

    @staticmethod
    def read_index(infile):
        return loads(zlib.decompress(infile.read()))


archive_handlers.append(RPAv1ArchiveHandler)

arc_files = []
archives = []


def index_archives():
    arc_files.sort(reverse=True)
    archives.clear()

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


def load_from_archive(name):
    for afn, index in archives:
        if name not in index:
            continue
        offset, dlen = index[name][0]
        rv = RWopsIO(afn, "rb", base=offset, length=dlen)
        return io.BufferedReader(rv)
    return None
'''

#: Header size used by :func:`write_disguised_dll`.  The obfuscated reader in the
#: real loader takes ``l[8:24]`` / ``l[25:33]`` out of a **40-byte** read, so the
#: text header is padded to that -- the same shape stock RPAv3 has.
_DISGUISED_HEADER = 40


def write_disguised_dll(path: Path, files: dict[str, bytes]) -> Path:
    """Write an archive disguised as a Windows DLL.

    The 8-byte ``RPA-3.0 `` magic becomes ``ILOVEYOU`` -- the same length, so every
    field keeps its offset -- and the index tuples store the length and offset in
    the opposite order from stock RPAv3.
    """
    key = 0x42424242
    data = bytearray()
    layout: list[tuple[str, int, int]] = []
    for name, payload in files.items():
        layout.append((name, _DISGUISED_HEADER + len(data), len(payload)))
        data.extend(payload)

    index = {name: [(length ^ key, offset ^ key)] for name, offset, length in layout}
    packed = zlib.compress(pickle.dumps(index, protocol=2))

    # The field layout the reader slices for, padded out to its 40-byte read:
    # 8 magic + space + 16 hex offset + space + 8 hex key, then padding.
    fields = ("ILOVEYOU%016x %08x" % (_DISGUISED_HEADER + len(data), key)).encode("ascii")
    header = fields.ljust(_DISGUISED_HEADER - 1, b" ") + b"\n"
    assert len(header) == _DISGUISED_HEADER, len(header)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(data) + packed)
    return path


def write_fake_system_dll(path: Path) -> Path:
    """Write a genuine-looking library that must NOT be treated as an archive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ\x90\x00" + b"\x00" * 1024)
    return path


__all__ = [
    "DISGUISED_LOADER",
    "EXTRA_HANDLER_LOADER",
    "OBFUSCATED_LOADER",
    "OBFUSCATION_DELTA",
    "write_disguised_dll",
    "write_fake_system_dll",
    "write_obfuscated_rpa3",
    "write_rpa1",
    "write_rpa1_with_data",
    "write_rpa2",
    "write_rpa3",
    "write_rpa3_split",
    "write_python2_rpa3",
    "write_plain_pickle",
]


def _blob(files: dict[str, bytes], header_size: int = 0) -> tuple[bytes, list[tuple[str, int, int]]]:
    """Concatenate *files* and return ``(data, [(name, offset, length)])``.

    RPA index offsets are absolute positions in the archive file, so *header_size*
    (the length of the text header that will precede the data) is folded in here.
    """
    data = bytearray()
    layout: list[tuple[str, int, int]] = []
    for name, payload in files.items():
        layout.append((name, header_size + len(data), len(payload)))
        data.extend(payload)
    return bytes(data), layout


def write_rpa3(
    path: Path,
    files: dict[str, bytes],
    *,
    key: int | None = None,
    compresslevel: int = 6,
) -> Path:
    """Write an RPA-3.0 archive with an XOR-obfuscated index."""
    header_size = 34
    key = random.Random(1234).randrange(1, 0xFFFFFFFF) if key is None else key
    data, layout = _blob(files, header_size)

    index = {name: [(offset ^ key, length ^ key)] for name, offset, length in layout}
    packed = zlib.compress(pickle.dumps(index, protocol=2), compresslevel)

    index_offset = header_size + len(data)
    header = ("RPA-3.0 %016x %08x\n" % (index_offset, key)).encode("ascii")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + data + packed)
    return path


def write_rpa3_split(path: Path, files: dict[str, bytes], split: int = 8) -> Path:
    """Write an RPA-3.0 archive whose entries are split into two segments.

    A split entry stores the first *split* bytes of the file inside the index
    pickle itself, with the remainder in the data area.  This is the code path
    that needs ``RWopsIO.from_split``, and it is the reason the reader must come
    from Ren'Py rather than a naive re-implementation.
    """
    key = 0x5EED5EED
    header_size = 34
    data = bytearray()
    layout: list[tuple[str, int, int, bytes]] = []

    for name, payload in files.items():
        head = payload[:split]
        tail = payload[split:]
        layout.append((name, header_size + len(data), len(tail), head))
        data.extend(tail)

    index = {}
    for name, offset, length, head in layout:
        index[name] = [(offset ^ key, length ^ key, head)]

    packed = zlib.compress(pickle.dumps(index, protocol=2))
    header_size = 34
    index_offset = header_size + len(data)
    header = ("RPA-3.0 %016x %08x\n" % (index_offset, key)).encode("ascii")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + bytes(data) + packed)
    return path


def write_python2_rpa3(path: Path, files: dict[str, bytes]) -> Path:
    """Write an RPA-3.0 archive whose index pickle uses Python 2 byte strings.

    Many older games were archived with Python 2, so the index keys come back as
    ``bytes`` unless the unpickler is told ``encoding="bytes"``.  This verifies
    that compatibility path.
    """
    key = 0x12345678
    header_size = 34
    data, layout = _blob(files, header_size)

    # protocol 2 + py2-encoded str keys reproduces what Ren'Py 6 produced.
    index = {
        name.encode("utf-8"): [(offset ^ key, length ^ key)]
        for name, offset, length in layout
    }
    packed = zlib.compress(pickle.dumps(index, protocol=2))

    index_offset = header_size + len(data)
    header = ("RPA-3.0 %016x %08x\n" % (index_offset, key)).encode("ascii")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + data + packed)
    return path


#: Byte delta a repacked archive applies between the offset stored in the index
#: and the real data position.  Mirrors the offset-shift obfuscation seen in the
#: wild (see ``write_obfuscated_rpa3``).
OBFUSCATION_DELTA = 33


def write_obfuscated_rpa3(path: Path, files: dict[str, bytes]) -> Path:
    """Write an RPA-3.0 archive that only a *modified* loader can read.

    Models the repacking pattern found in real obfuscated releases:

    * the header's index-offset field is a decoy -- it holds the XOR key again
      instead of the true index position, so a standard loader seeks megabytes
      past the end of a 20 KB file and dies with a zlib error;
    * the header is padded out to the requested size and the index pickle is
      placed immediately after it, so the obfuscated ``read_index`` never seeks
      at all -- it just consumes "the rest of the stream";
    * every stored offset is shifted by ``OBFUSCATION_DELTA``, undone by an
      extra ``offset -= 33`` inside the modified ``load_from_archive``.

    The real 黄莓C archive stores ``offset ^ key`` where the decoy happens to
    equal ``key``, which makes a plain XOR self-inverse and lets a loader that
    never assigns ``offset`` still recover the original values.  This reproduces
    that arrangement exactly.
    """
    delta = OBFUSCATION_DELTA
    key = 0x42424242
    text_header = 34
    # The 黄莓C layout: the .rpa is index-only and the member data lives in a
    # companion file whose name is the archive's with ".rpa" replaced by "zip".
    # Stored offsets are absolute positions in that companion file, shifted by the
    # delta that the modified load_from_archive subtracts back off.
    companion = path.with_suffix(".zip")

    data = bytearray()
    layout: list[tuple[str, int, int]] = []
    for name, payload in files.items():
        # True offset in the companion file; the delta is folded in by the
        # encoding below, never here.
        layout.append((name, len(data), len(payload)))
        data.extend(payload)

    # Two layers, in this order:
    #   decode 1 (read_index)          : stored ^ key -> true offset + delta
    #   decode 2 (load_from_archive)   : -= delta     -> true offset
    index = {
        name: [(((offset + delta) ^ key), length ^ key)]
        for name, offset, length in layout
    }
    packed = zlib.compress(pickle.dumps(index, protocol=2))

    # Decoy: the index-offset field holds the key again.  Note it also happens to
    # sit `delta` above the companion file's size -- the same relationship the
    # real archive exhibits.
    header = ("RPA-3.0 %016x %08x\n" % (key, key)).encode("ascii")
    assert len(header) == text_header, len(header)

    # read_index() decompresses everything after a 40-byte read, so pad to 40.
    padding = b"\x00" * (40 - text_header)

    path.parent.mkdir(parents=True, exist_ok=True)
    companion.write_bytes(bytes(data))
    path.write_bytes(header + padding + packed)
    return path


#: A loader.py whose archive reading has been obfuscated the same way.
OBFUSCATED_LOADER = '''
import io
import zlib
import pickle

loads = pickle.loads


class ArchiveHandlers(object):
    def __init__(self):
        self.exts = {}
        self.peek = {}

    def append(self, handler):
        candidates = []
        header_sizes = []
        for header in handler.get_supported_headers():
            candidates.append((header, handler))
            header_sizes.append(len(header))
        peek = max(header_sizes)
        for ext in handler.get_supported_extensions():
            self.exts.setdefault(ext, []).extend(candidates)
            self.peek[ext] = max(self.peek.get(ext, 0), peek)

    def spec(self, ext):
        return self.peek[ext], self.exts[ext]


archive_handlers = ArchiveHandlers()


class RPAv3ArchiveHandler(object):
    @staticmethod
    def get_supported_extensions():
        return [".rpa"]

    @staticmethod
    def get_supported_headers():
        return [b"RPA-3.0 "]

    @staticmethod
    def read_index(infile):
        l = infile.read(40)
        key = int(l[25:33], 16)
        index = loads(zlib.decompress(infile.read()))

        # `offset` is deliberately never assigned: it is the key, reused to make
        # the XOR self-inverse.  A naive extractor that tried to *resolve* this
        # name would inject something wrong and corrupt every offset.
        for k in index.keys():
            if len(index[k][0]) == 2:
                index[k] = [(offset ^ key, dlen ^ key) for offset, dlen in index[k]]
            else:
                index[k] = [(offset ^ key, dlen ^ key) for offset, dlen, _s in index[k]]

        return index


archive_handlers.append(RPAv3ArchiveHandler)

arc_files = []
archives = []


def index_archives():
    arc_files.sort(reverse=True)
    archives.clear()

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


def load_from_archive(name):
    for afn, index in archives:
        if name not in index:
            continue
        # Members live in the companion file named by swapping the extension.
        afn = afn[:-3] + "zip"
        offset, dlen = index[name][0]
        offset -= 33
        rv = RWopsIO(afn, "rb", base=offset, length=dlen)
        return io.BufferedReader(rv)
    return None
'''


def write_rpa2(path: Path, files: dict[str, bytes]) -> Path:
    """Write an RPA-2.0 archive: plain index offsets, no obfuscation."""
    header_size = 24
    data, layout = _blob(files, header_size)
    index = {name: [(offset, length)] for name, offset, length in layout}
    packed = zlib.compress(pickle.dumps(index, protocol=2))

    index_offset = header_size + len(data)
    # Exactly 24 bytes: 8 magic + 16 hex digits.  RPAv2's read_index takes
    # l[8:] of a 24-byte read, so any trailing newline would shift the index.
    header = ("RPA-2.0 %016x" % index_offset).encode("ascii")
    assert len(header) == header_size, (len(header), header)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + data + packed)
    return path


def write_rpa1(path: Path, files: dict[str, bytes]) -> Path:
    """Write an RPAv1 ``.rpi`` index: a bare zlib stream of the index pickle.

    RPAv1 has no text header -- ``RPAv1ArchiveHandler`` sniffs the zlib magic
    ``\\x78\\x9c`` -- and it declares the ``.rpi`` extension.  Ren'Py decompresses
    from byte 0, so an ``.rpi`` carries the index and nothing else; the member
    data lives in a separate file.  Offsets in the index are therefore relative
    to that companion file, which is why this factory only supports listing.

    The default compression level matters: zlib level 6 emits the ``78 9c``
    header byte pair the sniffer looks for (level 1 emits ``78 01`` and level 9
    emits ``78 da``).
    """
    data, layout = _blob(files)
    index = {name: [(offset, length)] for name, offset, length in layout}
    packed = zlib.compress(pickle.dumps(index, protocol=2), 6)
    assert packed[:2] == b"\x78\x9c", packed[:2].hex()

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(packed)
    return path


def write_rpa1_with_data(path: Path, files: dict[str, bytes]) -> Path:
    """Write a non-standard ``.rpi`` with the member data prepended.

    Some repacked archives inline the data before the index.  That fails the
    magic-byte sniff at byte 0, so it is kept here to pin down the *unsupported*
    shape: the tool must report it rather than silently produce garbage.
    """
    data, layout = _blob(files)
    index = {name: [(offset, length)] for name, offset, length in layout}
    packed = zlib.compress(pickle.dumps(index, protocol=2), 6)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + packed)
    return path

def write_plain_pickle(path: Path, payload) -> Path:
    """Dump an arbitrary object as pickle data (for abuse-case tests)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pickle.dumps(payload, protocol=2))
    return path
