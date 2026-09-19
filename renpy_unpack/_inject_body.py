"""Body of the injected ``.rpy`` script, as a source template.

``inject.py`` wraps this in ``init python early:`` and indents it, then drops the
result into ``<game>/game/`` for Ren'Py's auto-loader to pick up.  The body is kept
as a separate file so it stays readable and lintable rather than being a giant
string literal.

Why this backend exists at all
------------------------------
Ren'Py calls ``renpy.loader.index_files()`` (``renpy/main.py:367``) *before*
``renpy.game.script.load_script()`` (``:412``), and ``init python early:`` blocks run
inside ``load_script``.  So by the time this code runs, the loader has already built
its in-memory index::

    renpy.loader.archives     [(archive path, {name: [(offset, dlen), ...]}), ...]

The script therefore reads structures that already exist and calls the game's own
``load_from_archive()``.  It never parses an archive format itself, which is why one
unchanged script works on stock RPAv3, on archives disguised as ``.dll`` with a
custom magic and reordered index fields, and on builds that ship only ``loader.pyc``.

It exits with ``os._exit(0)`` before the game plays, so no window opens.

The two things it cannot do: run where ``game/`` is not writable, and run on a game
that cannot boot at all.  ``AST`` covers both of those, which is why this is opt-in.
"""

from __future__ import annotations

# ``__CONFIG_JSON__`` is replaced by the parent with a repr()'d JSON string; using a
# literal keeps the whole thing to a single file write with nothing to clean up later.
import json
import os
import sys
import traceback

_CONFIG = json.loads(__CONFIG_JSON__)

_OUT = _CONFIG["output"]
_MANIFEST = _CONFIG["manifest"]
_ONLY = _CONFIG.get("only") or []          # extensions to include, [] = all
_SKIP_EXISTING = bool(_CONFIG.get("skip_existing"))
_LOG = os.path.join(os.path.dirname(_MANIFEST), "inject.log")


def _log(message):
    try:
        with open(_LOG, "a", encoding="utf-8") as handle:
            handle.write(str(message) + "\n")
    except Exception:
        pass


def _emit(record):
    # Append-only JSON lines: a crash mid-way still leaves the earlier records, so
    # the parent can report exactly which files made it.
    try:
        with open(_MANIFEST, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception as exc:
        _log("manifest write failed: %r" % (exc,))


def _run():
    import renpy.loader as loader

    _log("=" * 60)
    _log("inject: start")
    _log("  python   : %s" % sys.version.split()[0])
    _log("  gamedir  : %s" % renpy.config.gamedir)
    _log("  output   : %s" % _OUT)
    _log("  arc_files: %d" % len(loader.arc_files))
    _log("  archives : %d" % len(loader.archives))

    # The index is already built by now; this is belt-and-braces for a loader that
    # indexes lazily at some other point.
    if not loader.archives and loader.arc_files:
        _log("  index empty -> calling index_archives()")
        loader.index_archives()
        _log("  archives : %d" % len(loader.archives))

    try:
        import renpy.aescrypt as aescrypt

        _log("  aescrypt : %s" % [n for n in dir(aescrypt) if not n.startswith("_")])
    except Exception as exc:
        _log("  aescrypt : unavailable (%s)" % type(exc).__name__)

    # Collect every name the loader knows about, in archive order.
    names = []
    seen = set()
    for _archive, index in loader.archives:
        for name in index:
            if isinstance(name, bytes):
                name = name.decode("utf-8", "surrogateescape")
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)

    if _ONLY:
        wanted = tuple(e.lower() for e in _ONLY)
        names = [n for n in names if n.lower().endswith(wanted)]

    _log("  entries  : %d" % len(names))

    total = 0
    for position, name in enumerate(names, 1):
        target = os.path.join(_OUT, name.replace("/", os.sep))
        try:
            if _SKIP_EXISTING and os.path.isfile(target):
                _emit({"name": name, "status": "skipped", "size": 0})
                continue

            handle = loader.load_from_archive(name)
            if handle is None:
                _emit({"name": name, "status": "missing", "size": 0})
                continue
            try:
                data = handle.read()
            finally:
                try:
                    handle.close()
                except Exception:
                    pass

            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
                out.write(data)
            total += len(data)
            _emit({"name": name, "status": "ok", "size": len(data)})

            if position % 200 == 0:
                _log("  progress : %d/%d" % (position, len(names)))
        except Exception as exc:
            _emit(
                {
                    "name": name,
                    "status": "error",
                    "size": 0,
                    "error": "%s: %s" % (type(exc).__name__, exc),
                }
            )

    _log("  done     : %.1f MiB" % (total / 1048576.0))
    _emit({"name": "", "status": "done", "size": total})


try:
    _run()
except Exception:
    _log("unhandled:\n" + traceback.format_exc())
    _emit({"name": "", "status": "fatal", "size": 0, "error": traceback.format_exc()})

_log("inject: os._exit(0) before the game plays")
try:
    sys.stdout.flush()
    sys.stderr.flush()
except Exception:
    pass
os._exit(0)
