"""Read-only pre-flight: would a *stock*, self-contained reader work on these archives?

Nothing here decrypts anything or writes anything.  It answers one question -- "can a
reader of the official format produce the real bytes?" -- from three signals a blind
parse cannot fake:

* a header this module recognises, and index numbers that stay inside the file;
* an index that unpickles under the *restricted* unpickler (never plain ``pickle``,
  which would execute code from a hostile archive);
* sample members whose first bytes match what their names promise.

The verdict is about the *reader*, not the archive.  A game-modified archive coming back
"not stock-readable" is correct information, not a failure: the game's own ``loader.py``
is still what reads it, which is what the AST / runtime / inject backends are for.

Measured against real samples: archives whose magic is ``ILOVEYOU0000``, ``WJZ-4.9`` or
``.Made with R``, and one whose index fields are swapped, all come back "no".  A stock
RPAv1/v2/v3 file comes back "yes".
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .runtime_shims import pickle_loads

#: Headers Ren'Py itself declares, mapped to a family name.  RPA-1.0 has no header at
#: all: its index starts at offset 0 (the official reader keys it off ``.rpi`` instead).
HEADERS = {
    b"RPA-3.0 ": "RPA-3.0",
    b"RPA-3.2 ": "RPA-3.2",
    b"RPA-4.0 ": "RPA-4.0",
    b"ALT-1.0 ": "ALT-1.0",
    b"RPA-2.0 ": "RPA-2.0",
}

#: Header-bearing families that carry an XOR key in the header line.  The official
#: reader XORs the *index numbers* with it -- the member data itself is stored raw.
KEYED = ("RPA-3.0", "RPA-3.2", "RPA-4.0", "ALT-1.0")

#: (suffix, offset, magic) -- offset 8 for containers whose magic sits behind a length.
SIGNATURES = (
    (".png", 0, b"\x89PNG\r\n\x1a\n"),
    (".jpg", 0, b"\xff\xd8\xff"),
    (".jpeg", 0, b"\xff\xd8\xff"),
    (".webp", 8, b"WEBP"),
    (".ogg", 0, b"OggS"),
    (".opus", 0, b"OggS"),
    (".rpyc", 0, b"RENPY RPC2"),
    (".ttf", 0, b"\x00\x01\x00\x00"),
    (".otf", 0, b"OTTO"),
    (".webm", 0, b"\x1a\x45\xdf\xa3"),
    (".mp4", 4, b"ftyp"),
    (".zip", 0, b"PK\x03\x04"),
    (".wav", 8, b"WAVE"),
    (".mp3", 0, b"ID3"),
)

#: How much we are willing to read looking for an index, and how much we let zlib hand
#: back.  Both are what stops a fake offset (a real sample points 759 MB into a
#: companion file) from turning a probe into a disk-filler.
INDEX_READ_CAP = 64 * 1024 * 1024
INDEX_SIZE_CAP = 128 * 1024 * 1024
MAX_ENTRIES = 200_000
MAX_NAME = 512
SAMPLE_LIMIT = 6
SNIFF_MIN_SIZE = 512


@dataclass
class Sample:
    name: str
    verdict: str            # "ok" | "mismatch" | "unverifiable"
    detail: str = ""


@dataclass
class Archive:
    path: Path
    size: int
    header: bytes | None = None
    family: str | None = None
    entries: int | None = None
    index_ok: bool = False
    index_problem: str | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)

    @property
    def stock_readable(self) -> bool:
        """True only when every signal agrees: header, index, structure, samples."""
        if self.family is None or not self.index_ok or self.problems:
            return False
        if not self.samples:
            return False            # nothing verified: not a yes
        return all(s.verdict != "mismatch" for s in self.samples) and any(
            s.verdict == "ok" for s in self.samples)

    @property
    def summary(self) -> str:
        if self.family is None:
            shown = self.header or b""
            text = shown[:12].decode("latin1", "replace").strip() or "(empty)"
            return "header %r is not one Ren'Py declares" % text
        if not self.index_ok:
            return "%s: %s" % (self.family, self.index_problem or "index unreadable")
        if self.problems:
            return "%s: %s" % (self.family, self.problems[0])
        ok = sum(1 for s in self.samples if s.verdict == "ok")
        bad = sum(1 for s in self.samples if s.verdict == "mismatch")
        return "%s: %d entries, %d sample(s) verified, %d wrong" % (
            self.family, self.entries or 0, ok, bad)


def _signature(name: str):
    lower = name.lower()
    for suffix, offset, magic in SIGNATURES:
        if lower.endswith(suffix):
            return offset, magic
    return None


def looks_like_archive(path: Path) -> bool:
    """Content-based, never extension-based -- that is the point of the rename defence."""
    try:
        if not path.is_file() or path.stat().st_size < SNIFF_MIN_SIZE:
            return False
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    return head.startswith(tuple(HEADERS)) or path.suffix.lower() in (".rpa", ".rpi")


def find_archives(folder: Path, extra_paths=()) -> list[Path]:
    """Files to probe: what content sniffing finds, plus anything the caller already
    knows about.  ``extra_paths`` matters for archives whose magic *and* extension were
    both replaced -- only the game's own loader can point at those, and by then the
    question is not "is it there" but "would a stock reader manage it"."""
    found = {path for path in Path(folder).rglob("*") if looks_like_archive(path)}
    for path in extra_paths:
        path = Path(path)
        if path.is_file():
            found.add(path)
    return sorted(found)


def _read_index(path: Path, family: str, header: bytes) -> tuple[dict | None, str | None]:
    # The official reader takes 40 bytes for the keyed families and 24 for RPA-2.0;
    # splitting more than that swallows member data as if it were a hex field.
    length = 24 if family == "RPA-2.0" else 40
    text = header[:length].decode("latin1")
    parts = text.split()
    key = None
    if family == "RPA-2.0":
        try:
            offset = int(parts[1], 16)
        except (IndexError, ValueError):
            return None, "the header's index offset is not hex"
    elif family in KEYED:
        # The official reader slices l[8:24] and l[25:33]; splitting on whitespace
        # accepts the same thing and survives writers that pad differently.
        try:
            offset = int(parts[1], 16)
            key = int(parts[2], 16)
        except (IndexError, ValueError):
            return None, "the header's offset/key fields are not hex"
    else:
        offset = 0                       # RPA-1.0: the index starts at the file

    size = path.stat().st_size
    if not 0 <= offset < size:
        return None, "index offset %d is outside the file (%d bytes)" % (offset, size)

    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            blob = handle.read(min(size - offset, INDEX_READ_CAP))
    except OSError as exc:
        return None, "could not read the index: %s" % exc

    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(blob, INDEX_SIZE_CAP)
    except zlib.error as exc:
        return None, "the index is not zlib data (%s)" % str(exc)[:60]
    if decompressor.unconsumed_tail:
        return None, "the index is larger than %d bytes" % INDEX_SIZE_CAP

    try:
        index = pickle_loads(raw)
    except Exception as exc:                      # a hostile or corrupt pickle
        return None, "the index does not unpickle safely (%s)" % type(exc).__name__
    if not isinstance(index, dict):
        return None, "the index is a %s, not a dict" % type(index).__name__

    fixed_index = {}
    for name, entries in index.items():
        if isinstance(name, bytes):
            name = name.decode("latin-1")
        fixed = []
        for entry in entries:
            if len(entry) == 2:
                entry_offset, dlen = entry
                start = b""
            else:
                entry_offset, dlen, start = entry
            if key is not None:
                entry_offset ^= key
                dlen ^= key
            fixed.append((entry_offset, dlen, start if isinstance(start, bytes) else b""))
        fixed_index[name] = fixed
    return fixed_index, None


def probe_archive(path: Path) -> Archive:
    """Inspect one archive: a header, a bounded index, and a few member prefixes.

    Reads only, writes nothing, and never unpickles with plain ``pickle``.
    """
    path = Path(path)
    result = Archive(path=path, size=path.stat().st_size)

    with path.open("rb") as handle:
        head = handle.read(41)
    # Keep the bytes either way: "header b'ILOVEYOU0000' is not one Ren'Py declares" is
    # the single most useful line in the report.
    result.header = head[:16]
    for magic, family in HEADERS.items():
        if head.startswith(magic):
            result.header, result.family = magic, family
            break
    else:
        if head.startswith(b"\x78\x9c"):
            # RPAv1 ships no text header at all: the official handler sniffs the zlib
            # magic and declares .rpi.  The index sits at byte 0 and the members live in
            # a companion file, so nothing here can be sample-verified.
            result.family = "RPA-1.0"

    index, problem = _read_index(path, result.family or "RPA-1.0", head[:41])
    if index is None:
        result.index_problem = problem
        return result
    result.index_ok = True
    result.entries = len(index)
    if result.entries > MAX_ENTRIES:
        result.problems.append("the index claims %d entries" % result.entries)
        return result

    for name, entries in index.items():
        if not isinstance(name, str) or not name or "\x00" in name or len(name) > MAX_NAME:
            # Odd but not fatal: an empty name costs nobody anything.  Traversal and
            # out-of-range offsets below stay hard failures.
            result.notes.append("an entry name is not a usable string: %r" % (name,))
            continue
        if name.startswith(("/", "\\")) or ".." in name.replace("\\", "/").split("/"):
            result.problems.append("entry %r would escape the output directory" % name[:40])
            break
        for entry_offset, dlen, _start in entries:
            if not isinstance(entry_offset, int) or not isinstance(dlen, int):
                result.problems.append("entry %r has a non-integer offset/length" % name[:40])
                break
            if result.family == "RPA-1.0":
                # Its offsets are relative to the companion data file, not to this one,
                # so a range check here would be meaningless.
                continue
            if entry_offset < 0 or dlen < 0 or entry_offset + dlen > result.size:
                result.problems.append(
                    "entry %r points outside the file (%d+%d > %d)"
                    % (name[:40], entry_offset, dlen, result.size))
                break
        if result.problems:
            break

    if not result.problems:
        if result.family == "RPA-1.0":
            result.notes.append(
                "RPA-1.0 has no header; its offsets are relative to a companion data "
                "file, so neither the range check nor sampling applies here")
        else:
            result.samples = _sample(path, index)
    return result


def _read_at(handle, offset: int, length: int) -> bytes:
    handle.seek(offset)
    return handle.read(max(0, length))


def _sample(path: Path, index: dict) -> list[Sample]:
    """Verify the first bytes of a few members whose names promise a known type."""
    candidates = []
    for name, entries in sorted(index.items()):
        if not isinstance(name, str) or not entries:
            continue
        signature = _signature(name)
        if signature is not None:
            candidates.append((name, entries, signature))

    seen, ordered = set(), []
    for candidate in candidates:                 # one per type first
        suffix = Path(candidate[0]).suffix.lower()
        if suffix not in seen:
            seen.add(suffix)
            ordered.append(candidate)
    ordered += [c for c in candidates if c not in ordered]

    samples = []
    for name, entries, (where, magic) in ordered[:SAMPLE_LIMIT]:
        offset, dlen, start = entries[0]
        want = max(0, where + len(magic))
        try:
            with path.open("rb") as handle:
                if start:
                    head = start + _read_at(handle, offset, max(0, want - len(start)))
                else:
                    head = _read_at(handle, offset, want)
        except OSError as exc:
            samples.append(Sample(name, "unverifiable", "could not read: %s" % exc))
            continue
        if len(head) < want:
            samples.append(Sample(name, "unverifiable", "member is too short to check"))
            continue
        found = head[where:where + len(magic)]
        if found == magic:
            samples.append(Sample(name, "ok", repr(found)))
        else:
            samples.append(Sample(name, "mismatch",
                                  "expected %r, found %r" % (magic, found)))
    return samples


@dataclass
class Report:
    folder: Path
    archives: list[Archive] = field(default_factory=list)

    @property
    def stock_readable(self) -> bool:
        """True only when every archive found can be read by a stock reader."""
        return bool(self.archives) and all(a.stock_readable for a in self.archives)

    @property
    def readable_count(self) -> int:
        return sum(1 for archive in self.archives if archive.stock_readable)

    def lines(self) -> list[str]:
        out = ["%s" % self.folder]
        if not self.archives:
            out.append("  no archive found -- this release may keep its files loose")
            return out
        out.append("  %d of %d archive(s) readable by a stock reader"
                   % (self.readable_count, len(self.archives)))
        for archive in self.archives:
            out.append("  %-26s %8.1f MiB  %s"
                       % (archive.path.name[:26], archive.size / 1048576.0,
                          "OK" if archive.stock_readable else "no"))
            out.append("      %s" % archive.summary)
            for problem in archive.problems[1:]:
                out.append("      %s" % problem)
            for note in archive.notes:
                out.append("      note: %s" % note)
            for sample in archive.samples:
                if sample.verdict != "ok":
                    out.append("      sample %-26s %s %s"
                               % (sample.name[:26], sample.verdict, sample.detail))
        return out


def probe(folder: Path, extra_paths=()) -> Report:
    """Probe every archive under *folder* (and *extra_paths*).  Reads; never writes."""
    folder = Path(folder)
    return Report(folder=folder,
                  archives=[probe_archive(path)
                            for path in find_archives(folder, extra_paths)])
