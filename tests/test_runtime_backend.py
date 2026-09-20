"""Tests for the optional live-runtime backend.

The protocol parser and the discovery logic are pure enough to test without a
Ren'Py install, which matters because the runtime backend's failure modes are
exactly the ones that are expensive to discover on a real game: a payload written
somewhere unwritable, an interpreter for the wrong platform, a batch split that
exceeds the memory cap.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path

from test_support import ScratchCase, sdk_loader

from renpy_unpack import probe
from renpy_unpack.runtime import (
    BANNER,
    DATA,
    END,
    ExternalRuntime,
    ProbeError,
    RuntimeLayout,
    RuntimeUnavailable,
    _platform_names,
    discover_runtime,
    parse_stream,
)


def build_payload(
    *,
    archives: int = 2,
    index: list[tuple[str, str, int]] | None = None,
    files: dict[str, bytes] | None = None,
    missing: list[str] | None = None,
    aescrypt: list[str] | None = None,
    status: str = "ok",
    trace: str = "Traceback: boom",
) -> bytes:
    """Serialise a stream exactly the way probe.py does."""
    index = index if index is not None else [("a.rpa", "one.txt", 3)]
    files = files or {}
    missing = missing or []

    out = bytearray()
    out += (BANNER + "\n").encode()
    out += (status + "\n").encode()
    if status == "error":
        for line in trace.splitlines():
            out += (line + "\n").encode()
        out += b"\n"
        return bytes(out)

    out += (str(archives) + "\n").encode()
    if aescrypt is None:
        out += b"aescrypt\t0\n\n"
    else:
        out += b"aescrypt\t1\n"
        out += (",".join(aescrypt) + "\n").encode()

    out += (str(len(index)) + "\n").encode()
    for archive, name, size in index:
        out += f"{archive}\t{name}\t{size}\n".encode()

    out += (DATA + "\n").encode()
    for name, data in files.items():
        out += f"FILE\t{name}\t{len(data)}\n".encode()
        out += data
    for name in missing:
        out += f"MISSING\t{name}\n".encode()
    out += (END + "\n").encode()
    return bytes(out)


def parse(data: bytes, **kwargs):
    return parse_stream(io.BytesIO(data), **kwargs)


class TestStreamParsing(unittest.TestCase):
    def test_round_trip(self):
        payload = build_payload(
            archives=3,
            index=[("a.rpa", "x/y.txt", 4), ("b.rpa", "z.bin", 2)],
            files={"x/y.txt": b"data", "z.bin": b"\x00\xff"},
            missing=["gone.txt"],
            aescrypt=["decrypt_block", "decrypt_file"],
        )
        result = parse(payload)

        self.assertEqual(result.archives, 3)
        self.assertEqual(
            result.index, [("a.rpa", "x/y.txt", 4), ("b.rpa", "z.bin", 2)]
        )
        self.assertEqual(result.files, {"x/y.txt": b"data", "z.bin": b"\x00\xff"})
        self.assertEqual(result.missing, ["gone.txt"])
        self.assertTrue(result.has_aescrypt)
        self.assertEqual(result.aescrypt_attrs, ["decrypt_block", "decrypt_file"])

    def test_binary_data_with_newlines_survives(self):
        # The header is line-oriented but file bodies are length-prefixed, so a
        # blob that itself contains protocol-looking lines must round-trip.
        blob = bytes(range(256)) * 4 + b"\nEND\nFILE\tfake\t9\n" + b"\r\n\x00"
        payload = build_payload(files={"weird.bin": blob})
        self.assertEqual(parse(payload).files["weird.bin"], blob)

    def test_empty_file_entry(self):
        payload = build_payload(files={"empty.bin": b""})
        self.assertEqual(parse(payload).files, {"empty.bin": b""})

    def test_missing_banner_is_reported(self):
        with self.assertRaises(ProbeError) as ctx:
            parse(b"nonsense\n")
        self.assertIn("banner", str(ctx.exception))

    def test_truncated_body_is_reported(self):
        payload = build_payload(files={"a.bin": b"0123456789"})
        with self.assertRaises(ProbeError) as ctx:
            parse(payload[:-6])
        self.assertIn("truncated", str(ctx.exception))

    def test_error_status_returns_the_traceback(self):
        result = parse(build_payload(status="error", trace="Traceback: boom"))
        self.assertIn("boom", result.traceback)

    def test_want_files_false_skips_blobs(self):
        payload = build_payload(
            index=[("a.rpa", "big.bin", 5)], files={"big.bin": b"hello"}
        )
        result = parse(payload, want_files=False)
        self.assertEqual(result.files, {})
        self.assertEqual(result.index, [("a.rpa", "big.bin", 5)])

    def test_no_aescrypt_reports_empty_attrs(self):
        result = parse(build_payload())
        self.assertFalse(result.has_aescrypt)
        self.assertEqual(result.aescrypt_attrs, [])

    def test_real_probe_constants_match_the_parser(self):
        # Guards the two halves against drifting apart.
        self.assertEqual(probe.BANNER, BANNER)
        self.assertEqual(probe.DATA, DATA)
        self.assertEqual(probe.END, END)


class TestDiscovery(ScratchCase):
    def make_game(self, *, platforms=("windows", "linux"), launcher=True) -> Path:
        root = self.path("Game")
        (root / "renpy").mkdir(parents=True)
        (root / "game").mkdir()
        for platform in platforms:
            directory = root / "lib" / f"py3-{platform}-x86_64"
            directory.mkdir(parents=True)
            for name in ("python.exe", "python"):
                (directory / name).write_bytes(b"")
        if launcher:
            (root / "SomeGame.py").write_text("# launcher\n", encoding="utf-8")
        return root

    def test_prefers_the_current_platform_interpreter(self):
        root = self.make_game()
        layout = discover_runtime(root)
        self.assertIn(_platform_tag(), layout.python.parent.name)
        self.assertEqual(layout.launcher.name, "SomeGame.py")

    def test_non_game_root_is_rejected(self):
        empty = self.path("NotAGame")
        empty.mkdir(parents=True)
        with self.assertRaises(RuntimeUnavailable) as ctx:
            discover_runtime(empty)
        self.assertIn("renpy/", str(ctx.exception))

    def test_missing_interpreter_suggests_the_static_backend(self):
        root = self.make_game(platforms=())
        (root / "lib").mkdir()
        with self.assertRaises(RuntimeUnavailable) as ctx:
            discover_runtime(root)
        message = str(ctx.exception)
        self.assertIn("--runtime-python", message)
        self.assertIn("--runtime", message)

    def test_explicit_python_is_trusted_but_must_exist(self):
        root = self.make_game()
        fake = self.path("elsewhere", "python.exe")
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"")
        self.assertEqual(discover_runtime(root, python=fake).python, fake)

        with self.assertRaises(RuntimeUnavailable):
            discover_runtime(root, python=self.path("nope.exe"))

    def test_launcher_may_be_absent(self):
        root = self.make_game(launcher=False)
        self.assertIsNone(discover_runtime(root).launcher)


def _platform_tag() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


class TestInterpreterSelection(ScratchCase):
    """The probe's results travel over a pipe, so the interpreter must have stdout."""

    def make_game(self) -> Path:
        root = self.path("Game")
        (root / "renpy").mkdir(parents=True)
        (root / "game").mkdir()
        for platform in ("windows", "linux"):
            directory = root / "lib" / f"py3-{platform}-x86_64"
            directory.mkdir(parents=True)
            for name in ("python.exe", "pythonw.exe", "python"):
                (directory / name).write_bytes(b"")
        return root

    @unittest.skipUnless(os.name == "nt", "pythonw.exe is Windows-only")
    def test_python_exe_is_preferred_over_pythonw(self):
        """pythonw.exe is a GUI binary with no stdout handle.

        It also looks like the obvious choice, and picking it produced an empty
        stream -- which only showed up on a game that got far enough to write.
        """
        from renpy_unpack.runtime import _python_candidates, _platform_names

        self.assertEqual(_platform_names()[0], "python.exe")
        chosen = _python_candidates(self.make_game())[0]
        self.assertEqual(chosen.name, "python.exe")

    def test_the_platform_specific_interpreter_wins(self):
        # Ren'Py ships one lib/py3-<platform> per target, so a Windows install also
        # carries a Linux interpreter; choosing it fails with WinError 193.
        layout = discover_runtime(self.make_game())
        self.assertIn(_platform_tag(), layout.python.parent.name)


class TestLauncherSelection(ScratchCase):
    """The launcher is identified by its hooks, not by its filename.

    A real game's folder also holds ``.py`` files left behind by other people's
    unpackers, and taking the first name alphabetically picked ``core.py`` -- which
    has no hooks, so bootstrap then failed with
    ``module '__main__' has no attribute 'path_to_gamedir'``.
    """

    def make_game_with_decoys(self, *, with_interpreter: bool = False) -> Path:
        root = self.path("Game")
        (root / "renpy").mkdir(parents=True)
        (root / "game").mkdir()
        (root / "SomeGame.exe").write_bytes(b"MZ")
        if with_interpreter:
            directory = root / "lib" / f"py3-{_platform_tag()}-x86_64"
            directory.mkdir(parents=True)
            (directory / _platform_names()[0]).write_bytes(b"")
        # Alphabetically first, but not a launcher.
        (root / "core.py").write_text("# leftover tool\n", encoding="utf-8")
        (root / "aaa_unpacker.py").write_text("# also not a launcher\n", encoding="utf-8")
        (root / "SomeGame.py").write_text(
            "def path_to_gamedir(basedir, name):\n"
            "    return basedir\n"
            "\n"
            "def path_to_renpy_base():\n"
            "    return '.'\n",
            encoding="utf-8",
        )
        return root

    def test_hooks_decide_the_launcher(self):
        from renpy_unpack.runtime import _launcher_candidates

        root = self.make_game_with_decoys(with_interpreter=True)
        candidates = _launcher_candidates(root)
        self.assertEqual(candidates[0].name, "SomeGame.py")
        self.assertEqual(discover_runtime(root).launcher.name, "SomeGame.py")

    def test_the_probe_picks_the_same_launcher(self):
        # The two halves must agree, or the probe reads hooks out of a decoy.
        from renpy_unpack.probe import _find_launcher

        root = self.make_game_with_decoys()
        self.assertEqual(_find_launcher(root).name, "SomeGame.py")

    def test_a_launcher_without_hooks_still_falls_back(self):
        from renpy_unpack.runtime import _launcher_candidates

        root = self.path("Odd")
        (root / "renpy").mkdir(parents=True)
        (root / "Odd.py").write_text("# no hooks at all\n", encoding="utf-8")
        self.assertEqual(_launcher_candidates(root)[0].name, "Odd.py")


class TestProbeUsesTheLoadersExtensions(ScratchCase):
    """The probe must not hardcode ``*.rpa`` either.

    The archive extension is the loader's declaration, and the same mistake is
    available on the runtime side: a hardcoded glob found nothing in the game whose
    archives are named ``.dll`` and reported zero entries while the static backend
    found 2971.
    """

    def test_probe_reads_extensions_from_the_handler_registry(self):
        source = Path(probe.__file__).read_text(encoding="utf-8")
        # Both shapes of the registry, because 8.3.x keeps a plain list of handler
        # classes while 8.4+ keeps an object with an ``exts`` map; asking only the
        # latter is what left a 8.3.4 game reporting zero archives.
        self.assertIn('getattr(handlers, "exts", None)', source)
        self.assertIn("handler.get_supported_extensions()", source)
        # No hardcoded glob may survive outside comments.
        code_lines = [
            line
            for line in source.splitlines()
            if "*.rpa" in line and not line.lstrip().startswith("#")
        ]
        for line in code_lines:
            self.assertNotIn("glob(", line, f"hardcoded archive glob: {line}")

    def test_probe_stops_before_the_scripts_are_loaded(self):
        """A game that cannot start is still readable at archive level.

        One release could not boot at all: a previous unpack left
        ``game/extracted/`` beside ``game/rpy/``, so Ren'Py reported every script as
        "defined twice". Stopping at the loader avoids ever reaching that step --
        and wrapping ``renpy.main.main`` is not an option, since Ren'Py pickles it.
        """
        source = Path(probe.__file__).read_text(encoding="utf-8")
        self.assertIn("settrace", source)
        self.assertIn("_stop_at_loader", source)
        # The wrapped-main approach must not come back: it fails on pickle.
        self.assertNotIn("renpy_main.main =", source)

    def test_probe_refuses_to_run_the_game(self):
        """Losing the early stop must fail loudly, not silently play the game.

        This exists because of a real incident: disabling the early stop to check
        whether it was still needed made the tool boot the game repeatedly, since
        the change stayed on disk. Reading someone's archives must never launch
        their game, so the probe now verifies it regained control.

        Exercised against a stub ``renpy`` whose bootstrap simply returns -- that
        is exactly the "the game ran to completion" situation -- so no real game is
        involved and nothing is launched.
        """
        fake = self.path("FakeGame")
        (fake / "renpy").mkdir(parents=True)
        (fake / "game").mkdir()
        (fake / "renpy" / "__init__.py").write_text(
            "def import_all():\n    pass\n", encoding="utf-8"
        )
        (fake / "renpy" / "bootstrap.py").write_text(
            "def bootstrap(base):\n    return None\n", encoding="utf-8"
        )
        (fake / "FakeGame.py").write_text(
            "def path_to_gamedir(basedir, name):\n    return basedir\n"
            "def path_to_renpy_base():\n    return '.'\n"
            "def path_to_logdir(basedir):\n    return basedir\n",
            encoding="utf-8",
        )

        import subprocess

        completed = subprocess.run(
            [sys.executable, "-c", Path(probe.__file__).read_text(encoding="utf-8"), str(fake)],
            capture_output=True,
            timeout=120,
        )
        output = (completed.stdout or b"").decode("utf-8", "replace")

        self.assertIn("error", output)
        self.assertIn("could not take control", output)
        self.assertIn("running the game rather than reading its archives", output)


class TestBatching(unittest.TestCase):
    def make_runtime(self, batch_bytes: int) -> ExternalRuntime:
        layout = RuntimeLayout(
            python=Path("python"),
            game_root=Path("game"),
            renpy_dir=Path("game/renpy"),
            launcher=None,
        )
        return ExternalRuntime(layout, batch_bytes=batch_bytes)

    def test_single_batch_when_everything_fits(self):
        runtime = self.make_runtime(1000)
        sizes = {"a": 100, "b": 100}
        self.assertEqual(runtime.batches(sizes), [["a", "b"]])

    def test_split_when_the_cap_is_exceeded(self):
        runtime = self.make_runtime(250)
        sizes = {"a": 100, "b": 100, "c": 100}
        self.assertEqual(runtime.batches(sizes), [["a", "b"], ["c"]])

    def test_oversized_entry_gets_its_own_batch(self):
        # A single member bigger than the cap still has to be readable.
        runtime = self.make_runtime(50)
        sizes = {"a": 10, "huge": 5000, "b": 10}
        self.assertEqual(runtime.batches(sizes), [["a"], ["huge"], ["b"]])

    def test_empty_input_yields_no_batches(self):
        self.assertEqual(self.make_runtime(10).batches({}), [])

    def test_scratch_root_is_honoured(self):
        runtime = self.make_runtime(10)
        runtime.close()


class TestNoScratchFiles(ScratchCase):
    """The backend must not need any writable location.

    Results stream over a pipe precisely so the backend works on a read-only
    install: Steam libraries reject writes, and an earlier payload-file design both
    failed there and left its scratch directory inside the output tree.
    """

    def test_external_runtime_exposes_no_scratch_directory(self):
        layout = RuntimeLayout(
            python=Path("python"),
            game_root=Path("game"),
            renpy_dir=Path("game/renpy"),
            launcher=None,
        )
        runtime = ExternalRuntime(layout)
        self.assertFalse(hasattr(runtime, "scratch_dir"))
        self.assertFalse(hasattr(runtime, "_scratch"))

    def test_unpacker_does_not_invent_a_scratch_path(self):
        from renpy_unpack.core import Unpacker

        unpacker = Unpacker(self.path("Game"), progress=False)
        self.assertFalse(hasattr(unpacker, "_runtime_scratch"))

    def test_child_environment_forbids_bytecode_writing(self):
        """Reading the archives must not *write* anything into the install.

        This is not a theoretical gap.  Importing the game's own ``renpy`` package
        compiles it, and CPython caches the bytecode in ``renpy/__pycache__/`` inside
        the installation -- one run against a real Steam install left 52 ``.pyc``
        files there, while the backend advertised that it writes nothing.
        """
        from renpy_unpack.runtime import child_environment

        env = child_environment()
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["RENPY_DISABLE_LOG"], "1")

    def test_probe_turns_bytecode_writing_off_itself(self):
        # Belt and braces: the probe disables it as its first statement, so the
        # backend stays write-free even if the probe is started by hand without
        # the environment variable.
        import ast

        source = Path(probe.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        self.assertEqual(
            ast.unparse(main.body[0]),
            "sys.dont_write_bytecode = True",
            "the probe must disable bytecode writing before importing renpy",
        )


@unittest.skipUnless(sdk_loader(), "no Ren'Py loader.py available")
class TestProbeSourceIsIndependent(unittest.TestCase):
    """The probe must not import the package it lives in.

    It runs inside the game's interpreter, whose sys.path we do not control, so a
    package import there would be fragile.  Checked against the parsed module, not
    the raw text: the docstring mentions the phrase on purpose.
    """

    @staticmethod
    def imported_modules() -> set[str]:
        import ast

        source = Path(probe.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    modules.add(node.module)
        return modules

    def test_probe_does_not_import_renpy_unpack(self):
        modules = self.imported_modules()
        offenders = [name for name in modules if name.split(".")[0] == "renpy_unpack"]
        self.assertEqual(offenders, [])

    def test_probe_only_imports_stdlib_and_renpy(self):
        modules = self.imported_modules()
        allowed = {"os", "sys", "traceback", "types", "pathlib", "__future__"}
        unexpected = {
            name
            for name in modules
            if name.split(".")[0] not in allowed and name.split(".")[0] != "renpy"
        }
        self.assertEqual(unexpected, set(), "the probe should stay self-contained")

    def test_probe_writes_no_files_at_all(self):
        """The probe must be write-free.

        Results stream over a pipe, not to a scratch file, so the backend needs no
        writable location anywhere -- the reason ``--list`` works on a read-only
        Steam install.  A stray write would not merely be untidy: it would break
        the whole backend there.
        """
        import ast

        source = Path(probe.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        writes: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            mode = None
            if isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = node.args[1] if len(node.args) >= 2 else None
            elif isinstance(node.func, ast.Attribute) and node.func.attr in (
                "open",
                "write_text",
                "write_bytes",
                "mkdir",
                "unlink",
                "remove",
                "rmtree",
                "copy",
                "copy2",
            ):
                mode = node.args[0] if node.args else None
                if node.func.attr != "open":
                    writes.append(ast.unparse(node))
                    continue

            if isinstance(mode, ast.Constant) and any(
                flag in str(mode.value) for flag in ("w", "a", "+", "x")
            ):
                writes.append(ast.unparse(node))

        self.assertEqual(writes, [], f"the probe must not write: {writes}")

    def test_probe_opens_files_read_only(self):
        # The one intentional file access is reading the launcher .py.
        import ast

        source = Path(probe.__file__).read_text(encoding="utf-8")
        self.assertIn("launcher.read_bytes()", source)
        tree = ast.parse(source)
        modes = [
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "open"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        for mode in modes:
            self.assertNotIn("w", str(mode), f"unexpected write mode {mode!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
