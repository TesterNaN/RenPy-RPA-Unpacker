"""Fifth backend: run the extraction inside the game's own process.

Why this exists
---------------
The other four backends all ask the filesystem something.  ``static`` wants the game's
``renpy/loader.py``; ``runtime``/``inject`` want a launcher ``.py`` and an interpreter
that will run our code; ``env`` wants ``renpy.py``.  A build that compiles the engine
into ``renpy.pyd`` and checks a file manifest deletes all four answers at once: there is
no loader to read, no launcher to drive, ``game/`` refuses new files, and the environment
is sanitised.  What such a build still has is a *running interpreter* that decrypts its
own assets -- so that is what we use.

How it works
------------
1. start the game exactly as a player would (its own exe, with Steam variables set);
2. find the process that holds ``libpython3.X.dll``;
3. find a module inside the game folder that imports CPython through its import table;
4. write a tiny stub into a zero run of an already-loaded image -- the process may have
   ACG on, which forbids *new* executable memory but allows image pages to be made
   writable/executable;
5. point one of those import slots at the stub; the stub restores the slot, calls
   ``PyRun_SimpleString`` with our payload, then tail-jumps to the original function, so
   the engine's own call still happens;
6. the payload runs on the engine's Python thread, with the GIL, and uses the engine's
   own ``renpy.loader.load_from_archive`` -- no format is guessed anywhere.

Limits, measured
----------------
Nothing here is tied to a particular Python version.  The only CPython entry point used
is ``PyRun_SimpleString``, unchanged from 2.7 to 3.13; there is no ``PyConfig``, no
vectorcall, no ``Py_NewRef``, and the GIL is never touched by hand (``PyGILState_Ensure``
from a foreign thread faults, which is why the hook borrows a thread that already holds
it).  DLLs are matched by name pattern, so ``libpython3.12.dll``, ``libpython3.9.dll``,
``python312.dll``, ``python39.dll`` and ``libpython2.7.dll`` are all recognised.

What actually decides whether it fires:

* **dynamic linkage** -- the game must import CPython through its import table.  Ren'Py
  8.4/8.5 builds do.  An 8.3-era ``librenpython.dll`` links CPython statically, never
  touches its import table for those calls, and cannot be hooked this way (measured: 12
  slots resolved, hooks installed, never called; ``PyObject_Call``'s signature from the
  standalone DLL does not appear inside it either).
* **64-bit** -- the stub is x64 machine code: REX prefixes are different instructions in
  32-bit mode, there are no R8/R9 registers, the calling convention puts arguments on the
  stack, and an IAT slot there is four bytes where this code writes eight.  The injector
  would also have to be a 32-bit process.  So a 32-bit target is refused *before* anything
  is written.  Nothing is lost in practice: Ren'Py 8.x ships ``lib/py3-windows-x86_64``
  (all six samples), 32-bit only existed in the 7.x era, and 7.x is Python 2.7 -- a payload
  this backend does not carry either.
* **Python 3 payload** -- it uses ``io``-style text files and ``exist_ok``.  Ren'Py 7.x
  (Python 2.7) would need an older payload, which has not been written because there is
  no such build here to test it against.

Windows only.  Some antivirus products classify cross-process import-table edits as
injection, which is what this is.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# CPython has shipped under several names: libpython3.12.dll (Ren'Py's own build),
# python312.dll / python39.dll (upstream CPython), libpython2.7.dll (Ren'Py 7.x).
PY_DLL = re.compile(r"^(?:lib)?python(?:3\.\d+|3\d{1,2}|2\.7)\.dll$")

#: CPython entry points that any embedded interpreter calls constantly
HOT = ("PyObject_Call", "PyDict_GetItem", "PyObject_GetAttr", "PyObject_CallObject",
       "PyObject_CallMethod", "PyObject_IsTrue", "PyObject_RichCompareBool",
       "PyImport_ImportModule", "PyDict_SetItem", "PyTuple_New", "PyLong_FromLong")

WINDOWS = sys.platform == "win32"


@dataclass
class HookResult:
    ok: bool
    files: int = 0
    total_bytes: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)


def available() -> tuple[bool, str]:
    if not WINDOWS:
        return False, "this backend edits another process's memory and is Windows only"
    return True, "starts the game and runs the payload on its own Python thread"


# --------------------------------------------------------------------------
# PE reading (offsets and export/import tables)
# --------------------------------------------------------------------------

def _pe(path: Path):
    data = Path(path).read_bytes()
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    count = struct.unpack_from("<H", data, pe_off + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe_off + 20)[0]
    opt = pe_off + 24
    magic = struct.unpack_from("<H", data, opt)[0]
    dirs = opt + (112 if magic == 0x20B else 96)
    sections = []
    for i in range(count):
        off = opt + opt_size + i * 40
        name = data[off:off + 8].rstrip(b"\0").decode("latin1")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        sections.append((name, vaddr, vsize, rawptr, rawsize))

    def rva_to_off(rva):
        for name, vaddr, vsize, rawptr, rawsize in sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + rva - vaddr
        return None

    return data, sections, dirs, rva_to_off


def _machine(path: Path) -> str:
    """``x64`` / ``x86`` / ``arm64`` / ``unknown`` from the COFF header.

    The stub this backend injects is x64 machine code, so a 32-bit game must be refused
    instead of being crashed: the same import slot would be patched with code the CPU
    cannot run.
    """
    data, _sections, _dirs, _rva = _pe(path)
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    machine = struct.unpack_from("<H", data, pe_off + 4)[0]
    return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(machine, "unknown")


def _exports(path: Path) -> dict[str, int]:
    data, _sections, dirs, rva_to_off = _pe(path)
    offset = rva_to_off(struct.unpack_from("<I", data, dirs)[0])
    if offset is None:
        return {}
    where = offset
    count_names, _, names_rva, ordinals_rva = struct.unpack_from("<IIII", data, where + 24)
    funcs_rva = struct.unpack_from("<I", data, where + 28)[0]
    names_off = rva_to_off(names_rva)
    ordinals_off = rva_to_off(ordinals_rva)
    funcs_off = rva_to_off(funcs_rva)
    # The export directory's "Base" field is the ordinal base, never part of an address.
    out = {}
    for i in range(count_names):
        name_off = rva_to_off(struct.unpack_from("<I", data, names_off + 4 * i)[0])
        name = data[name_off:data.index(b"\0", name_off)].decode("latin1")
        ordinal = struct.unpack_from("<H", data, ordinals_off + 2 * i)[0]
        out[name] = struct.unpack_from("<I", data, funcs_off + 4 * ordinal)[0]
    return out


def _iat_slots(path: Path, module_name: str) -> dict[str, int]:
    """Import name -> IAT slot RVA, scanning normal and delay-load descriptors."""
    data, _sections, dirs, rva_to_off = _pe(path)
    slots: dict[str, int] = {}
    for index in (1, 13):
        rva = struct.unpack_from("<I", data, dirs + index * 8)[0]
        if not rva:
            continue
        offset = rva_to_off(rva)
        while offset:
            desc = data[offset:offset + 20]
            if len(desc) < 20 or desc == b"\0" * 20:
                break
            lookup, _, _, name_rva, iat = struct.unpack_from("<IIIII", desc)
            name_off = rva_to_off(name_rva)
            if not name_off:
                break
            dll = data[name_off:data.index(b"\0", name_off)].decode("latin1")
            if dll.lower() == module_name.lower():
                thunk = rva_to_off(lookup) if lookup else rva_to_off(iat)
                slot_index = 0
                while thunk:
                    entry, = struct.unpack_from("<Q", data, thunk)
                    if not entry:
                        break
                    if not (entry >> 63):
                        hint_off = rva_to_off(entry & 0x7FFFFFFF)
                        nm = data[hint_off + 2:data.index(b"\0", hint_off + 2)].decode("latin1")
                        slots[nm] = iat + 8 * slot_index
                    slot_index += 1
                    thunk += 8
            offset += 20
    return slots


# --------------------------------------------------------------------------
# Win32 plumbing
# --------------------------------------------------------------------------

class _Api:
    def __init__(self) -> None:
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.psapi = ctypes.WinDLL("psapi")
        k32, psapi = self.k32, self.psapi
        k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
        k32.OpenProcess.restype = wt.HANDLE
        k32.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                      wt.DWORD, wt.DWORD]
        k32.VirtualAllocEx.restype = ctypes.c_void_p
        k32.VirtualProtectEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                        wt.DWORD, ctypes.POINTER(wt.DWORD)]
        k32.WriteProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.ReadProcessMemory.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE),
                                               wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD]
        psapi.GetModuleBaseNameW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
        psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]

    def modules(self, handle) -> dict[str, tuple[int, str]]:
        if not handle:
            return {}
        array = (wt.HMODULE * 2048)()
        needed = wt.DWORD()
        if not self.psapi.EnumProcessModulesEx(handle, array, ctypes.sizeof(array),
                                               ctypes.byref(needed), 3):
            return {}
        out = {}
        for i in range(needed.value // ctypes.sizeof(wt.HMODULE)):
            name = ctypes.create_unicode_buffer(260)
            self.psapi.GetModuleBaseNameW(handle, array[i], name, 260)
            full = ctypes.create_unicode_buffer(1024)
            self.psapi.GetModuleFileNameExW(handle, array[i], full, 1024)
            out[name.value.lower()] = (array[i], full.value)
        return out


# --------------------------------------------------------------------------
# payload
# --------------------------------------------------------------------------

def _payload(out_dir: Path, log_path: Path) -> str:
    return (
        "import os, json, time, traceback\n"
        "import renpy.loader as L\n"
        'OUT = r"%s"\n'
        'LOG = r"%s"\n'
        "def log(m):\n"
        '    with open(LOG, "a", encoding="utf-8") as fh: fh.write(str(m) + "\\n")\n'
        "try:\n"
        '    log("payload running on the engine thread")\n'
        "    for _ in range(400):\n"
        '        if getattr(L, "archives", None):\n'
        "            break\n"
        "        try:\n"
        "            L.index_archives()\n"
        "        except Exception:\n"
        "            pass\n"
        "        time.sleep(0.05)\n"
        '    log("archives ready: %%d" %% len(getattr(L, "archives", [])))\n'
        "    names = []\n"
        '    for afn, index in getattr(L, "archives", []):\n'
        "        for name in index:\n"
        '            names.append(name.decode("latin1") if isinstance(name, bytes) else name)\n'
        '    log("archives=%%d names=%%d" %% (len(getattr(L, "archives", [])), len(set(names))))\n'
        "    ok = 0\n    total = 0\n    done = []\n    failed = []\n"
        "    for name in dict.fromkeys(names):\n"
        "        try:\n"
        "            handle = L.load_from_archive(name)\n"
        "            if handle is None:\n"
        '                failed.append([name, "missing"])\n'
        "                continue\n"
        "            data = handle.read()\n"
        "            try:\n                handle.close()\n            except Exception:\n                pass\n"
        '            target = os.path.join(OUT, name.replace("/", os.sep))\n'
        "            parent = os.path.dirname(target)\n"
        "            if parent:\n                os.makedirs(parent, exist_ok=True)\n"
        '            with open(target, "wb") as fh:\n                fh.write(data)\n'
        "            ok += 1\n            total += len(data)\n            done.append(name)\n"
        '            if ok %% 200 == 0:\n'
        '                log("  %%d files / %%.1f MiB" %% (ok, total / 1048576.0))\n'
        "        except Exception as exc:\n"
        "            failed.append([name, repr(exc)[:120]])\n"
        '    log("done: %%d files, %%.1f MiB, %%d failure(s)" %% (ok, total / 1048576.0, len(failed)))\n'
        '    with open(os.path.join(OUT, "_hook_result.json"), "w", encoding="utf-8") as fh:\n'
        '        json.dump({"ok": ok, "bytes": total, "failures": failed[:200]}, fh,\n'
        "                  ensure_ascii=False, indent=1)\n"
        "except Exception:\n"
        '    log("FATAL\\n" + traceback.format_exc())\n'
        "os._exit(0)\n" % (out_dir, log_path)
    )


# --------------------------------------------------------------------------
# the backend entry point
# --------------------------------------------------------------------------

def _find_exe(game_root: Path, exe: Path | None) -> Path | None:
    if exe and Path(exe).is_file():
        return Path(exe)
    named = game_root / (game_root.name + ".exe")
    if named.is_file():
        return named
    candidates = sorted(p for p in game_root.glob("*.exe"))
    if not candidates:
        children = [d for d in game_root.iterdir() if d.is_dir() and any(d.glob("*.exe"))]
        if len(children) == 1:
            return _find_exe(children[0], exe)
    return candidates[0] if candidates else None


def _steam_env(game_root: Path, appid: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("RENPY_SEARCHPATH", "RENPY_LOG_BASE", "PYTHONPATH",
                        "PYTHONDONTWRITEBYTECODE")}
    if appid is None:
        # steamapps/<game>: the manifest names the app id, but a manual install or a
        # cleaned-up library often has none, so the caller can supply it.
        for manifest in game_root.parent.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if '"installdir"' in text and game_root.name.lower() in text.lower():
                for line in text.splitlines():
                    if '"appid"' in line:
                        appid = line.split('"')[3]
                        break
                break
    env.update({"SteamAppId": appid or "0", "SteamGameId": appid or "0",
                "SteamClientLaunch": "1"})
    # SteamPath must be the real Steam root: a launcher that consults it exits early
    # when it points at a path without a Steam client.  The layout is
    # <Steam>/steamapps/common/<game>, so the root is three levels up.
    candidates = list(game_root.parents[:4]) + [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"]
    for candidate in candidates:
        if (candidate / "steam.exe").is_file():
            env["SteamPath"] = str(candidate)
            break
    return env


def extract(game_root: Path, out_dir: Path, *, exe: Path | None = None,
            steam_appid: str | None = None, settle: float = 2.5,
            timeout: float = 900.0, verbose: bool = False) -> HookResult:
    """Run the game and pull every archive member out of its interpreter."""
    game_root = Path(game_root).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir.parent / (out_dir.name + ".hook.log")
    log_path.unlink(missing_ok=True)
    (out_dir / "_hook_result.json").unlink(missing_ok=True)

    result = HookResult(ok=False)

    def note(message: str) -> None:
        result.log.append(message)
        if verbose:
            print("[hook] " + message, flush=True)

    if not WINDOWS:
        note("not Windows")
        return result
    executable = _find_exe(game_root, exe)
    if not executable:
        note("no exe found in " + str(game_root))
        return result

    api = _Api()
    note("starting " + executable.name)
    proc = subprocess.Popen([str(executable)], cwd=str(executable.parent),
                            env=_steam_env(game_root, steam_appid),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    handle = None
    started = time.time()
    try:
        info = None
        for _ in range(int(30 / 0.002)):
            if handle is None:
                handle = api.k32.OpenProcess(0x1F0FFF, False, proc.pid)
            modules = api.modules(handle)
            python_name = next((n for n in modules if PY_DLL.match(n)), None)
            if python_name:
                info = (modules, python_name)
                break
            if proc.poll() is not None:
                note("game exited before a python dll appeared (%s)" % proc.returncode)
                return result
            time.sleep(0.002)
        if not info:
            note("no libpython3.X.dll in the game process")
            return result
        modules, python_name = info
        base_python, python_path = modules[python_name]
        note("%s found after %.0f ms" % (python_name, (time.time() - started) * 1000))

        machine = _machine(Path(python_path))
        if machine != "x64":
            note("%s is %s, but this backend only carries an x64 stub" % (python_name, machine))
            return result

        python_exports = _exports(Path(python_path))
        if "PyRun_SimpleString" not in python_exports:
            note("%s does not export PyRun_SimpleString" % python_name)
            return result
        run_simple = base_python + python_exports["PyRun_SimpleString"]

        # Which module of the game calls into CPython?  The runtime appears first, the
        # engine module (renpy.pyd) is mapped later, so keep polling instead of deciding
        # on the first snapshot.
        game_low = str(game_root).lower()
        hookable = []
        scan_deadline = time.time() + 15
        while time.time() < scan_deadline and not hookable:
            for name, (base, path) in api.modules(handle).items():
                if not path or not path.lower().startswith(game_low):
                    continue
                try:
                    slots = _iat_slots(Path(path), python_name)
                except (OSError, struct.error):
                    continue
                hot = [h for h in HOT if h in slots and h in python_exports]
                if hot:
                    hookable.append((name, base, path, slots, hot))
            if not hookable:
                if proc.poll() is not None:
                    note("game exited before a hookable module appeared")
                    return result
                time.sleep(0.05)
        if not hookable:
            note("no module in the game folder imports CPython")
            return result
        note("hookable: " + ", ".join("%s(%d)" % (n, len(h)) for n, _, _, _, h in hookable))
        # Let the engine finish wiring itself up: patching an import slot that the
        # bootstrap is still using is legal, but firing before the archive index exists
        # wastes the one-shot hook.
        time.sleep(max(0.0, settle))

        source = _payload(out_dir, log_path).encode() + b"\0"
        written = ctypes.c_size_t()
        remote = api.k32.VirtualAllocEx(handle, None, len(source) + 64, 0x3000, 0x04)
        api.k32.WriteProcessMemory(handle, remote, source, len(source), ctypes.byref(written))

        # a zero run inside a loaded image is executable-able even with ACG on
        data_python, sections, _, _ = _pe(Path(python_path))
        cave = None
        for name, vaddr, vsize, rawptr, rawsize in sections:
            zero = data_python[rawptr:rawptr + rawsize].find(b"\0" * 4096)
            if zero >= 0:
                cave = base_python + vaddr + zero
                break
        if not cave:
            note("no zero run in %s to hold the stub" % python_name)
            return result
        old_protect = ctypes.c_ulong()
        api.k32.VirtualProtectEx(handle, cave, 4096, 0x40, ctypes.byref(old_protect))

        index = 0
        for name, base, path, slots, hot in hookable:
            for symbol in hot[:4]:
                slot = base + slots[symbol]
                current = ctypes.create_string_buffer(8)
                api.k32.ReadProcessMemory(handle, slot, current, 8, ctypes.byref(written))
                pointer, = struct.unpack("<Q", current.raw)
                expected = base_python + python_exports[symbol]
                if pointer != expected:
                    continue
                stub_at = cave + index * 128
                stub = (b"\x51\x52\x41\x50\x41\x51"          # push rcx rdx r8 r9
                        + b"\x48\x83\xEC\x28"                # shadow space / alignment
                        + b"\x48\xB9" + struct.pack("<Q", slot)
                        + b"\x48\xB8" + struct.pack("<Q", expected)
                        + b"\x48\x89\x01"                    # restore the slot (no recursion)
                        + b"\x48\xB9" + struct.pack("<Q", remote)
                        + b"\x48\xB8" + struct.pack("<Q", run_simple)
                        + b"\xFF\xD0"                        # call PyRun_SimpleString
                        + b"\x48\x83\xC4\x28"
                        + b"\x41\x59\x41\x58\x5A\x59"        # pop r9 r8 rdx rcx
                        + b"\x48\xB8" + struct.pack("<Q", expected)
                        + b"\xFF\xE0")                       # tail-jump to the real function
                api.k32.WriteProcessMemory(handle, stub_at, stub, len(stub), ctypes.byref(written))
                api.k32.VirtualProtectEx(handle, slot, 8, 0x04, ctypes.byref(old_protect))
                api.k32.WriteProcessMemory(handle, slot, struct.pack("<Q", stub_at), 8,
                                           ctypes.byref(written))
                index += 1
        note("hooks installed: %d" % index)
        if not index:
            note("no import slot pointed where the loader said it would")
            return result

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(0.5)
            if (out_dir / "_hook_result.json").is_file():
                break
            if proc.poll() is not None:
                break
        summary = out_dir / "_hook_result.json"
        if summary.is_file():
            data = json.loads(summary.read_text(encoding="utf-8"))
            result.ok = True
            result.files = int(data.get("ok") or 0)
            result.total_bytes = int(data.get("bytes") or 0)
            result.failures = [tuple(item) for item in data.get("failures") or []]
            note("extracted %d files, %.1f MiB" % (result.files, result.total_bytes / 1048576))
        else:
            note("the payload never reported back")
        if log_path.is_file():
            result.log.extend(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
        return result
    finally:
        try:
            subprocess.run(["taskkill", "/IM", executable.name, "/F"],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass
        if handle:
            api.k32.CloseHandle(handle)


if __name__ == "__main__":  # pragma: no cover - manual run
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("./extracted/hook")
    appid = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--appid=")), None)
    outcome = extract(root, target, steam_appid=appid, verbose=True)
    print(json.dumps({"ok": outcome.ok, "files": outcome.files,
                      "bytes": outcome.total_bytes,
                      "failures": outcome.failures[:5]}, ensure_ascii=False, indent=1))


def cli(game=None, output=None, appid: str | None = None, quiet: bool = False) -> int:
    """``--hook`` on the command line: extract into the usual output layout."""
    import hashlib

    from .classify import classify

    start = Path(game) if game else Path(".")
    profile = classify(start)
    root = profile.root
    target = Path(output) if output else Path("./extracted") / root.name
    usable, why = available()
    if not usable:
        print("error: --hook is not usable here: " + why, file=sys.stderr)
        return 2
    if not quiet:
        print("backend: in-process hook -- " + why, flush=True)
        print("game   : %s" % root, flush=True)
        print("output : %s" % target, flush=True)
    result = extract(root, target, steam_appid=appid, verbose=not quiet)
    if not result.ok:
        for line in result.log[-6:]:
            print("  " + line, file=sys.stderr)
        print("error: the hook backend did not finish", file=sys.stderr)
        return 2

    # Same manifest shape the file-based backends produce, so downstream tools and
    # comparisons do not need to care which backend ran.
    manifest = {}
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name.startswith("_"):
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        manifest[path.relative_to(target).as_posix()] = [path.stat().st_size, digest.hexdigest()]
    if manifest:
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print("extracted %d files, %.1f MiB" % (len(manifest),
                                            sum(v[0] for v in manifest.values()) / 1048576))
    if result.failures:
        print("  %d member(s) could not be read" % len(result.failures))
    return 0
