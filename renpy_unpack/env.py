"""The environment-variable backend: env, wait, unset.

Nothing is written into the game installation at all.  A directory holding a Ren'Py extension is put on
``RENPY_SEARCHPATH`` -- a **user-level** variable, on purpose, because Steam hands a
game the environment *it* was started with, and a launch option cannot carry one --
Ren'Py then execs the extension during Early init, and the extension extracts with the game's own
loader and puts the environment back the way it found it.

Measured, on six real games: byte-identical output to the three shipped backends with
the installation untouched (``+0/-0/~0`` over a full before/after file snapshot), and
through a registry-provided environment on a real Steam install.

The registry is reached through the module-level functions below so tests can replace
them; nothing else in this module touches the machine.
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

#: What installing sets, in the order it is reported.  All three are user-level ``REG_SZ``
#: values under ``HKCU\\Environment``.
INSTALLED_VARIABLES = ("RENPY_SEARCHPATH", "RENPY_LOG_BASE", "PYTHONDONTWRITEBYTECODE")

#: The generated extension Ren'Py execs.  Ren'Py loads ``*.rpe`` and ``*.rpe.py`` from
#: its base directory and from every searchpath entry during Early init.
EXTENSION_NAME = "zz_renpy_unpack.rpe.py"

USER_KEY = "Environment"


class EnvError(Exception):
    """Something the caller has to fix before installing can mean anything."""


# -- registry seam --------------------------------------------------------


def read_user_variable(name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_KEY) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def write_user_variable(name: str, value: str) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, USER_KEY) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def delete_user_variable(name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, USER_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
    except OSError:
        pass


def broadcast_change() -> None:
    """Tell running programs the environment changed (Explorer picks it up at once)."""
    try:
        import ctypes

        # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment",
                                                 0x0002, 5000, None)
    except Exception:
        pass


def require_windows() -> None:
    if sys.platform != "win32":
        raise EnvError(
            "the environment-variable backend is Windows-only: it relies on the "
            "user-level environment Steam hands to a game, and on Ren'Py's own "
            "RENPY_SEARCHPATH being read from the desktop launcher. Use --runtime or "
            "--inject instead."
        )


# -- state ----------------------------------------------------------------


def state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or str(Path.home())
    return Path(base) / "renpy-unpack"


def searchpath_dir() -> Path:
    return state_dir() / "searchpath"


def config_path() -> Path:
    return searchpath_dir() / "config.json"


def manifest_path() -> Path:
    return searchpath_dir() / "manifest.jsonl"


def log_path() -> Path:
    return searchpath_dir() / "rpe.log"


def extension_path() -> Path:
    return searchpath_dir() / EXTENSION_NAME


@dataclass
class InstallResult:
    """What installing did, and what the caller has to do next."""

    searchpath: Path
    output: Path
    game_root: Path | None
    variables: dict = field(default_factory=dict)
    previous: dict = field(default_factory=dict)
    lines: list = field(default_factory=list)

    def describe(self) -> str:
        return "\n".join(self.lines)


def _previous_values() -> dict:
    return {name: read_user_variable(name) for name in INSTALLED_VARIABLES}


def install(game_root: Path | None, output: Path) -> InstallResult:
    """Write the extension, remember the current environment, then change it.

    ``game_root`` may be ``None``, and then the extension accepts *any* Ren'Py game.
    That is the point of this backend: it is installed before a launcher starts anything,
    so the game that starts is not necessarily the one that was asked for.

    ``output`` is a *base*.  The extension appends a folder named after the game it
    ends up in -- the same layout the other backends default to -- so capturing an
    unexpected game cannot mix two games' files together.

    Idempotent: installing again clears a previous run's marker and manifest, so the next
    launch extracts again instead of declining.
    """
    require_windows()

    output = Path(os.path.abspath(output))
    target = None
    if game_root is not None:
        target = Path(os.path.abspath(game_root))
        if not target.is_dir():
            raise EnvError(f"not a directory: {target}")

    searchpath = searchpath_dir()
    searchpath.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("_extension_body.py")
    extension_path().write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    # A fresh env means "extract on the next launch", so anything a previous run left
    # behind that would make the extension decline has to go.
    for stale in (manifest_path(), log_path(), searchpath / "spent"):
        stale.unlink(missing_ok=True)

    logs = state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    previous = _previous_values()
    variables = {
        "RENPY_SEARCHPATH": str(searchpath),
        "RENPY_LOG_BASE": str(logs),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    config_path().write_text(
        json.dumps(
            {
                "output": str(output),
                "game": str(target) if target else None,                "manifest": str(manifest_path()),
                "installed": variables,
                "previous": {name: value for name, value in previous.items()},
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    for name, value in variables.items():
        write_user_variable(name, value)
    broadcast_change()

    result = InstallResult(searchpath=searchpath, output=output, game_root=target,
                       variables=variables, previous=previous)
    changed = [name for name, value in variables.items() if previous.get(name) != value]
    result.lines = [
        "Installed. Nothing was written into the game, and nothing is extracted yet.",
        "",
        "  1. Start the game -- double-click it, or launch it from whatever store or",
        "     launcher you use.  A launcher that is *already running* hands down the",
        "     running will not see this.",
        "  2. Launch "
        + (f"{target.name!r}" if target else "the game you want")
        + " from Steam.  It extracts through its own loader and quits before any",
        "     window appears."
        + ("  Any other Ren'Py game is left alone." if target
           else "  Only the first one does: this is a one-shot."),
        "  3. Run this tool again with --unset-env when you are done.  It also",
        "     unsets itself and refuses to run twice after a clean extraction.",
        "",
        f"  searchpath: {searchpath}",
        f"  output    : {output}",
        "              (the extension adds a folder named after the game it finds)",
        f"  set       : {', '.join(changed) if changed else '(nothing had to change)'}",
    ]
    if len(changed) < len(variables):
        result.lines.append(
            "  note      : some of those were already set to the same value"
        )
    result.lines.append(
        "  process-wide: while installed, PYTHONDONTWRITEBYTECODE and RENPY_LOG_BASE apply "
        "to every Python and Ren'Py program; --unset-env restores them"
    )
    return result


def uninstall() -> list:
    """Put the environment back, and stop the extension from being loaded again."""
    require_windows()
    actions: list = []

    configuration = {}
    if config_path().is_file():
        try:
            configuration = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            configuration = {}

    installed = configuration.get("installed") or {}
    previous = configuration.get("previous") or {}
    for name in INSTALLED_VARIABLES:
        value = installed.get(name)
        earlier = previous.get(name)
        current = read_user_variable(name)
        if value is None:
            # Never installed by us: leave whatever is there alone, but say so.
            if current is not None:
                actions.append(f"{name}: left alone (not set by us)")
            continue
        if current is None:
            actions.append(f"{name}: already gone")
        elif current != value:
            actions.append(f"{name}: changed since installing, left alone")
            continue
        if earlier:
            write_user_variable(name, earlier)
            actions.append(f"{name}: restored to its earlier value")
        else:
            delete_user_variable(name)
            actions.append(f"{name}: removed")

    broadcast_change()

    # The variable is gone, but a launcher still holding the old environment would load
    # the extension again on the next launch: take the file away as well.
    extension_path().unlink(missing_ok=True)
    (searchpath_dir() / "spent").unlink(missing_ok=True)
    return actions


# -- reading what the game did -------------------------------------------


def read_manifest() -> dict:
    """Summarise the extension's manifest: ``{files, bytes, failures, records}``."""
    summary = {"files": 0, "bytes": 0, "failures": 0, "done": False, "fatal": None,
               "output": None, "records": []}
    path = manifest_path()
    if not path.is_file():
        return summary
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        status = record.get("status")
        if status == "ok":
            summary["files"] += 1
            summary["bytes"] += int(record.get("size") or 0)
        elif status in ("missing", "error"):
            summary["failures"] += 1
        elif status == "done":
            summary["done"] = True
            # Where it actually went: the extension adds the game's own folder, so
            # the caller cannot work this out from what it installed with.
            summary["output"] = record.get("output")
        elif status == "fatal":
            summary["fatal"] = record.get("error") or "unknown"
        summary["records"].append(record)
    return summary


def wait_for_extraction(timeout: float = 900.0, poll: float = 1.0) -> dict:
    """Block until the game has extracted, or the timeout runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = read_manifest()
        if summary["done"] or summary["fatal"]:
            return summary
        time.sleep(poll)
    summary = read_manifest()
    summary["timed_out"] = True
    return summary


def read_log(tail: int = 8) -> list:
    path = log_path()
    if not path.is_file():
        return []
    lines = [line for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
             if line.strip()]
    return lines[-tail:]


def _inside(inner: Path, outer: Path) -> bool:
    try:
        inner.relative_to(outer)
    except ValueError:
        return False
    return True


def launch_once(game_root: Path, output: Path, *, timeout: float = 600.0) -> dict:
    """Start the game once with the extension on its search path, then clean up.

    Nothing persistent is touched and nothing is written into the game: the extension
    lives in this tool's own state directory, and the three environment variables go
    into one child process -- which is what any launcher started after this was set hands down.
    The game exits by itself before any window appears.
    """
    from .runtime import discover_runtime

    game_root = Path(os.path.abspath(game_root))
    output = Path(os.path.abspath(output))
    layout = discover_runtime(game_root)

    searchpath = searchpath_dir()
    searchpath.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("_extension_body.py")
    extension_path().write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    for stale in (manifest_path(), log_path(), searchpath / "spent"):
        stale.unlink(missing_ok=True)

    logs = state_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    config_path().write_text(
        json.dumps(
            {
                "output": str(output),
                # No game is pinned: we are about to launch whatever this is.
                "game": None,                "manifest": str(manifest_path()),
                # Nothing outside this process was changed, so there is nothing for the
                # extension to restore -- its restore step then does nothing at all.
                "installed": {},
                "previous": {},
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    command = [str(layout.python), str(layout.launcher)]
    environment = dict(os.environ)
    environment["RENPY_SEARCHPATH"] = str(searchpath)
    environment["RENPY_LOG_BASE"] = str(logs)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    started = time.time()
    try:
        completed = subprocess.run(
            command,
            cwd=str(layout.game_root),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        code = completed.returncode
    except subprocess.TimeoutExpired:
        code = -1
    except OSError as exc:
        raise EnvError(f"could not start the game ({layout.python}): {exc}") from exc

    summary = read_manifest()
    summary["exit"] = code
    summary["seconds"] = round(time.time() - started, 1)
    summary["log"] = read_log(6)
    summary["game"] = str(layout.game_root)

    # Take the extension away again: nothing should stay armed, even though nothing
    # outside this tool's own state directory was ever touched.
    extension_path().unlink(missing_ok=True)
    (searchpath / "spent").unlink(missing_ok=True)
    return summary


def _launch_cli(*, game=None, output=None, stream=None, errors=None) -> int:
    """Console path for ``--env-launch``."""
    out = stream if stream is not None else sys.stdout
    err = errors if errors is not None else sys.stderr

    root = Path(game) if game else Path.cwd()
    base = Path(os.path.abspath(output if output is not None else Path.cwd() / "extracted"))

    print(f"Starting {root} once with the extension on its search path...", file=out)
    print(f"  output base: {base}  (each game gets its own folder under it)", file=out)
    try:
        summary = launch_once(root, base)
    except EnvError as exc:
        print(f"error: {exc}", file=err)
        return 2

    if summary.get("fatal"):
        print("error: the extension failed inside the game:\n" + str(summary["fatal"]),
              file=err)
        for line in summary.get("log", []):
            print("  " + line, file=err)
        return 1

    from .core import format_size

    print(f"\nextracted {summary['files']} file(s) ({format_size(summary['bytes'])}), "
          f"failed {summary['failures']}", file=out)
    if summary.get("output"):
        print(f"  into {summary['output']}", file=out)
    print(f"  the game exited by itself (code {summary['exit']}) after "
          f"{summary['seconds']}s", file=out)
    for line in summary.get("log", []):
        print("  " + line, file=out)
    return 1 if summary["failures"] else 0


def cli(*, do_env: bool = False, do_unset_env: bool = False, do_env_launch: bool = False,
        game=None, output=None,
        wait: float | None = None, stream=None,
        errors=None) -> int:
    """Console entry point for ``--env`` / ``--unset-env``; returns the exit status."""
    out = stream if stream is not None else sys.stdout
    err = errors if errors is not None else sys.stderr

    if do_env and do_unset_env:
        print("error: pick one of --env or --unset-env.", file=err)
        return 2

    if do_unset_env:
        try:
            actions = uninstall()
        except EnvError as exc:
            print(f"error: {exc}", file=err)
            return 2
        print("environment restored:", file=out)
        for action in actions:
            print(f"  {action}", file=out)
        return 0

    if do_env_launch:
        return _launch_cli(game=game, output=output, stream=out, errors=err)

    if not do_env:
        print("error: nothing to do; pass --env, --env-launch or --unset-env.", file=err)
        return 2

    root = Path(game).resolve() if game else None
    # A base directory: the extension adds a folder named after the game it finds, so
    # this is also the default for every other backend.
    default_output = Path.cwd() / "extracted"
    target = Path(os.path.abspath(output if output is not None else default_output))

    # The one thing this backend must never do is write into the installation, and a
    # careless --output is exactly how that happens.  Only checkable when the game is
    # known: without --game, the extension picks the folder from whatever launches.
    if root is not None and _inside(target, root):
        print(
            f"error: --env must extract somewhere outside the game, but {target} is "
            f"inside {root}.\n  Nothing may be written into the installation. Choose "
            f"another --output, or leave it out and get {default_output}.",
            file=err,
        )
        return 2

    try:
        result = install(root, target)
    except EnvError as exc:
        print(f"error: {exc}", file=err)
        return 2
    print(result.describe(), file=out)

    if wait is None:
        return 0

    print(f"\nwaiting up to {wait:.0f}s for the game to extract through its own loader "
          f"(launch the game)...", file=out)
    summary = wait_for_extraction(wait)

    if summary.get("fatal"):
        print("error: the extension failed inside the game:\n" + str(summary["fatal"]), file=err)
        for line in read_log(6):
            print("  " + line, file=err)
        return 1

    if not summary.get("done"):
        print(f"error: no extraction arrived within {wait:.0f}s.", file=err)
        print("  A launcher that is already running keeps the environment it was started "
              "with: restart it, or start the game directly.", file=err)
        for line in read_log(6):
            print("  " + line, file=err)
        return 1

    from .core import format_size

    print("\nextracted %d file(s) (%s), failed %d"
          % (summary["files"], format_size(summary["bytes"]), summary["failures"]), file=out)
    if summary.get("output"):
        print("  into %s" % summary["output"], file=out)
    for action in uninstall():
        print("  unset: " + action, file=out)
    return 0
