"""Optional backend: read archives through the game's *own* live Ren'Py runtime.

The default backend reads ``renpy/loader.py`` as source (see ``ast_extract``).
That covers everything written in Python, but it cannot reach decryption that
lives in a **compiled** module -- one real game ships ``renpy.aescrypt`` as a Rust
extension inside ``librenpython.dll`` and uses it for an AES-256-CTR archive
format.  For those, the only correct approach is to let the game's own runtime do
the work.

How it stays non-invasive
-------------------------
* The probe is passed to the interpreter with ``-c``, so **nothing is written into
  the game directory**.
* Results stream back over a pipe, so there is no scratch file at all -- which is
  what makes ``--list`` work on a read-only Steam install.
* The probe exits before Ren'Py opens a window or compiles scripts; Ren'Py's own
  argparse rejecting our extra arguments is itself a clean, side-effect-free exit.

The trade-off is real: this runs the game's code in a subprocess, so it is
strictly opt-in (``--runtime``) and slower than the static backend.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ExternalRuntime",
    "ProbeError",
    "ProbeResult",
    "RuntimeLayout",
    "RuntimeUnavailable",
    "child_environment",
    "discover_runtime",
    "parse_stream",
]

BANNER = "RPA-PROBE-1"
DATA = "DATA"
END = "END"

#: Hard cap on how many names one invocation may carry.  Names travel as command
#: line arguments, and Windows caps a command line at 32767 characters -- a real
#: game with 2003 members produced 86 KB of names and every single one failed with
#: WinError 206 until this cap existed.
MAX_NAMES_PER_BATCH = 200

#: Default per-invocation byte budget.  Each batch pays one Ren'Py start-up, so
#: batches want to be large; the cap only bounds peak memory, since a batch's
#: payload is materialised whole.
DEFAULT_BATCH_BYTES = 512 * 1024 * 1024


class RuntimeUnavailable(RuntimeError):
    """The game's own interpreter/runtime could not be located or started."""


class ProbeError(RuntimeError):
    """The probe ran but reported a failure."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeLayout:
    """Where the game's own interpreter and Ren'Py live."""

    python: Path
    game_root: Path
    renpy_dir: Path
    launcher: Path | None

    def describe(self) -> str:
        return f"{self.python} (game root {self.game_root})"


def _platform_tag() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _platform_names() -> tuple[str, ...]:
    """Interpreter file names for the *current* platform, best first.

    ``python.exe`` is preferred over ``pythonw.exe`` on purpose.  Both are present
    in a Windows Ren'Py build, but ``pythonw`` is a GUI-subsystem binary with **no
    stdout handle**: the probe's results travel over a pipe, so choosing
    ``pythonw`` yields an empty stream.  It only appeared to work while bootstrap
    happened to bail out before writing anything.

    Ren'Py ships one ``lib/py3-<platform>`` per supported target, so a Windows
    install also carries a Linux interpreter; picking the wrong one fails with
    ``WinError 193`` (not a valid Win32 application) on Windows and
    ``Exec format error`` elsewhere, so the platform is filtered first too.
    """
    if os.name == "nt":
        return ("python.exe", "pythonw.exe")
    return ("python3", "python", "pythonw")


def _python_candidates(game_root: Path) -> list[Path]:
    names = _platform_names()
    want = _platform_tag()

    lib = game_root / "lib"
    directories = sorted(lib.glob("py3-*")) if lib.is_dir() else []
    ordered = [d for d in directories if want in d.name.lower()]
    ordered += [d for d in directories if want not in d.name.lower()]

    found: list[Path] = []
    for directory in ordered:
        if not directory.is_dir():
            continue
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                found.append(candidate)
    if found:
        return found

    for name in names:
        candidate = lib / name
        if candidate.is_file():
            found.append(candidate)
    return found


def _defines_launcher_hooks(path: Path) -> bool:
    """Whether *path* is the game's launcher, judged by the hooks it must define.

    ``renpy.bootstrap`` calls ``renpy.__main__.path_to_gamedir`` and
    ``path_to_logdir``, so a real launcher defines them.  Filename heuristics are
    unreliable: one real game's folder also holds unrelated ``.py`` files left
    behind by other people's unpackers (``core.py``, ``unpacker_universal.py``),
    and alphabetical order picked one of those.
    """
    try:
        source = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    return "def path_to_gamedir" in source or "def path_to_renpy_base" in source


def _launcher_candidates(game_root: Path) -> list[Path]:
    """Launcher scripts, best candidate first.

    Preference order: files defining the distributor hooks, then the one matching
    the game's executable, then anything else.
    """
    everything = [
        path
        for path in sorted(game_root.glob("*.py"))
        if path.name not in ("renpy.py", "probe.py")
    ]
    hooks = [path for path in everything if _defines_launcher_hooks(path)]
    if hooks:
        # Prefer the one named after the executable when several qualify.
        stems = {exe.stem for exe in game_root.glob("*.exe")}
        hooks.sort(key=lambda path: (path.stem not in stems, path.name))
        return hooks
    return everything


def discover_runtime(game_root: Path, *, python: Path | None = None) -> RuntimeLayout:
    """Locate the game's bundled interpreter, or raise ``RuntimeUnavailable``.

    An explicit *python* is trusted (and must exist).  Otherwise the game's
    ``lib/py3-*/python`` is preferred, since only that interpreter has the native
    Ren'Py modules registered as builtins -- a system Python cannot import them at
    all.
    """
    game_root = Path(game_root).resolve()
    renpy_dir = game_root / "renpy"
    if not renpy_dir.is_dir():
        raise RuntimeUnavailable(
            f"{game_root} has no renpy/ directory; that is not a game root"
        )

    if python is not None:
        python = Path(python)
        if not python.is_file():
            raise RuntimeUnavailable(f"--runtime-python does not exist: {python}")
    else:
        candidates = _python_candidates(game_root)
        if not candidates:
            raise RuntimeUnavailable(
                "no bundled interpreter found (looked for lib/py3-*/python). "
                "Pass --runtime-python to point at the game's python executable, "
                "or drop --runtime to use the static backend."
            )
        python = candidates[0]

    launchers = _launcher_candidates(game_root)
    return RuntimeLayout(
        python=python,
        game_root=game_root,
        renpy_dir=renpy_dir,
        launcher=launchers[0] if launchers else None,
    )


# ---------------------------------------------------------------------------
# Stream protocol
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    """Everything the probe managed to report."""

    archives: int = 0
    aescrypt_attrs: list[str] = field(default_factory=list)
    #: ``(archive name, entry name, declared size)``
    index: list[tuple[str, str, int]] = field(default_factory=list)
    #: ``name -> bytes`` for the members the probe was asked for.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Names the archives did not provide.
    missing: list[str] = field(default_factory=list)
    traceback: str = ""

    @property
    def has_aescrypt(self) -> bool:
        return bool(self.aescrypt_attrs)


class _LineReader:
    """Reads UTF-8 lines from a binary stream, tracking position."""

    def __init__(self, stream):
        self.stream = stream

    def line(self) -> str:
        raw = self.stream.readline()
        if not raw:
            raise ProbeError("the probe's output ended unexpectedly")
        return raw.decode("utf-8", "backslashreplace").rstrip("\r\n")


def parse_stream(stream, *, want_files: bool = True) -> ProbeResult:
    """Parse the probe's stdout.

    Kept separate from process handling so it can be tested exhaustively without a
    Ren'Py runtime present.
    """
    reader = _LineReader(stream)
    result = ProbeResult()

    banner = reader.line()
    if banner == "error":
        # The probe reports failures in-band; read them rather than complaining
        # about a malformed banner.
        lines: list[str] = []
        for _ in range(200):
            try:
                line = reader.line()
            except ProbeError:
                break
            lines.append(line)
            if len(lines) > 1 and not line.strip():
                break
        result.traceback = "\n".join(lines)
        return result
    if banner != BANNER:
        raise ProbeError(
            f"unexpected probe banner {banner!r}; the runtime produced no "
            f"recognisable payload"
        )

    status = reader.line()
    if status == "error":
        lines: list[str] = []
        for _ in range(200):
            try:
                line = reader.line()
            except ProbeError:
                break
            lines.append(line)
            if len(lines) > 1 and not line.strip():
                break
        result.traceback = "\n".join(lines)
        return result
    if status != "ok":
        raise ProbeError(f"unexpected probe status {status!r}")

    result.archives = int(reader.line())

    marker = reader.line()
    if marker.startswith("aescrypt"):
        flag = marker.split("\t", 1)[1] if "\t" in marker else "0"
        attrs = reader.line()
        if flag == "1":
            result.aescrypt_attrs = [a for a in attrs.split(",") if a]

    entry_count = int(reader.line())
    for _ in range(entry_count):
        record = reader.line()
        try:
            archive, name, size = record.split("\t")
        except ValueError as exc:
            raise ProbeError(f"malformed index record {record!r}") from exc
        result.index.append((archive, name, int(size)))

    marker = reader.line()
    if marker != DATA:
        raise ProbeError(f"expected the data section, found {marker!r}")

    while True:
        header = reader.line()
        if header == END:
            break
        parts = header.split("\t")
        if parts[0] == "MISSING":
            result.missing.append(parts[1])
            continue
        if parts[0] != "FILE":
            raise ProbeError(f"unknown data record {header!r}")

        _, name, size_text = parts
        size = int(size_text)
        chunk = stream.read(size) if size else b""
        if chunk is None or len(chunk) != size:
            raise ProbeError(
                f"truncated payload for {name!r}: wanted {size} bytes, "
                f"got {len(chunk or b'')}"
            )
        if want_files:
            result.files[name] = chunk

    return result


# ---------------------------------------------------------------------------
# Process handling
# ---------------------------------------------------------------------------


def _probe_source() -> str:
    return Path(__file__).with_name("probe.py").read_text(encoding="utf-8")


def child_environment() -> dict[str, str]:
    """Environment for the game's interpreter -- inert, and write-free.

    ``RENPY_DISABLE_LOG`` stops Ren'Py from dropping a log next to the game.
    ``PYTHONDONTWRITEBYTECODE`` matters just as much and was missed for a long time:
    importing the game's ``renpy`` package compiles it, and CPython caches the
    result in ``renpy/__pycache__/`` *inside the installation*.  One run against a
    real Steam install left 52 ``.pyc`` files there, in a backend whose whole point
    is that the game directory is read-only for us.
    """
    env = dict(os.environ)
    env.setdefault("RENPY_DISABLE_LOG", "1")
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


class ExternalRuntime:
    """Runs the game's own Ren'Py runtime to index and read its archives."""

    def __init__(
        self,
        layout: RuntimeLayout,
        *,
        batch_bytes: int = DEFAULT_BATCH_BYTES,
        max_names: int = MAX_NAMES_PER_BATCH,
        timeout: float = 900.0,
    ):
        self.layout = layout
        self.batch_bytes = max(1, int(batch_bytes))
        self.max_names = max(1, int(max_names))
        self.timeout = timeout
        #: Filled in by :meth:`manifest` so callers can report on it afterwards.
        self.last_manifest: ProbeResult | None = None

    def close(self) -> None:
        """Nothing to release; kept for symmetry with the unpacker's lifecycle."""

    def __enter__(self) -> ExternalRuntime:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- invocation --------------------------------------------------------

    def invoke(self, names: list[str]) -> ProbeResult:
        """Run the probe once, asking it for *names* (may be empty)."""
        command = [
            str(self.layout.python),
            "-c",
            _probe_source(),
            str(self.layout.game_root),
            *names,
        ]
        env = child_environment()

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            # A batch too large for the platform's command line lands here
            # (WinError 206 / E2BIG).  Say so, because the symptom -- every file in
            # the batch failing -- looks nothing like the cause.
            raise ProbeError(
                f"could not start the game's runtime with {len(names)} name "
                f"argument(s): {exc}. This is usually the platform's command-line "
                f"length limit; lower --runtime-batch-mb."
            ) from exc

        try:
            assert process.stdout is not None
            result = parse_stream(process.stdout)
        except ProbeError:
            process.kill()
            process.wait(timeout=30)
            raise

        try:
            _, stderr = process.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ProbeError(
                f"the game's runtime did not finish within {self.timeout:.0f}s"
            ) from None

        if result.traceback:
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            raise ProbeError(
                "the probe failed inside the game's runtime:\n"
                + result.traceback
                + (f"\n--- stderr ---\n{detail[-2000:]}" if detail else "")
            )
        return result

    # -- public API --------------------------------------------------------

    def manifest(self) -> ProbeResult:
        """Index the archives without reading any member data."""
        self.last_manifest = self.invoke([])
        return self.last_manifest

    def batches(self, sizes: dict[str, int]) -> list[list[str]]:
        """Split *sizes* into batches bounded by bytes and by name count.

        Each batch costs one Ren'Py start-up, so batches want to be large; the caps
        bound peak memory (a batch's payload is materialised whole) and keep the
        argument list inside the platform's command-line limit.
        """
        batches: list[list[str]] = []
        current: list[str] = []
        running = 0
        for name, size in sizes.items():
            too_big = current and running + size > self.batch_bytes
            too_many = len(current) >= self.max_names
            if too_big or too_many:
                batches.append(current)
                current, running = [], 0
            current.append(name)
            running += size
        if current:
            batches.append(current)
        return batches

    def read_selected(self, sizes: dict[str, int]) -> ProbeResult:
        """Read every requested member, batching to bound peak memory."""
        merged = ProbeResult()
        for batch in self.batches(sizes):
            result = self.invoke(batch)
            merged.archives = merged.archives or result.archives
            merged.aescrypt_attrs = merged.aescrypt_attrs or result.aescrypt_attrs
            if not merged.index:
                merged.index = result.index
            merged.files.update(result.files)
            merged.missing.extend(result.missing)
        return merged

    def read_all(self) -> ProbeResult:
        """Index the archives and read every member."""
        manifest = self.manifest()
        sizes = {name: size for _archive, name, size in manifest.index if name}
        result = self.read_selected(sizes)
        result.index = manifest.index or result.index
        result.archives = manifest.archives
        result.aescrypt_attrs = manifest.aescrypt_attrs or result.aescrypt_attrs
        return result

    def load(self, name: str) -> bytes | None:
        """Read one member, or ``None`` when no archive provides it."""
        result = self.invoke([name])
        return result.files.get(name)
