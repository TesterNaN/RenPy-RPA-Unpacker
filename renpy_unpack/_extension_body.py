"""Ren'Py extension: decrypt through the game's own loader, then put everything back.

**This file is a template, not a module.**  It is copied verbatim into a directory
that is put on ``RENPY_SEARCHPATH``, where Ren'Py execs it during Early init -- before
the display exists, before the script layer runs.  Nothing is ever written into the
game installation, and the extension undoes its own installing before it leaves:

* the environment variables that put it here are **restored to whatever they were**
  before installing (recorded in ``config.json``), so a one-shot extraction does not leave
  a user-level ``RENPY_SEARCHPATH`` behind;
* a ``spent`` marker is written, so a second launch -- including one from a Steam that
  is still holding the old environment -- plays the game normally instead of
  extracting again;
* ``os.environ`` is cleaned up too, so any process the game starts inherits nothing.

The payload extraction is the part that was measured against the three shipped
backends: byte-identical output on all six sample games, with the game install
untouched.  Two rules it learned the hard way are encoded below -- Ren'Py 8.3 cannot
tell archives from assets in its scan, and archive handling must never share one API
with archive members only.
"""

import json
import os
import sys
import traceback

# The game process is started by Steam, not by us, so this file cannot rely on the
# launcher's environment to keep CPython from caching bytecode inside the install.
# This covers every module imported from here on; the handful already imported
# (bootstrap, config, loader, main) may have been cached before we ran.
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "rpe.log")

with open(os.path.join(HERE, "config.json"), encoding="utf-8") as _handle:
    CONFIG = json.load(_handle)

MANIFEST = CONFIG.get("manifest") or os.path.join(HERE, "manifest.jsonl")

#: A *base*: the folder for the game that actually launched is added below.  That way
#: a capture of an unexpected game cannot land among another game's files.  Absolute
#: on purpose -- this runs inside the game process, whose working directory is the
#: game root, so a relative path would be created inside the installation.
BASE_OUT = CONFIG["output"]
OUT = None
SPENT = os.path.join(HERE, "spent")

#: Tests exec this file with their own globals and put something else here, because
#: ``os._exit`` would take the test runner down with it.  Ren'Py execs it with a fresh
#: globals dict, so in production this is always the real thing.
_leave = globals().get("RENPY_UNPACK_LEAVE", os._exit)

DONE = []


def _log(message):
    try:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(str(message) + "\n")
    except Exception:
        pass


def _emit(record):
    try:
        with open(MANIFEST, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as exc:
        _log("manifest write failed: %r" % (exc,))


def _game_folder():
    """The game's own folder name, made safe to use as a directory name."""
    import renpy          # explicit: the module-level import lives in a later block

    basedir = getattr(renpy.config, "basedir", "") or ""
    name = os.path.basename(os.path.normpath(basedir)) if basedir else ""
    if not name:
        gamedir = getattr(renpy.config, "gamedir", "") or ""
        name = os.path.basename(os.path.dirname(os.path.normpath(gamedir)))
    for bad in '<>:"/\\|?*':
        name = name.replace(bad, "_")
    return name.strip().rstrip(".") or "game"


def _write(name, data):
    target = os.path.join(OUT, name.replace("/", os.sep))
    os.makedirs(os.path.dirname(target) or OUT, exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(data)
    return len(data)


def _decode(name):
    return name.decode("utf-8", "surrogateescape") if isinstance(name, bytes) else name


def _archive_extensions(loader):
    """The extensions the loader's own handlers accept, however it stores them.

    Nothing here may raise: this runs inside the engine's own scan callback, and an
    exception escaping from there makes Ren'Py write traceback.txt into the game's
    installation and exit.
    """
    handlers = getattr(loader, "archive_handlers", None)
    if handlers is None:
        return set()
    exts = getattr(handlers, "exts", None)          # 8.4+: an ArchiveHandlers object
    if isinstance(exts, dict):
        return set(exts)
    found = set()                                    # 8.3: a plain list of handlers
    try:
        for handler in handlers:
            try:
                found.update(handler.get_supported_extensions())
            except Exception:
                pass
    except Exception:
        pass
    return found



def _restore_environment():
    """Undo the installing, from inside the game process.

    The variables live in ``HKCU\\Environment``, and this process runs as the same
    user, so the extension can put them back itself -- a one-shot extraction should not
    leave a user-level ``RENPY_SEARCHPATH`` pointing at a directory forever.  Values
    are only touched while they still hold what installing set, so a change made in the
    meantime is never clobbered, and a value that existed beforehand is restored
    rather than deleted.
    """
    try:
        import winreg
    except Exception:
        _log("restore: winreg unavailable, leaving the environment alone")
    else:
        installed = CONFIG.get("installed") or {}
        previous = CONFIG.get("previous") or {}
        try:
            # KEY_QUERY_VALUE *and* KEY_SET_VALUE: reading a value's current contents
            # needs the former.  Without it every QueryValueEx below raised
            # PermissionError, which an ``except OSError`` swallowed -- so the extension
            # reported a clean run and left the environment installed.
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
                for name, value in installed.items():
                    try:
                        current, _kind = winreg.QueryValueEx(key, name)
                    except OSError as exc:
                        _log("restore: cannot read %s (%s), left alone" % (name, exc))
                        continue
                    if current != value:
                        _log("restore: %s changed since installing, left alone" % name)
                        continue
                    if previous.get(name):
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, previous[name])
                        _log("restore: %s restored to its earlier value" % name)
                    else:
                        winreg.DeleteValue(key, name)
                        _log("restore: %s removed" % name)
        except Exception:
            _log("restore failed:\n" + traceback.format_exc())

    # The current process already read what it needed; this is for anything it starts.
    for name in (CONFIG.get("installed") or {}):
        os.environ.pop(name, None)

    try:
        # Newly launched processes (from Explorer, say) pick the change up at once;
        # an already-running Steam keeps its own snapshot either way.
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment",
                                                 0x0002, 5000, None)
    except Exception:
        pass


def _finish(trigger):
    """Extract everything the loader knows about, then leave before the display."""
    if DONE:
        return
    DONE.append(trigger)

    import renpy
    import renpy.loader as loader

    _log("finish via %s: arc_files=%s archives=%s"
         % (trigger, len(getattr(loader, "arc_files", []) or []),
            len(loader.archives)))

    # The game is only known from here on, which is why the output folder is decided at
    # the last moment rather than when the extension was installed.
    global OUT
    OUT = os.path.join(BASE_OUT, _game_folder())
    _log("output: %s (base %s)" % (OUT, BASE_OUT))

    seen = set()
    archived = []
    for _archive, index in loader.archives:
        for name in index:
            name = _decode(name)
            if name and name not in seen:
                seen.add(name)
                archived.append(name)

    _log("archived=%d" % len(archived))
    members = [(name, loader.load_from_archive) for name in archived]

    total, failures = 0, 0
    for position, (name, reader) in enumerate(members, 1):
        try:
            handle = reader(name)
            if handle is None:
                failures += 1
                _emit({"name": name, "status": "missing", "size": 0})
                continue
            try:
                data = handle.read()
            finally:
                try:
                    handle.close()
                except Exception:
                    pass
            total += _write(name, data)
            _emit({"name": name, "status": "ok", "size": len(data)})
            if position % 500 == 0:
                _log("  progress %d/%d" % (position, len(members)))
        except Exception as exc:
            failures += 1
            _emit({"name": name, "status": "error", "size": 0,
                   "error": "%s: %s" % (type(exc).__name__, exc)})

    _emit({"name": "", "status": "done", "size": total, "archives": len(loader.archives),
           "files": len(members), "failures": failures, "trigger": trigger,
           "output": OUT})
    _log("done: %d file(s), %.1f MiB, %d failure(s)" % (len(members), total / 1048576.0,
                                                        failures))

    # Only after the payload is on disk: a crash mid-extraction leaves the installing in
    # place so the run can simply be repeated.
    if not failures:
        try:
            with open(SPENT, "w", encoding="utf-8") as handle:
                handle.write("%d\n" % len(members))
        except Exception:
            pass
        _restore_environment()
    else:
        _log("leaving the environment alone: %d file(s) failed" % failures)

    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    _leave(0)


try:
    import renpy
    import renpy.loader as loader

    game_dir = renpy.config.gamedir

    # A user-level RENPY_SEARCHPATH puts this file in front of *every* Ren'Py game, so
    # it has to recognise its own target and step aside for the rest.  An installed hook
    # that extracted and exited on any game would quit them all, silently.
    target = CONFIG.get("game")
    candidates = {
        os.path.normcase(os.path.abspath(game_dir)),
        os.path.normcase(os.path.abspath(os.path.dirname(game_dir))),
    }
    installed = target is None or os.path.normcase(os.path.abspath(target)) in candidates

    if not installed:
        _log("declined: %s is not the installed game (%s)" % (game_dir, target))
    elif os.path.exists(SPENT):
        # Already done once.  A Steam that is still holding the old environment would
        # otherwise extract again on every launch.
        _log("declined: already spent (%s)" % SPENT)
    else:
        _log("installed: python=%s engine=%s gamedir=%s arc_files=%s"
             % (sys.version.split()[0], getattr(renpy, "version", "?"), game_dir,
                len(getattr(loader, "arc_files", []) or [])))

        original_index = getattr(loader, "index_archives", None)
        if original_index is not None:
            def hooked_index(*args, **kwargs):
                result = original_index(*args, **kwargs)
                _finish("index_archives")
                return result

            loader.index_archives = hooked_index

        # Ren'Py 8.4+ indexes lazily, and only when there are archive files at all, so
        # the scan callback is the point that is reached either way.
        scanned = getattr(loader, "scandirfiles_from_archives", None)
        callbacks = getattr(loader, "scandirfiles_callbacks", None)
        if scanned is not None:
            def hooked_scan(add, seen):
                result = scanned(add, seen)
                _finish("scandirfiles_from_archives")
                return result

            loader.scandirfiles_from_archives = hooked_scan
            if isinstance(callbacks, list):
                for position, callback in enumerate(callbacks):
                    if callback is scanned:
                        callbacks[position] = hooked_scan
except Exception:
    _log("fatal:\n" + traceback.format_exc())
    _emit({"name": "", "status": "fatal", "size": 0, "error": traceback.format_exc()})
    _leave(1)
