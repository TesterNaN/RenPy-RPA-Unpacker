"""Build and run the archive reader for one game.

This module owns the lifecycle:

    discover game root -> find loader.py -> AST-extract the reader
    -> index archives -> extract entries in parallel

The generated module is executed in memory, and optionally mirrored to disk
(``--write-core``) so the extraction is auditable.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from . import _fallbacks
from .ast_extract import (
    ExtractionError,
    ExtractionResult,
    arc_files_fields,
    archive_extensions,
    archive_headers,
    extract_module,
)
from .runtime import (
    DEFAULT_BATCH_BYTES,
    ExternalRuntime,
    ProbeError,
    RuntimeUnavailable,
    discover_runtime,
)
from .inject import InjectError, InjectResult, Injector, discover_for_inject
__all__ = ["ArchiveEntry", "Unpacker", "UnpackError", "discover_game_root", "find_loader"]

#: Names the reader needs.  Order matters only for the report; the extractor
#: resolves the real dependency graph itself.  Archive *handlers* are deliberately
#: absent -- they are discovered from the loader's own registration calls, because
#: a modified loader may offer formats beyond the stock RPA v1/v2/v3.
#:
#: ``load`` and ``walkdir`` are here for ``--include-loose``: loose files are
#: enumerated and read through the game's own loader as well, just without the
#: archive/decryption step.  ``transfn`` and ``load_from_filesystem`` come along
#: because ``load()`` dispatches through Ren'Py's callback list, and the filesystem
#: callback is the one that actually locates a loose file -- without them it raises
#: ``FileNotFoundError`` even for a file sitting right there.
REQUIRED_NAMES = (
    "ArchiveHandlers",
    "index_archives",
    "load_from_archive",
    "load",
    "walkdir",
    "transfn",
    "load_from_filesystem",
    "archives",
    "arc_files",
)

#: Used only when a loader's registrations could not be read at all.
FALLBACK_HANDLER_NAMES = (
    "RPAv3ArchiveHandler",
    "RPAv2ArchiveHandler",
    "RPAv1ArchiveHandler",
)

ARCHIVE_SUFFIXES = (".rpa", ".rpi")

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class UnpackError(RuntimeError):
    """A user-actionable failure (bad path, unusable loader, ...)."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _archives_in(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    found = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in ARCHIVE_SUFFIXES
    ]
    return sorted(found, key=lambda p: p.name, reverse=True)


def _looks_already_unpacked(root: Path) -> bool:
    """Whether the game's assets are plain files rather than archives.

    A game that ships uncompressed scripts on disk has nothing for this tool to
    do, and saying so is very different from implying the archive search failed.

    Deliberately a shallow scan: one bad answer here only changes the wording of an
    error, so it must not walk a multi-gigabyte asset tree to find out.
    """
    game_dir = root / "game"
    scripts = 0
    for base in (game_dir, root):
        if not base.is_dir():
            continue
        for depth in (0, 1):
            try:
                entries = os.scandir(base) if depth == 0 else _scandir_children(base)
            except OSError:  # pragma: no cover - unreadable tree
                continue
            for entry in entries:
                try:
                    if not entry.is_file():
                        continue
                except OSError:  # pragma: no cover
                    continue
                suffix = os.path.splitext(entry.name)[1].lower()
                if suffix in (".rpa", ".rpi"):
                    return False
                if suffix in (".rpy", ".rpym"):
                    scripts += 1
                    if scripts >= 3:
                        return True
    return scripts > 0


def _scandir_children(base: Path) -> list:
    """Files one level below *base*, for the shallow already-unpacked probe."""
    found = []
    try:
        with os.scandir(base) as top:
            for entry in top:
                if not entry.is_dir():
                    continue
                try:
                    with os.scandir(entry.path) as inner:
                        found.extend(item for item in inner if item.is_file())
                except OSError:  # pragma: no cover
                    continue
    except OSError:  # pragma: no cover
        pass
    return found


def _looks_like_game_root(candidate: Path) -> bool:
    return (candidate / "renpy").is_dir() or (candidate / "renpy.py").is_file()


def _looks_like_renpy_game_dir(path: Path) -> bool:
    """Whether *path* is a ``game/`` directory Ren'Py loads content from.

    Two signals, strongest first: a sibling ``renpy/`` means a full install, while
    archives or loose scripts inside mean a trimmed/repacked one.  Either is enough
    to know that writing scripts here would be picked up by Ren'Py.
    """
    if not path.is_dir():
        return False
    if (path.parent / "renpy").is_dir():
        return True
    try:
        for entry in path.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() in (".rpa", ".rpi", ".rpy", ".rpyc", ".rpym"):
                return True
    except OSError:  # pragma: no cover - unreadable directory
        return False
    return False


def _renpy_game_dir(root: Path) -> Path | None:
    """The ``game/`` directory Ren'Py will load, if one sits at or above *root*.

    Ren'Py loads exactly one directory: the ``game`` subdirectory of its base
    directory.  Detecting it structurally -- rather than assuming ``root/game`` --
    is what keeps the default output from landing inside it when the caller points
    ``--game`` at that directory itself.
    """
    root = Path(root)
    for candidate in [root, *root.parents]:
        if candidate.name.lower() != "game":
            continue
        if _looks_like_renpy_game_dir(candidate):
            return candidate

    conventional = root / "game"
    return conventional if conventional.is_dir() else None


def _safe_output_root(game_root: Path) -> Path:
    """Where extraction defaults to, guaranteed outside the loaded game directory.

    The caller may point ``--game`` at the base directory *or* straight at ``game``
    (or at ``renpy``).  Defaulting to ``<game_root>/extracted_files`` drops scripts
    inside ``game/`` in the second case, where Ren'Py will load them -- and a second
    copy of a script is exactly what stops a game from starting.
    """
    game_dir = _renpy_game_dir(game_root)
    if game_dir is not None and (game_root == game_dir or game_dir in game_root.parents):
        return game_dir.parent
    return game_root


def archives_with_extension(root: Path, extensions: Sequence[str]) -> list[Path]:
    """Archives under *root* whose extension the loader declares.

    Ren'Py itself decides this by asking the handlers, and a repacked release may
    declare anything -- ``.dll`` in one real game, where the archives masquerade as
    system libraries -- so this takes the declared list rather than assuming
    ``.rpa``.  A one-level scan of the root and its ``game/`` subdirectory mirrors
    where ``scandirfiles_from_filesystem`` looks.
    """
    wanted = {ext.lower() if ext.startswith(".") else f".{ext.lower()}" for ext in extensions}
    if not wanted:
        return []

    found: list[Path] = []
    directories = [root / "game", root]
    for directory in directories:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if entry.suffix.lower() in wanted:
                found.append(entry)
    return found


def _has_archive_header(path: Path, headers: Sequence[bytes]) -> bool:
    """Whether *path* starts with one of the loader's accepted archive headers.

    The same test ``index_archives()`` applies.  It is what separates a disguised
    archive from a genuine library sitting in the same directory: a real
    ``steam_api.dll`` starts with ``MZ`` and is correctly ignored.
    """
    if not headers:
        return True
    longest = max(len(header) for header in headers)
    try:
        with open(path, "rb") as handle:
            head = handle.read(longest)
    except OSError:
        return False
    return any(head.startswith(header) for header in headers)


def discover_game_root(start: Path) -> tuple[Path, Path, list[Path]]:
    """Locate ``(game_root, archive_dir, archives)``.

    ``game_root`` is the directory that contains ``renpy/``; ``archive_dir`` is
    where the archives live (usually ``game_root/game``).

    Two signals are combined.  An ancestor that also has a ``renpy/`` directory
    is a game root, so it wins over a bare directory that merely holds archives;
    failing that, the nearest ancestor with archives is used.  This is what lets
    the tool be run from inside ``<game>/game/`` and still resolve the right
    root -- picking the wrong one would make ``--loader`` auto-detection miss.
    """
    candidates: list[Path] = [start, *start.parents]

    best_with_archives: tuple[Path, Path, list[Path]] | None = None

    for candidate in candidates:
        for root, directory in ((candidate, candidate / "game"), (candidate, candidate)):
            archives = _archives_in(directory)
            if not archives:
                continue
            resolved = (root, directory, archives)
            if _looks_like_game_root(root):
                return resolved
            if best_with_archives is None:
                best_with_archives = resolved

    if best_with_archives is not None:
        return best_with_archives

    raise UnpackError(
        "found no .rpa/.rpi archive looking in "
        + ", ".join(str(path) for path in candidates[:5])
        + ("..." if len(candidates) > 5 else "")
        + ". Pass --game pointing at the game's root directory."
    )


def game_loader_candidates(game_root: Path) -> list[Path]:
    """Places a game ships its *own* ``loader.py``, most specific first.

    Deliberately excludes the parent directory: an SDK sitting next to a game is a
    different product, and its loader describes stock formats only.
    """
    return [
        game_root / "renpy" / "loader.py",
        game_root / "loader.py",
        game_root / "game" / "renpy" / "loader.py",
        game_root / "lib" / "renpy" / "loader.py",
    ]


def find_game_loader(game_root: Path) -> Path | None:
    """The loader.py that ships with this game, if any."""
    for candidate in game_loader_candidates(game_root):
        if candidate.is_file():
            return candidate
    return None


def find_loader(game_root: Path, explicit: Path | None = None) -> Path:
    """Locate the ``loader.py`` whose archive machinery we can reuse.

    The game's *own* loader is the authority on its archive format, so it always
    wins when present: a repacked release may use a custom magic, a custom
    extension, or a reordered index, and a stock loader would describe none of
    that.  ``explicit`` (``--loader``) is therefore a **substitute for a missing
    loader**, not an override -- if the game ships one and the two disagree, that
    is reported rather than silently obeyed.
    """
    own = find_game_loader(game_root)

    if explicit is not None:
        explicit = Path(explicit)
        if not explicit.is_file():
            raise UnpackError(f"--loader does not exist or is not a file: {explicit}")
        if own is not None and not _same_file(own, explicit):
            raise UnpackError(
                _loader_conflict_message(own, explicit)
            )
        return explicit

    if own is not None:
        return own

    searched = ", ".join(str(path) for path in game_loader_candidates(game_root))
    raise UnpackError(
        "could not find this game's own renpy/loader.py.\n"
        f"  searched: {searched}\n"
        "  A compiled build may ship loader.pyc without loader.py. In that case "
        "--loader must point at a loader.py that describes THIS game's archive "
        "format -- normally a copy taken from the same release. A stock Ren'Py "
        "loader only understands stock formats and will find no archives."
    )


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or a.samefile(b)
    except OSError:  # pragma: no cover - unreadable path
        return False


def _loader_conflict_message(own: Path, explicit: Path) -> str:
    """Explain why an explicit loader must not replace the game's own."""
    detail = (
        f"this game ships its own loader: {own}\n"
        f"  --loader was given: {explicit}\n"
        "  The game's own loader is the authority on its archive format. Because it "
        "is present, --loader is not used."
    )
    try:
        own_source = read_loader_source(own)
        other_source = read_loader_source(explicit)
    except UnpackError:  # pragma: no cover - read_loader_source reports its own errors
        return detail

    own_ext = archive_extensions(own_source)
    other_ext = archive_extensions(other_source)
    own_hdr = archive_headers(own_source)
    other_hdr = archive_headers(other_source)

    if (own_ext, own_hdr) != (other_ext, other_hdr):
        detail += (
            "\n  They disagree on the archive format, which is exactly why the "
            "game's own loader is required:\n"
            f"    game  : extensions {own_ext}, headers {own_hdr}\n"
            f"    --loader: extensions {other_ext}, headers {other_hdr}"
        )
    else:
        detail += "\n  (They agree on the format; --loader is simply redundant here.)"

    detail += "\n  Drop --loader, or point it at this game's own loader.py."
    return detail


def locate_loader_pyc(loader: Path) -> Path:
    """Best-effort peek at a sibling ``loader.pyc`` for version hints."""
    return loader.with_suffix(".pyc")


# ---------------------------------------------------------------------------
# Reading the loader source
# ---------------------------------------------------------------------------


def read_loader_source(loader: Path) -> str:
    """Read loader.py as text, tolerating odd encodings and BOMs."""
    try:
        raw = loader.read_bytes()
    except OSError as exc:
        raise UnpackError(f"cannot read {loader}: {exc}") from exc

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_loader_source(source: str, filename: str) -> str:
    """Parse *source*, falling back to an annotation-stripped copy if needed.

    A loader.py newer than the running interpreter used to be fatal.  Annotation
    syntax is the usual culprit, and since the extractor re-emits annotations as
    strings they can simply be dropped from the *analysis* copy.
    """
    import ast

    try:
        ast.parse(source, filename=filename)
        return source
    except SyntaxError as first_error:
        stripped = strip_annotations(source)
        if stripped == source:
            raise UnpackError(
                f"cannot parse {filename}: {first_error}. If this loader.py is "
                f"newer than Python {sys.version_info.major}.{sys.version_info.minor}, "
                "re-run under a newer interpreter."
            ) from first_error
        try:
            ast.parse(stripped, filename=filename)
        except SyntaxError as second_error:  # pragma: no cover - unusual
            raise UnpackError(
                f"cannot parse {filename}: {first_error}"
            ) from second_error
        return stripped


def _split_top_level(text: str) -> list[str]:
    """Split *text* on commas that are not nested inside brackets or strings."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None

    for char in text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue

        if char in "\"'":
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)

    parts.append("".join(current))
    return parts


def _strip_parameter_annotation(parameter: str) -> str:
    """Drop the ``: annotation`` from one parameter, keeping its default."""
    if ":" not in parameter:
        return parameter

    depth = 0
    quote: str | None = None
    for index, char in enumerate(parameter):
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == ":" and depth == 0:
            # Everything between here and a top-level '=' is the annotation.
            head = parameter[:index]
            tail = parameter[index + 1 :]
            eq_depth = 0
            for offset, inner in enumerate(tail):
                if inner in "([{":
                    eq_depth += 1
                elif inner in ")]}":
                    eq_depth -= 1
                elif inner == "=" and eq_depth == 0:
                    return head + tail[offset:]
                elif inner == "*" and eq_depth == 0:
                    # Bare '*' marker after an annotation, e.g. `a: int, *, b`.
                    return head + tail[offset:]
            return head
    return parameter


def strip_annotations(source: str) -> str:
    """Remove signature annotations while preserving layout and semantics.

    This is only a *fallback* for a loader.py that the running interpreter cannot
    parse.  Annotation syntax is the usual culprit, and since the extractor
    re-emits annotations as strings they can be dropped from the analysis copy.

    Handles nested subscripts (``dict[str, int]``), defaults containing commas
    and brackets, ``*args`` / ``**kwargs`` and bare ``*`` markers -- a plain
    regex gets ``dict[str, int]`` wrong because it splits on the inner comma.
    """
    out: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    paren_depth = 0
    in_signature = False

    def flush_signature(head: str) -> str:
        prefix, _, rest = head.partition("(")
        body, _, tail = rest.rpartition(")")
        if not rest:
            return head
        parameters = [_strip_parameter_annotation(p) for p in _split_top_level(body)]
        # Drop a trailing return annotation before the ':'.
        tail = re.sub(r"\s*->\s*[^:]+:", ":", tail, count=1)
        return f"{prefix}({','.join(p for p in parameters if p.strip() != '')}){tail}"

    for line in source.splitlines(keepends=True):
        for char in line:
            if quote is not None:
                buffer.append(char)
                if char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
                buffer.append(char)
            elif char == "(":
                paren_depth += 1
                buffer.append(char)
            elif char == ")":
                paren_depth -= 1
                buffer.append(char)
            else:
                buffer.append(char)

        candidate = "".join(buffer)
        if not in_signature and re.match(r"\s*(async\s+)?def\s+\w+\s*\(", candidate):
            in_signature = True

        if in_signature and paren_depth <= 0 and ":" in candidate.split(")", 1)[-1]:
            out.append(flush_signature(candidate))
            buffer = []
            in_signature = False

    if buffer:
        out.append("".join(buffer))
    return "".join(out)


# ---------------------------------------------------------------------------
# Generated module
# ---------------------------------------------------------------------------


def build_reader(
    loader_source: str,
    *,
    filename: str,
    unsafe_pickle: bool = False,
    write_core: Path | None = None,
) -> tuple[dict, ExtractionResult]:
    """Extract, compile and execute the reader, returning its namespace.

    The generated module's ``build()`` takes no required arguments: every shim is
    defined *inside* the generated function, so the module is self-contained and
    there is no second place where a name could be supplied inconsistently.

    *write_core* is written as soon as the source exists, before it is compiled or
    executed.  Writing it only on success was useless for the case it exists for:
    when the generated module itself is what fails, the error message tells the
    user to inspect it with ``--write-core``, and that run would fail in exactly
    the same place without ever producing the file.
    """
    fallbacks = _fallbacks.sources(unsafe_pickle=unsafe_pickle)

    try:
        result = extract_module(
            loader_source,
            filename=filename,
            required=REQUIRED_NAMES,
            fallbacks=fallbacks,
            forced_shims=_fallbacks.FORCED_SHIMS,
        )
    except ExtractionError as exc:
        raise UnpackError(str(exc)) from exc
    except SyntaxError as exc:
        raise UnpackError(f"could not analyse {filename}: {exc}") from exc

    if not result.generated_names:
        raise UnpackError(
            f"none of the archive readers could be found in {filename}. "
            f"Looked for: {', '.join(REQUIRED_NAMES)}. "
            "This does not look like a Ren'Py loader.py."
        )

    if write_core is not None:
        write_core.parent.mkdir(parents=True, exist_ok=True)
        write_core.write_text(result.source, encoding="utf-8")

    try:
        code = compile(result.source, "<renpy_unpack:generated>", "exec")
    except SyntaxError as exc:
        raise UnpackError(
            f"the extracted module does not compile ({exc.msg} at line {exc.lineno}). "
            "This is a renpy_unpack bug; re-run with --write-core and inspect the "
            "generated file."
        ) from exc

    module_globals: dict = {"__name__": "renpy_unpack.core.generated"}

    try:
        exec(code, module_globals)
        namespace = module_globals["build"]()
    except UnpackError:
        raise
    except Exception as exc:
        raise UnpackError(
            f"could not initialise the extracted reader: {type(exc).__name__}: {exc}\n"
            "Re-run with --write-core to inspect the generated module."
        ) from exc

    missing = [name for name in ("index_archives", "load_from_archive") if name not in namespace]
    if missing:
        raise UnpackError(
            f"the extracted reader is missing {', '.join(missing)}; "
            f"{filename} does not look like a Ren'Py loader.py"
        )

    return namespace, result


@dataclass
class Reader:
    """A ready-to-use archive reader: the generated namespace plus its metadata."""

    namespace: dict
    extraction: ExtractionResult
    #: Field names ``index_archives`` unpacks out of each ``arc_files`` entry.
    arc_entry_fields: list[str]
    #: ``extension -> [(header, handler), ...]``, deduplicated.
    handlers_by_extension: dict[str, list[tuple[bytes, object]]]
    handlers: list[object]

    def point_config_at(self, content_dir: Path) -> None:
        """Tell the shimmed ``renpy.config`` where the game's files live.

        ``loader.load()`` resolves a name through ``transfn``, which joins
        ``config.basedir`` and ``config.searchpath``.  Without this, reading a loose
        file fails with ``FileNotFoundError`` even though the file is right there.
        """
        renpy = self.namespace.get("renpy")
        config = getattr(renpy, "config", None)
        if config is None:  # pragma: no cover - defensive
            return
        try:
            config.basedir = str(content_dir.parent)
            config.searchpath = [content_dir.name]
        except Exception:  # pragma: no cover - a config that refuses assignment
            pass

    def archivable_suffix(self, suffix: str) -> bool:
        return suffix in self.handlers_by_extension

    def arc_entry(self, archive: Path, handler) -> tuple:
        """Build one ``arc_files`` entry in whatever shape the loader unpacks."""
        values = {
            "stem": archive.stem,
            "ext": archive.suffix,
            "fn": str(archive),
            "filename": str(archive),
            "path": str(archive),
            "name": archive.name,
            "headers": handler.get_supported_headers(),
            "extensions": handler.get_supported_extensions(),
            "handler": handler,
        }

        fields = self.arc_entry_fields or ["stem", "ext", "fn"]
        missing = [field for field in fields if field not in values]
        if missing:
            raise UnpackError(
                "cannot build arc_files entries: loader.py unpacks unknown "
                f"field(s) {', '.join(missing)} from arc_files "
                f"(known: {', '.join(sorted(values))})"
            )
        return tuple(values[field] for field in fields)

    def _name_archives_in_config(self, archives: list[Path]) -> None:
        """Name *archives* in ``renpy.config.archives``, the pre-8.4 way.

        Older ``index_archives()`` does ``transfn(prefix + ext)`` for every entry
        in that list, trying each handler-declared extension, so what it wants is
        archive names *without* the extension.
        """
        config = getattr(self.namespace.get("renpy"), "config", None)
        if config is None:
            raise UnpackError(
                "this loader has no arc_files list, and the reader has no config to "
                "name archives in, so its archives cannot be indexed"
            )

        prefixes: list[str] = []
        for archive in archives:
            prefix = (
                archive.name[: -len(archive.suffix)] if archive.suffix else archive.name
            )
            if prefix not in prefixes:
                prefixes.append(prefix)
        config.archives = prefixes

    def index_archives(self, archives: list[Path]) -> None:
        """Index *archives* in one call, using Ren'Py's own ``index_archives()``.

        All archives are passed together on purpose: loader's implementation
        begins with ``archives.clear()``, so indexing them one at a time would
        leave only the last one's index behind.

        Exactly one ``arc_files`` entry is emitted per archive.  Every handler
        that accepts an extension still reaches that archive through
        ``archive_handlers.spec()``, so the header sniff can pick the right one --
        which is how Ren'Py does it, and it keeps a single archive from being
        parsed once per candidate header.
        """
        arc_files = self.namespace.get("arc_files")

        usable = [archive for archive in archives if self.archivable_suffix(archive.suffix)]
        unusable = [archive for archive in archives if not self.archivable_suffix(archive.suffix)]

        if not usable:
            names = ", ".join(path.name for path in unusable)
            raise UnpackError(
                f"these archives use an extension no extracted handler supports: {names}. "
                f"Supported: {', '.join(sorted(self.handlers_by_extension)) or '(none)'}"
            )

        if arc_files is None:
            # Ren'Py 8.3.x and older have no ``arc_files`` list at all: their
            # ``index_archives()`` walks ``renpy.config.archives`` and probes each
            # prefix against the handler-declared extensions itself.  So the same
            # information has to be handed over in that form instead.
            self._name_archives_in_config(usable)
        else:
            arc_files.clear()
            for archive in usable:
                arc_files.append(
                    self.arc_entry(archive, self.handlers_by_extension[archive.suffix][0][1])
                )

        try:
            self.namespace["index_archives"]()
        except Exception as exc:
            raise UnpackError(
                f"Ren'Py's own index_archives() failed on these archives "
                f"({type(exc).__name__}: {exc}). The archives may be corrupted or "
                "use a newer RPA revision than the loader.py in use; try --loader "
                "pointing at a matching Ren'Py SDK."
            ) from exc

        self._reload_rebound_globals()

    def _reload_rebound_globals(self) -> None:
        """Re-read names the loader rebinds rather than mutates.

        ``build()`` returns a dict, so every entry is a snapshot taken when it
        returned.  Ren'Py 8.3's ``index_archives()`` starts with::

            global archives
            archives = [ ]

        -- it *rebinds* the name, so the list handed back by ``build()`` is not the
        one that ends up holding the index; the populated list is the generated
        module's global and the reader would see zero entries.  8.4 and later use
        ``archives.clear()``, which mutates in place, so this never showed up.
        A function's ``__globals__`` is that live namespace.
        """
        module_globals = getattr(
            self.namespace.get("index_archives"), "__globals__", None
        )
        if not module_globals:
            return
        for name in list(self.namespace):
            if name in module_globals:
                self.namespace[name] = module_globals[name]

    def load(self, name: str) -> io.BufferedReader | None:
        """Open one archive member, or ``None`` when no archive provides it.

        Falls back to :func:`load_split_entry` when Ren'Py's own
        ``load_from_archive()`` declines an entry it should have served.
        """
        handle = self.namespace["load_from_archive"](name)
        if handle is not None:
            return handle
        return load_split_entry(self.namespace["archives"], name)

    def load_loose(self, name: str) -> bytes | None:
        """Read a file that sits on disk, using the game's own resolution.

        Goes through ``loader.load()`` rather than opening the path directly: that
        applies the game's prefix rules and consults the same callback list the game
        uses, so the bytes are what the game would actually see.

        ``tl=False`` asks for the base file rather than a translation, which also
        keeps the call off ``renpy.game`` (the shim cannot synthesise a full
        runtime).  The loader raises instead of returning ``None`` when nothing
        matches, hence the conversion.
        """
        loader = self.namespace.get("load")
        if loader is None:
            return None
        try:
            handle = loader(name, tl=False)
        except TypeError:
            # An older loader without the ``tl`` parameter.
            try:
                handle = loader(name)
            except Exception:
                return None
        except Exception:
            return None
        try:
            return handle.read()
        finally:
            try:
                handle.close()
            except Exception:  # pragma: no cover - best effort
                pass


def load_split_entry(archives, name: str) -> io.BufferedReader | None:
    """Serve a single-triplet split entry that Ren'Py's reader drops.

    Ren'Py 8.5.2's ``parsed.load_from_archive`` builds the reader for a split
    entry and then discards it:

        if start == None or len(start) == 0:
            rv = RWopsIO(afn, "rb", base=offset, length=dlen)
            return io.BufferedReader(rv)
        else:
            a = RWopsIO.from_buffer(start, name=name)
            b = RWopsIO(afn, "rb", base=offset, length=dlen)
            rv = RWopsIO.from_split(a, b, name=name)
            rv = io.BufferedReader(rv)        # <-- never returned

    Execution falls out of the loop and returns ``None``, so the member looks
    missing even though the index describes it completely.  This reproduces the
    intended behaviour -- the head bytes followed by the ``dlen`` bytes at
    ``offset`` -- for entries whose triplet is split into exactly two parts.
    Multi-segment entries (the "compatibility path") are handled by Ren'Py's own
    code and are left to it.
    """
    for archive_path, index in archives:
        entry = index.get(name)
        if entry is None:
            continue
        if len(entry) != 1:
            continue  # multi-segment: Ren'Py's compatibility path owns this

        parts = entry[0]
        if len(parts) != 3:
            continue

        offset, length, head = parts
        # A non-empty bytes head is what marks a split entry; Ren'Py itself
        # builds the reader for exactly this condition.
        if not isinstance(head, bytes) or not head:
            continue

        with open(archive_path, "rb") as handle:
            handle.seek(offset)
            tail = handle.read(length)

        return io.BufferedReader(io.BytesIO(head + tail))

    return None


def _populate_file_callbacks(namespace: dict) -> None:
    """Register the file openers, in the order Ren'Py registers them.

    The extractor recovers *definitions*, not the module-level
    ``file_open_callbacks.append(...)`` statements that wire them up, so the list
    arrives empty and ``load()`` finds nothing.  Order matters: Ren'Py appends the
    filesystem opener before the archive one, and that is precisely why a loose
    file shadows an archived copy of the same name.  Replaying them in this order
    keeps that behaviour.
    """
    callbacks = namespace.get("file_open_callbacks")
    if callbacks is None:  # pragma: no cover - unusual loader
        return

    callbacks.clear()
    for name in ("load_from_filesystem", "load_from_archive"):
        opener = namespace.get(name)
        if opener is not None:
            callbacks.append(opener)


def prepare_reader(namespace: dict, extraction: ExtractionResult) -> Reader:
    """Register the extracted handlers and describe the resulting reader.

    Ren'Py wires this up with module-level ``archive_handlers.append(...)`` calls
    sitting next to each class definition.  Those calls are statements rather than
    definitions, so they are not recoverable by name and have to be replayed here.

    Three adjustments make the extracted code work outside a running game:

    * ``archive_handlers`` must be repopulated, or ``spec()`` raises ``KeyError``.
    * The handler *set* is taken from the loader's own registration calls, not a
      fixed list.  A modified loader may register extra formats -- one real game
      adds an AES-256-CTR encrypted archive type -- and hardcoding v1/v2/v3 would
      silently ignore it.
    * Ren'Py registers *each* handler for ``.rpa`` -- v1, v2 and v3 all claim it
      -- all reporting the same header size, so several handlers land in the
      candidate list for one extension and ``spec()`` raises ``ValueError`` on
      ``max()``.  Deduplicating per extension collapses that to what the header
      sniffing actually needs.
    """
    archive_handlers = namespace.get("archive_handlers")
    if archive_handlers is None:
        raise UnpackError(
            "the extracted reader has no archive_handlers registry; "
            "this does not look like a Ren'Py loader.py"
        )

    # Newer Ren'Py keeps the registry in an ``ArchiveHandlers`` object that caches
    # header specs per extension; older versions use a plain list, which has no
    # cache to clear.  (8.3.x is a plain list -- assuming otherwise crashed here
    # with AttributeError rather than a readable message.)
    for cache_name in ("exts", "peek"):
        cache = getattr(archive_handlers, cache_name, None)
        if cache is not None:
            cache.clear()

    by_extension: dict[str, list[tuple[bytes, object]]] = {}
    handlers: list[object] = []

    # Register the handlers the loader registers, in the loader's own order:
    # index_archives() tries candidate headers in turn, so that order decides
    # which handler claims an archive whose header matches more than one.
    registered = list(extraction.handler_names)
    unusable = [name for name in registered if namespace.get(name) is None]
    if unusable:
        # An unextractable handler normally needs a native module.  It only
        # costs us the archives that format serves, so report and continue.
        extraction.notes.append(
            "handler(s) not available: "
            + ", ".join(unusable)
            + " (archives in those formats cannot be read)"
        )

    usable = [name for name in registered if namespace.get(name) is not None]
    if not usable:
        # A loader whose registrations we could not read at all.
        usable = [name for name in FALLBACK_HANDLER_NAMES if namespace.get(name) is not None]

    for name in usable:
        handler = namespace[name]
        archive_handlers.append(handler)
        handlers.append(handler)

        candidates = [(header, handler) for header in handler.get_supported_headers()]
        for extension in handler.get_supported_extensions():
            bucket = by_extension.setdefault(extension, [])
            for candidate in candidates:
                if candidate not in bucket:
                    bucket.append(candidate)

    if not handlers:
        raise UnpackError(
            "no RPA handlers could be extracted from loader.py; the file does not "
            "look like a Ren'Py loader"
        )

    _populate_file_callbacks(namespace)

    # Collapse handlers that describe themselves identically.  Ren'Py has no need
    # to: it indexes one process-wide list.  We build one `arc_files` entry per
    # (archive, handler) pair so the header sniff can still pick a worker handler
    # per extension, and without this a handler registered once per format variant
    # would make every archive be indexed several times over.
    deduped: list[object] = []
    seen: set[tuple] = set()
    for handler in handlers:
        try:
            identity = (
                tuple(handler.get_supported_headers()),
                tuple(handler.get_supported_extensions()),
                getattr(handler, "__name__", repr(handler)),
            )
        except Exception:  # pragma: no cover - a handler that cannot describe itself
            identity = (repr(handler),)
        if identity in seen:
            continue
        seen.add(identity)
        deduped.append(handler)

    return Reader(
        namespace=namespace,
        extraction=extraction,
        arc_entry_fields=arc_files_fields(extraction.source),
        handlers_by_extension=by_extension,
        handlers=deduped,
    )


# ---------------------------------------------------------------------------
# Entry selection and path safety
# ---------------------------------------------------------------------------


@dataclass
class ArchiveEntry:
    """One file the game can load, normalised to forward slashes."""

    name: str
    archive: Path
    #: Size the index declares.  Only the live-runtime backend knows this before
    #: reading, and it needs it to size read batches.
    declared_size: int = 0
    #: Absolute output path, filled in by the runtime extractor.
    destination: Path | None = None
    #: True when this file sits on disk rather than inside an archive.  Loose
    #: files are read through the loader too, just without the archive step.
    loose: bool = False

    @property
    def suffix(self) -> Path:
        return Path(self.name).suffix.lower()


def is_safe_relative(name: str) -> bool:
    """Reject names that would escape the output directory.

    Every component is validated: no absolute paths, no ``..``, no drive
    letters, and nothing Windows would reject.  Without this, an archive entry
    like ``../../.ssh/authorized_keys`` turns a read-only unpack into an
    arbitrary-write primitive.
    """
    if not name or name.startswith(("/", "\\")):
        return False
    if re.match(r"^[A-Za-z]:", name):
        return False

    parts = [part for part in re.split(r"[\\/]+", name)]
    if not parts or any(part in ("", ".", "..") for part in parts):
        return False

    for part in parts:
        if part.rstrip(" .") != part:
            return False
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            return False
        if any(char in part for char in '<>:"|?*\0'):
            return False
    return True


class _Filters:
    def __init__(self, globs: list[str], suffixes: list[str]):
        self.globs = [re.compile(_glob_to_regex(pattern), re.IGNORECASE) for pattern in globs]
        self.suffixes = {suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}" for suffix in suffixes}

    def __bool__(self) -> bool:
        return bool(self.globs or self.suffixes)

    def matches(self, entry: ArchiveEntry) -> bool:
        if self.suffixes and entry.suffix not in self.suffixes:
            return False
        if self.globs:
            basename = entry.name.rsplit("/", 1)[-1]
            if not any(
                pattern.fullmatch(entry.name) or pattern.fullmatch(basename)
                for pattern in self.globs
            ):
                return False
        return True


def _glob_to_regex(pattern: str) -> str:
    """Translate a glob supporting ``**`` into a regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i : i + 2] == "**":
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# The unpacker
# ---------------------------------------------------------------------------


@dataclass
class UnpackStats:
    extracted: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_written: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


class _Progress:
    """Single-line progress that degrades gracefully off a TTY."""

    def __init__(self, total: int, enabled: bool):
        self.total = total
        self.enabled = enabled
        self.done = 0
        self.ok = 0
        self.skipped = 0
        self.bad = 0
        self._lock = threading.Lock()
        self._last_width = 0

    def update(self, outcome: str, name: str = "") -> None:
        with self._lock:
            self.done += 1
            if outcome == "ok":
                self.ok += 1
            elif outcome == "skip":
                self.skipped += 1
            else:
                self.bad += 1
            if not self.enabled:
                return
            text = (
                f"\r  {self.done}/{self.total} "
                f"ok={self.ok} skip={self.skipped} failed={self.bad}"
            )
            if name:
                room = 60
                text += f"  {name[-room:] if len(name) > room else name}"
            padding = " " * max(0, self._last_width - len(text))
            self._last_width = len(text)
            sys.stderr.write(text + padding)
            sys.stderr.flush()

    def close(self) -> None:
        if self.enabled:
            sys.stderr.write("\n")
            sys.stderr.flush()


#: Subtrees Ren'Py refuses to treat as game content.  ``loader.scandirfiles``'s
#: ``add()`` returns early for these two prefixes, so they never enter the game's
#: own index: ``cache/`` holds Ren'Py's build/reload caches and ``saves/`` holds the
#: player's save data.  Excluding them keeps "export what the loader exports" true.
_LOOSE_EXCLUDED_PREFIXES = ("cache/", "saves/")


class Unpacker:
    """Extracts every archive member of one game."""

    def __init__(
        self,
        game_root: Path,
        *,
        loader: Path | None = None,
        output: Path | None = None,
        jobs: int = 8,
        skip_existing: bool = False,
        unsafe_pickle: bool = False,
        globs: list[str] | None = None,
        suffixes: list[str] | None = None,
        write_core: Path | None = None,
        progress: bool | None = None,
        runtime: bool = False,
        runtime_python: Path | None = None,
        runtime_batch_bytes: int = DEFAULT_BATCH_BYTES,
        include_loose: bool = False,
        inject: bool = False,
        inject_keep_script: bool = False,
    ):
        self.game_root = Path(game_root).resolve()
        self.loader_hint = Path(loader).resolve() if loader else None
        self.output = Path(output) if output else None
        self.jobs = max(1, int(jobs))
        self.skip_existing = skip_existing
        self.unsafe_pickle = unsafe_pickle
        self.filters = _Filters(globs or [], suffixes or [])
        self.write_core = Path(write_core) if write_core else None
        self.progress_enabled = (
            sys.stderr.isatty() if progress is None else bool(progress)
        )

        self.use_runtime = bool(runtime) or runtime_python is not None
        self.use_inject = bool(inject)
        self.inject_keep_script = bool(inject_keep_script)
        if self.use_inject and self.use_runtime:
            raise UnpackError(
                "--inject and --runtime are two ways to do the same thing (use the "
                "game's own runtime); pick one. --inject needs a writable game "
                "directory, --runtime does not."
            )
        self.runtime_python = Path(runtime_python) if runtime_python else None
        self.runtime_batch_bytes = max(1, int(runtime_batch_bytes))
        self.include_loose = bool(include_loose)

        self.archive_dir: Path | None = None
        self.archives: list[Path] = []
        self.unsupported_archives: list[Path] = []
        self.skipped_index_entries: list[str] = []
        self.loose_count = 0
        self.entries: list[ArchiveEntry] = []
        self.reader: Reader | None = None
        self.external: ExternalRuntime | None = None
        self.injector: Injector | None = None
        #: Set when discovery had to climb above the directory the user named.
        self.climbed_from: Path | None = None

    # -- setup -------------------------------------------------------------

    def discover(self) -> None:
        """Locate the game and its archives.

        Archives are normally found by extension.  A repacked release may rename
        them (one real game calls its archives ``.dll`` and puts them next to real
        system libraries), so when the usual extensions turn up nothing this asks
        the loader what extensions *it* declares and looks again.

        The loader is the authority here, which also bounds what is possible: a
        loader that does not describe the archives on disk cannot be used to read
        them.  That is why an explicit ``--loader`` never overrides a loader the
        game ships itself.
        """
        requested = self.game_root
        try:
            self.game_root, self.archive_dir, self.archives = discover_game_root(
                self.game_root
            )
        except UnpackError as first_error:
            fallback = self._discover_by_declared_extensions()
            if fallback is not None:
                self.game_root, self.archive_dir, self.archives = fallback
            elif self.use_inject:
                # --inject does not need loader.py: the game's runtime already has it
                # loaded, source or bytecode.  That is exactly what lets it handle
                # builds that ship only loader.pyc, so it must not fall through to
                # the loader-based diagnostics below.
                root = self._loose_game_root()
                self.game_root = root
                self.archive_dir = self._game_content_dir() or root
                self.archives = []
            else:
                # Raises for a genuine dead end, but accepts an archive-less game
                # when --include-loose is set.
                root = self._nothing_to_do_error(first_error)
                self.game_root = root
                self.archive_dir = self._game_content_dir() or root
                self.archives = []

        # Report a climbed root rather than silently unpacking a different game:
        # pointing at ...\SomeGame\renpy is a natural mistake.
        if requested != self.game_root:
            self.climbed_from = requested

        if self.use_inject:
            # The injected script finds the archives from the loader's live index, so
            # there is nothing to validate here -- and no loader.py to validate
            # against, which is the whole point.
            return

        # Now that the root is settled, surface a --loader conflict for itself.
        # Leaving it to load_reader() would let "no archives found" be blamed for
        # what is really a wrong-loader problem.
        find_loader(self.game_root, self.loader_hint)

    def _loose_game_root(self) -> Path:
        """The game root to use when there are no archives to find it by."""
        for candidate in [self.game_root, *self.game_root.parents]:
            if find_game_loader(candidate) is not None:
                return candidate
        return self.game_root

    def _nothing_to_do_error(self, original: UnpackError) -> Path:
        """Resolve the game root, or raise explaining why nothing can be done.

        Returns a *root* rather than raising in the one case where there genuinely
        is work to do: an already-unpacked game with ``--include-loose`` set, where
        the loose files are the payload.  Otherwise raises, distinguishing "already
        unpacked" from "could not find the archives" -- the first is an answer, not a
        failure, and must not read as "your --game path is wrong".
        """
        if _looks_already_unpacked(self.game_root):
            if self.include_loose:
                return self._loose_game_root()
            raise UnpackError(
                f"{self.game_root} appears to be already unpacked: its scripts and "
                f"assets are plain files on disk (.rpy/.rpyc/.webp/.ogg/...) rather "
                f"than inside an archive. There is nothing to unpack.\n"
                f"  Looked for archives with extensions: "
                f"{', '.join(ARCHIVE_SUFFIXES)}\n"
                f"  Re-run with --include-loose to copy the loose files anyway."
            )
        raise self._no_archives_error(original)

    def _no_archives_error(self, original: UnpackError) -> UnpackError:
        """Explain *why* nothing was found, in terms of the loader's own claims.

        "no archives found" is misleading in two common situations, so both are
        checked and reported for themselves:

        * ``--loader`` disagrees with a loader the game ships -- the wrong-loader
          problem, which no amount of searching will fix;
        * the loader declares extensions that nothing on disk uses, or candidates
          exist that its header sniff rejects.
        """
        try:
            loader = find_loader(self.game_root, self.loader_hint)
        except UnpackError as conflict:
            # A --loader conflict is the real story; the archive-search failure is
            # only its symptom, so report the cause.
            if "--loader" in str(conflict) or "ships its own loader" in str(conflict):
                return conflict
            return original

        try:
            source = read_loader_source(loader)
            extensions = archive_extensions(source)
        except UnpackError:
            return original
        if not extensions:
            return original

        nearby = archives_with_extension(self.game_root, extensions)
        detail = (
            f"{loader} declares these archive extensions: "
            f"{', '.join(extensions)}, but no matching archive was found under "
            f"{self.game_root}."
        )
        if nearby:
            detail += (
                f" Candidates exist ({', '.join(p.name for p in nearby[:4])}) but none "
                f"starts with a header that loader accepts."
            )
        detail += (
            " If this game's archives use a different format, this loader is not the "
            "one that describes them; the game's own renpy/loader.py must be present."
        )
        return UnpackError(f"{detail}\n\n(original: {original})")

    def _discover_by_declared_extensions(
        self,
    ) -> tuple[Path, Path, list[Path]] | None:
        """Fall back to the extensions the loader's own handlers accept.

        The header sniff still decides which candidates are really archives, so
        picking up a genuine ``steam_api.dll`` alongside the disguised archives is
        harmless.
        """
        for candidate in [self.game_root, *self.game_root.parents]:
            if not _looks_like_game_root(candidate):
                continue
            try:
                loader = find_loader(candidate, self.loader_hint)
                source = read_loader_source(loader)
            except UnpackError:
                continue

            extensions = archive_extensions(source)
            if not extensions:
                continue
            archives = archives_with_extension(candidate, extensions)
            if not archives:
                continue

            headers = archive_headers(source)
            confirmed = [path for path in archives if _has_archive_header(path, headers)]
            if not confirmed:
                continue

            archive_dir = confirmed[0].parent
            return candidate, archive_dir, sorted(confirmed, key=lambda p: p.name, reverse=True)
        return None

    def output_dir(self) -> Path:
        if self.output is not None:
            return self.output
        # Derived from the structural game directory, so that pointing --game at
        # `game` itself cannot default the output into the directory Ren'Py loads.
        return _safe_output_root(self.game_root) / "extracted_files"

    def load_reader(self) -> ExtractionResult | None:
        """Bring up a reader: the static one by default, the live one if asked."""
        if self.use_inject:
            return self._load_inject_reader()
        if self.use_runtime:
            return self._load_runtime_reader()
        return self._load_static_reader()

    def _load_inject_reader(self) -> None:
        """Prepare for ``--inject``: no static reader, no probe runtime.

        The whole job happens inside the game process, so there is nothing to index
        here.  Entry metadata comes back from the script's manifest, which is why
        this deliberately leaves ``self.entries`` alone.
        """
        try:
            self.injector = Injector(
                discover_for_inject(self.game_root, python=self.runtime_python),
                self.output_dir(),
                # Pass the extension filter down so the script does not extract
                # 1.5 GiB only for us to discard most of it.
                only=sorted(self.filters.suffixes),
                skip_existing=self.skip_existing,
                keep_script=self.inject_keep_script,
            )
        except InjectError as exc:
            raise UnpackError(str(exc)) from exc
        return None

    def run_inject(self) -> InjectResult:
        """Perform the extraction by running the game once."""
        if self.injector is None:  # pragma: no cover - guarded by the CLI
            raise UnpackError("--inject was not initialised")
        try:
            return self.injector.run()
        except InjectError as exc:
            raise UnpackError(str(exc)) from exc

    def _load_static_reader(self) -> ExtractionResult:
        """Find loader.py, extract the readers, and index the archives."""
        loader = find_loader(self.game_root, self.loader_hint)
        source = parse_loader_source(read_loader_source(loader), str(loader))
        namespace, result = build_reader(
            source,
            filename=str(loader),
            unsafe_pickle=self.unsafe_pickle,
            write_core=self.write_core,
        )
        self.reader = prepare_reader(namespace, result)
        content_dir = self._game_content_dir()
        if content_dir is not None:
            self.reader.point_config_at(content_dir)

        if self.archives:
            self.reader.index_archives(self.archives)
        # No archives is a valid state under --include-loose: the loose files are
        # the payload, and index_archives() would (correctly) complain about an
        # empty list.
        self._collect_entries()
        return result

    def _game_content_dir(self) -> Path | None:
        """The directory Ren'Py loads content from, for enumerating loose files."""
        candidate = self.game_root / "game"
        if candidate.is_dir():
            return candidate
        return self.game_root if self.game_root.is_dir() else None

    def _loose_entries(self) -> list[ArchiveEntry]:
        """Files sitting on disk, obtained from the loader itself.

        Asked of the game's ``loader.walkdir`` rather than guessed: Ren'Py is the
        authority on which loose files are game content.  The two prefixes it
        excludes from its own index are excluded here too, so what comes out is what
        the loader would actually load -- ``cache/`` is Ren'Py's build/reload cache
        and ``saves/`` is the player's save data, and neither is game content.

        (``loader.game_files`` would be the other candidate, but ``scandirfiles``
        merges loose files and archive members into one list, which loses the
        distinction this needs to pick the right read path.)
        """
        if self.reader is None:
            return []
        content_dir = self._game_content_dir()
        if content_dir is None:
            return []

        namespace = self.reader.namespace
        walkdir = namespace.get("walkdir")
        found: list[ArchiveEntry] = []

        if walkdir is not None:
            try:
                names = list(walkdir(str(content_dir)))
            except Exception as exc:  # pragma: no cover - defensive
                raise UnpackError(
                    f"the game's own walkdir() failed on {content_dir}: {exc}"
                ) from exc
        else:  # pragma: no cover - a loader without walkdir
            names = [
                path.relative_to(content_dir).as_posix()
                for path in content_dir.rglob("*")
                if path.is_file()
            ]

        for name in names:
            if not name or name.startswith(_LOOSE_EXCLUDED_PREFIXES):
                continue
            path = content_dir / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            found.append(
                ArchiveEntry(
                    name=name,
                    archive=path,
                    declared_size=size,
                    loose=True,
                )
            )
        return found

    def _load_runtime_reader(self) -> None:
        """Ask the game's own Ren'Py runtime for its archive index.

        Nothing is written into the game tree: the probe travels via ``-c`` and its
        payload goes to the scratch directory.
        """
        try:
            layout = discover_runtime(self.game_root, python=self.runtime_python)
        except RuntimeUnavailable as exc:
            raise UnpackError(str(exc)) from exc

        # No scratch directory: results stream back over a pipe, so the backend is
        # usable on a read-only install (which is the normal Steam case).
        self.external = ExternalRuntime(
            layout,
            batch_bytes=self.runtime_batch_bytes,
        )
        try:
            manifest = self.external.manifest()
        except (ProbeError, RuntimeUnavailable) as exc:
            raise UnpackError(f"the live runtime backend failed: {exc}") from exc

        entries: list[ArchiveEntry] = []
        seen: set[str] = set()
        for archive_name, name, size in manifest.index:
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append(
                ArchiveEntry(
                    name=name,
                    archive=self.archive_dir / archive_name,
                    declared_size=size,
                )
            )
        entries.sort(key=lambda entry: entry.name)
        self.entries = entries
        return None

    def _collect_entries(self) -> None:
        """Flatten what the game can load into a deduplicated, sorted entry list.

        Archive members always; loose files as well when ``--include-loose`` is set,
        in which case they are listed **first** because that is the order the game
        resolves them.  Keeping a single entry per name means a loose file correctly
        shadows the archived copy of the same name.
        """
        entries: list[ArchiveEntry] = []
        seen: set[str] = set()
        skipped: list[str] = []
        loose_count = 0

        if self.include_loose:
            for entry in self._loose_entries():
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                entries.append(entry)
                loose_count += 1

        for archive_path, index in self.reader.namespace["archives"]:
            for name in index:
                if isinstance(name, bytes):
                    # Python 2 archives store byte-string keys.
                    name = name.decode("utf-8", errors="surrogateescape")

                if name == "":
                    # Ren'Py's own loader rejects the empty name outright, so it
                    # is never a readable member; one real archive still lists one.
                    skipped.append(f"{Path(archive_path).name}:<empty name>")
                    continue

                if not isinstance(name, str):
                    raise UnpackError(
                        f"archive index for {archive_path} contains a non-string key "
                        f"({type(name).__name__}); refusing to continue"
                    )
                if name in seen:
                    continue
                seen.add(name)
                entries.append(ArchiveEntry(name=name, archive=Path(archive_path)))

        self.skipped_index_entries = skipped
        self.loose_count = loose_count
        entries.sort(key=lambda entry: entry.name)
        self.entries = entries

    def selected_entries(self) -> list[ArchiveEntry]:
        """The entries the configured ``--match`` / ``--ext`` filters let through."""
        if not self.filters:
            return list(self.entries)
        return [entry for entry in self.entries if self.filters.matches(entry)]

    # -- extraction --------------------------------------------------------

    def extract(self, entries: list[ArchiveEntry]) -> UnpackStats:
        if self.external is not None:
            return self._extract_via_runtime(entries)
        return self._extract_via_reader(entries)

    def _extract_via_runtime(self, entries: list[ArchiveEntry]) -> UnpackStats:
        """Extract by asking the game's runtime for batched reads.

        Batching is essential here: each batch is one Ren'Py start-up, so a
        per-file process would mean thousands of them.  Files are still written as
        each batch lands, so the peak in memory is one batch, not the whole game.
        """
        output_root = self.output_dir().resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        stats = UnpackStats()
        stats_lock = threading.Lock()
        progress = _Progress(len(entries), self.progress_enabled)

        wanted: list[ArchiveEntry] = []
        for entry in entries:
            try:
                destination = _safe_join(output_root, entry.name)
            except UnpackError as exc:
                self._record(stats, stats_lock, progress, entry, ("fail", 0, str(exc)))
                continue
            if self.skip_existing and destination.is_file():
                self._record(stats, stats_lock, progress, entry, ("skip", 0, None))
                continue
            entry.destination = destination
            wanted.append(entry)

        sizes = {entry.name: entry.declared_size for entry in wanted}
        by_name = {entry.name: entry for entry in wanted}

        try:
            for batch in self.external.batches(sizes):
                requested = [by_name[name] for name in batch]
                try:
                    result = self.external.invoke(batch)
                except (ProbeError, RuntimeUnavailable) as exc:
                    for entry in requested:
                        self._record(
                            stats, stats_lock, progress, entry, ("fail", 0, str(exc))
                        )
                    continue

                for entry in requested:
                    data = result.files.get(entry.name)
                    if data is None:
                        self._record(
                            stats,
                            stats_lock,
                            progress,
                            entry,
                            ("fail", 0, "the game's reader returned no data"),
                        )
                        continue
                    try:
                        _write_atomically(entry.destination, data)
                    except Exception as exc:
                        self._record(
                            stats,
                            stats_lock,
                            progress,
                            entry,
                            ("fail", 0, f"write failed: {type(exc).__name__}: {exc}"),
                        )
                        continue
                    self._record(
                        stats, stats_lock, progress, entry, ("ok", len(data), None)
                    )
        finally:
            progress.close()

        return stats

    def _extract_via_reader(self, entries: list[ArchiveEntry]) -> UnpackStats:
        output_root = self.output_dir().resolve()
        reader = self.reader

        stats = UnpackStats()
        stats_lock = threading.Lock()
        progress = _Progress(len(entries), self.progress_enabled)

        output_root.mkdir(parents=True, exist_ok=True)

        def work(entry: ArchiveEntry) -> tuple[str, int, str | None]:
            try:
                destination = _safe_join(output_root, entry.name)
            except UnpackError as exc:
                return "fail", 0, str(exc)

            if self.skip_existing and destination.is_file():
                return "skip", 0, None

            try:
                data = _read_entry(reader, entry)
            except Exception as exc:
                return "fail", 0, f"{type(exc).__name__}: {exc}"

            try:
                _write_atomically(destination, data)
            except Exception as exc:
                # Report the real error instead of hiding it behind a marker.
                return "fail", 0, f"write failed: {type(exc).__name__}: {exc}"
            return "ok", len(data), None

        try:
            if self.jobs == 1 or len(entries) <= 1:
                results = (work(entry) for entry in entries)
                for entry, outcome in zip(entries, results):
                    self._record(stats, stats_lock, progress, entry, outcome)
            else:
                with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                    for entry, outcome in zip(entries, pool.map(work, entries)):
                        self._record(stats, stats_lock, progress, entry, outcome)
        finally:
            progress.close()

        return stats

    @staticmethod
    def _record(stats, lock, progress, entry, outcome) -> None:
        status, size, error = outcome
        with lock:
            if status == "ok":
                stats.extracted += 1
                stats.bytes_written += size
            elif status == "skip":
                stats.skipped += 1
            else:
                stats.failed += 1
                stats.failures.append((entry.name, error or "unknown error"))
        progress.update(status, entry.name)

    # -- convenience -------------------------------------------------------

    def close(self) -> None:
        """Release runtime scratch and any temporary payload files."""
        if self.external is not None:
            self.external.close()
            self.external = None

    def __enter__(self) -> Unpacker:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def run(self) -> UnpackStats:
        self.discover()
        self.load_reader()
        return self.extract(self.selected_entries())


def _read_entry(reader: Reader, entry: ArchiveEntry) -> bytes:
    """Read one file in full, through the game's own loader either way.

    Loose files go through ``loader.load()`` (which resolves prefixes and reads
    from disk) and archive members through ``load_from_archive()``.  Both are the
    game's own code; the only difference is that a loose file has no decryption
    step, because it never went through an archive.
    """
    if entry.loose:
        data = reader.load_loose(entry.name)
        if data is None:
            raise UnpackError("the game's loader could not read this file from disk")
        return data

    handle = reader.load(entry.name)
    if handle is None:
        raise UnpackError("no archive index provides this file")
    try:
        return handle.read()
    finally:
        try:
            handle.close()
        except Exception:  # pragma: no cover - best effort
            pass


def _safe_join(root: Path, name: str) -> Path:
    """Join *name* onto *root*, refusing anything that escapes *root*.

    Deliberately free of filesystem access.  An earlier version used
    ``Path.resolve()``, which on Windows follows the real path -- and that turned
    out to be racy: when a sibling worker thread created the parent directory
    mid-call, resolution could return a differently-normalised path, so a
    legitimate entry was intermittently rejected as an escape.  Comparing
    normalised absolute paths is deterministic and, combined with the component
    validation in :func:`is_safe_relative`, sufficient: no ``..`` or absolute
    component can survive that check in the first place.
    """
    if not is_safe_relative(name):
        raise UnpackError(f"refusing unsafe archive entry name: {name!r}")

    destination = Path(os.path.normpath(os.path.join(str(root), name)))
    canonical_root = Path(os.path.normpath(str(root)))

    try:
        if os.path.normcase(str(destination)) != os.path.normcase(str(canonical_root)) and (
            os.path.commonpath(
                [os.path.normcase(str(destination)), os.path.normcase(str(canonical_root))]
            )
            != os.path.normcase(str(canonical_root))
        ):
            raise UnpackError(f"refusing archive entry escaping the output dir: {name!r}")
    except ValueError as exc:
        # commonpath raises when the paths are on different drives.
        raise UnpackError(
            f"refusing archive entry escaping the output dir: {name!r}"
        ) from exc

    return destination


def _write_atomically(destination: Path, data: bytes) -> None:
    """Write via a temp file in the same directory, then replace.

    Parallel workers can collide on shared parent directories, so directory
    creation races are tolerated.  The rename keeps a killed run from leaving
    truncated files behind.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(f".{destination.name}.renpy-unpack-{os.getpid()}")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
        os.replace(temp, destination)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def describe_extraction(unpacker: Unpacker) -> str:
    """Report which backend is in play and how it resolved."""
    if unpacker.external is not None:
        layout = unpacker.external.layout
        manifest = unpacker.external.last_manifest
        lines = [
            "Archive reader: live runtime (the game's own Ren'Py)",
            f"  interpreter    : {layout.python}",
            f"  game root      : {layout.game_root}",
            f"  launcher hooks : {layout.launcher.name if layout.launcher else '(none)'}",
            f"  archives       : {manifest.archives if manifest else 0}",
        ]
        attrs = manifest.aescrypt_attrs if manifest else []
        if attrs:
            lines.append(f"  native crypto  : renpy.aescrypt -> {', '.join(attrs)}")
        else:
            lines.append("  native crypto  : renpy.aescrypt unavailable")
        return "\n".join(lines)

    reader = unpacker.reader
    if reader is None:
        return "Archive reader: (none)"

    lines = ["Archive reader: AST extraction (static)", reader.extraction.describe()]

    names = [getattr(handler, "__name__", str(handler)) for handler in reader.handlers]
    if names:
        lines.append(f"  handlers       : {', '.join(names)}")
    if reader.arc_entry_fields:
        lines.append(f"  arc_files shape: ({', '.join(reader.arc_entry_fields)})")
    return "\n".join(lines)


def format_size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renpy_unpack",
        description=(
            "Unpack Ren'Py .rpa archives by reusing the game's own archive readers. "
            "By default they are recovered from renpy/loader.py with AST extraction; "
            "--runtime and --inject use the game's own runtime instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "backends:\n"
            "  default      static AST extraction -- reads renpy/loader.py as source.\n"
            "               Safe, read-only, no game code is executed.\n"
            "  --runtime    the game's own Ren'Py runtime, in a subprocess, to read\n"
            "               archives whose decryption is compiled (e.g. RPAE/AES).\n"
            "               Nothing is written into the game directory.\n"
            "\n"
            "examples:\n"
            "  renpy_unpack.py --list\n"
            "  renpy_unpack.py --game D:\\Games\\MyGame --jobs 16\n"
            "  renpy_unpack.py --match '*.png' --match '*.webp' --skip-existing\n"
            "  renpy_unpack.py --write-core core_generated.py\n"
            "  renpy_unpack.py --runtime --list\n"
        ),
    )
    parser.add_argument(
        "--game",
        type=Path,
        default=Path.cwd(),
        help="game root directory (default: current directory, then walk upwards)",
    )
    parser.add_argument(
        "--loader",
        type=Path,
        default=None,
        help="substitute loader.py to use when the game does not ship one. The "
        "game's own renpy/loader.py always wins when present, because it is the "
        "only authority on that game's archive format",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="output directory (default: <game>/extracted_files)",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(16, (os.cpu_count() or 4) * 2),
        help="parallel extraction workers (default: %(default)s)",
    )
    parser.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="list archive contents and exit without writing anything",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be extracted without writing files",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="skip entries whose output file already exists",
    )
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        metavar="GLOB",
        help="only extract entries matching this glob (repeatable, supports **)",
    )
    parser.add_argument(
        "--ext",
        action="append",
        default=[],
        metavar="EXT",
        help="only extract entries with this extension, e.g. --ext png (repeatable)",
    )
    parser.add_argument(
        "--unsafe-pickle",
        action="store_true",
        help="use plain pickle.loads for archive indexes instead of the restricted "
        "unpickler (only for archives you trust)",
    )
    parser.add_argument(
        "--write-core",
        type=Path,
        default=None,
        metavar="PATH",
        help="also write the generated reader module to PATH for inspection",
    )
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="read archives through the game's own Ren'Py runtime instead of "
        "extracting loader.py as source. Needed for formats whose decryption is "
        "compiled (e.g. RPAE/AES); runs game code in a subprocess.",
    )
    parser.add_argument(
        "--runtime-python",
        type=Path,
        default=None,
        metavar="EXE",
        help="the game's python executable for --runtime (auto-detected from "
        "lib/py3-*/python)",
    )
    parser.add_argument(
        "--runtime-batch-mb",
        type=int,
        default=DEFAULT_BATCH_BYTES // (1024 * 1024),
        metavar="MB",
        help="peak read-batch size for --runtime (default: %(default)s)",
    )
    parser.add_argument(
        "--include-loose",
        action="store_true",
        help="also copy files that sit on disk rather than inside an archive. They "
        "are read through the game's own loader as well -- there is simply no "
        "decryption step. Works on a game with no archives at all.",
    )
    parser.add_argument(
        "--inject",
        action="store_true",
        help="let the game unpack itself: adds a generated .rpy to game/, runs the "
        "game once, then removes what it added. The most capable backend (works "
        "even when only loader.pyc ships) but needs a writable game directory.",
    )
    parser.add_argument(
        "--inject-keep-script",
        action="store_true",
        help="with --inject, leave the generated .rpy in place (the compiled .rpyc "
        "is always removed, since leaving it makes the game re-run on every start)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the progress line",
    )
    return parser


def _refuse_inject_with_plan(args) -> int | None:
    """Refuse ``--inject`` combined with ``--list``/``--dry-run``.

    The entry list only exists inside the game process, and there is no way to ask
    for it without running the game once, so "show me the list first" is not a thing
    this backend can do.  Saying so is better than printing an empty list.

    Called before any discovery as well as from ``_run_inject``: a plan-only request
    should get this reason, not an unrelated setup failure such as "this game has no
    launcher .py".
    """
    if not (args.inject and (args.list_only or args.dry_run)):
        return None
    print(
        "\nerror: --list / --dry-run cannot be combined with --inject; the entry "
        "list is only available inside the game process.\n"
        "  Use --runtime --list, or drop --list to run the extraction.",
        file=sys.stderr,
    )
    return 2


def _run_inject(unpacker: Unpacker, args) -> int:
    """Drive the ``--inject`` backend and report on it."""
    refusal = _refuse_inject_with_plan(args)
    if refusal is not None:
        return refusal

    injector = unpacker.injector
    assert injector is not None  # ensured by _load_inject_reader

    layout = injector.layout
    print()
    print("Archive reader: injected script (the game unpacks itself)")
    print(f"  interpreter    : {layout.python}")
    print(f"  game root      : {layout.game_root}")
    print(f"  launcher       : {layout.launcher.name if layout.launcher else '(none)'}")
    print(f"  output         : {injector.output}")
    print(f"  script in game/: {injector.SCRIPT_NAME}")

    print("\nrunning the game once (it exits before playing; no window should appear)...")
    try:
        outcome = unpacker.run_inject()
    except UnpackError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 3

    print()
    print("-" * 68)
    print(
        f"extracted {outcome.ok} file(s) ({format_size(outcome.bytes_written)}), "
        f"skipped {outcome.skipped}, failed {outcome.failed + outcome.missing}"
    )
    if outcome.leftovers:
        print("\nwarning: these could not be removed; delete them by hand:")
        for path in outcome.leftovers:
            print(f"  {path}")
    elif injector.keep_script:
        print(f"kept the script at {injector.script_path()}")
    else:
        print("the generated script was removed from the game directory")

    if outcome.failures:
        print("\nfailures:")
        for name, error in outcome.failures[:20]:
            print(f"  {name}: {error}")
        if len(outcome.failures) > 20:
            print(f"  ... and {len(outcome.failures) - 20} more")
        return 1

    return 0


def _warn_if_inside_game_dir(unpacker: Unpacker, entries) -> str | None:
    """Warn when extraction would land where Ren'Py scans for scripts.

    Ren'Py walks ``game/`` recursively and loads every ``.rpy``/``.rpyc`` it finds,
    so writing scripts into a subdirectory of it can define the same label twice.
    That is not hypothetical: one real game refuses to start because a previous
    unpack left ``game/extracted/`` beside ``game/rpy/``, and Ren'Py reported every
    script as "defined twice" until the stray copy was deleted.

    Only a warning: extracting into ``game/tl/`` to override a translation is a
    legitimate thing to want.

    Uses the same structural detection as the default output, so an explicitly
    chosen destination is judged by the real game directory rather than by
    ``<game_root>/game`` -- comparing against that missed the case where
    ``--game`` pointed at ``game`` itself.
    """
    try:
        output = unpacker.output_dir().resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return None

    game_dir = _renpy_game_dir(unpacker.game_root)
    if game_dir is None:
        return None
    try:
        game_dir = game_dir.resolve()
    except OSError:  # pragma: no cover
        return None

    if output != game_dir and game_dir not in output.parents:
        return None

    scripts = sum(1 for entry in entries if entry.suffix in (".rpy", ".rpyc"))
    if not scripts:
        return None

    return (
        f"warning: the output directory is inside this game's game/ directory "
        f"({output}), and {scripts} extracted file(s) are Ren'Py scripts.\n"
        f"  Ren'Py scans game/ recursively, so a later run could define the same "
        f"label twice and stop the game from starting. Consider an output "
        f"directory outside game/."
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    # Plan-only requests are answered before anything is discovered, so an
    # incompatible combination is reported as such rather than as whichever setup
    # step happens to fail first.
    refusal = _refuse_inject_with_plan(args)
    if refusal is not None:
        return refusal

    try:
        unpacker = Unpacker(
            args.game,
            loader=args.loader,
            output=args.output,
            jobs=args.jobs,
            skip_existing=args.skip_existing,
            unsafe_pickle=args.unsafe_pickle,
            globs=args.match,
            suffixes=args.ext,
            write_core=args.write_core,
            progress=False if args.quiet else None,
            runtime=args.runtime,
            runtime_python=args.runtime_python,
            runtime_batch_bytes=args.runtime_batch_mb * 1024 * 1024,
            include_loose=args.include_loose,
            inject=args.inject,
            inject_keep_script=args.inject_keep_script,
        )
    except UnpackError as exc:
        # Conflicting options are rejected by the constructor; report them like any
        # other bad input instead of letting the traceback escape.
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        unpacker.discover()
    except UnpackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print("=" * 68)
    print("Ren'Py archive unpacker")
    print("=" * 68)
    print(f"  game root      : {unpacker.game_root}")
    if unpacker.climbed_from is not None:
        print(f"  (climbed from  : {unpacker.climbed_from})")
    print(f"  archives       : {len(unpacker.archives)} file(s) in {unpacker.archive_dir}")
    for archive in unpacker.archives:
        print(f"                   - {archive.name} ({format_size(archive.stat().st_size)})")

    try:
        result = unpacker.load_reader()
    except UnpackError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        # Anything unexpected in here is our bug, not the user's, and a raw
        # traceback reads like a crash of their machine rather than of this tool.
        # Found by testing against Ren'Py 8.3.4, whose loader differs from 8.5's in
        # several places the reader plumbing had assumed.
        print(
            f"\nerror: internal error while preparing the archive reader: "
            f"{type(exc).__name__}: {exc}\n"
            f"  This is a renpy_unpack bug, not a problem with your game. Please\n"
            f"  re-run with --write-core to capture the generated module and report\n"
            f"  it together with your game's Ren'Py version (game/script_version.txt).",
            file=sys.stderr,
        )
        return 3

    if args.inject:
        return _run_inject(unpacker, args)

    print()
    print(describe_extraction(unpacker))
    print(f"  entries        : {len(unpacker.entries)} unique file(s)")
    if unpacker.skipped_index_entries:
        print(
            f"  unusable index : {len(unpacker.skipped_index_entries)} "
            f"({', '.join(unpacker.skipped_index_entries[:4])})"
        )

    selected = unpacker.selected_entries()
    if unpacker.filters:
        print(f"  after filters  : {len(selected)} file(s)")

    if args.list_only:
        print()
        for entry in selected:
            print(f"  {entry.name}")
        return 0

    if args.dry_run:
        print(f"\ndry run: would extract {len(selected)} file(s) to {unpacker.output_dir()}")
        warning = _warn_if_inside_game_dir(unpacker, selected)
        if warning:
            print(f"\n{warning}", file=sys.stderr)
        return 0

    if not selected:
        print("\nnothing to extract.")
        return 0

    warning = _warn_if_inside_game_dir(unpacker, selected)
    if warning:
        print(f"\n{warning}", file=sys.stderr)

    if unpacker.external is not None:
        print(
            f"backend: live runtime, batch limit "
            f"{format_size(unpacker.runtime_batch_bytes)}"
        )
    else:
        print(f"extracting to {unpacker.output_dir()} with {unpacker.jobs} worker(s)...")

    try:
        stats = unpacker.extract(selected)
    finally:
        # The runtime backend keeps a scratch directory; it is named after the
        # output directory and must not outlive the run.
        unpacker.close()

    print()
    print("-" * 68)
    print(
        f"extracted {stats.extracted} file(s) ({format_size(stats.bytes_written)}), "
        f"skipped {stats.skipped}, failed {stats.failed}"
    )
    if result is not None and result.shim_names:
        print(f"runtime shims used: {', '.join(result.shim_names)}")
    if stats.failures:
        print("\nfailures:")
        for name, error in stats.failures[:20]:
            print(f"  {name}: {error}")
        if len(stats.failures) > 20:
            print(f"  ... and {len(stats.failures) - 20} more")
        return 1

    return 0
