"""The environment-variable backend: installing, unsetting, and the extension itself.

Nothing here touches the real machine.  ``env.py`` reaches the registry only through
module-level functions, which these tests replace with an in-memory store; the extension's
own restore imports ``winreg``, and gets the same fake through ``sys.modules``.
That is deliberate: the extension restoring the user's environment is exactly the behaviour
under test, so it has to be observable.
"""

from __future__ import annotations

import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from test_support import ScratchCase

from renpy_unpack import env as env_backend


class FakeRegistry:
    """An in-memory ``HKCU\\Environment``, doubling as a stand-in ``winreg``."""

    REG_SZ = 1
    HKEY_CURRENT_USER = 0
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 1
    Environment = "Environment"

    def __init__(self, initial=None):
        self.values = dict(initial or {})
        self.broadcasts = 0

    # -- the seam env.py uses --
    def read(self, name):
        return self.values.get(name)

    def write(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)

    def broadcast(self):
        self.broadcasts += 1

    # -- the winreg module the extension imports --
    def OpenKey(self, hive, path, reserved=0, access=0):
        return _FakeKey(self, access)

    def CreateKey(self, hive, path):
        return _FakeKey(self, self.KEY_QUERY_VALUE | self.KEY_SET_VALUE)

    def QueryValueEx(self, key, name):
        # A real key opened without KEY_QUERY_VALUE refuses to be read, and that is not
        # theoretical: the extension shipped that way, the refusal was swallowed, and the
        # environment stayed installed while the run reported success.
        if not getattr(key, "access", 0) & self.KEY_QUERY_VALUE:
            raise PermissionError(5, "Access is denied")
        if name not in self.values:
            raise OSError(2, "The system cannot find the file specified")
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        self.values.pop(name, None)


class _FakeKey:
    def __init__(self, registry, access=0):
        self.registry = registry
        self.access = access

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Handlers:
    """Stands in for Ren'Py 8.4+ ``ArchiveHandlers``, which carries ``.exts``."""

    def __init__(self, exts):
        self.exts = exts


class EnvCase(ScratchCase):
    def setUp(self):
        super().setUp()
        self.registry = FakeRegistry()
        patcher = mock.patch.multiple(
            env_backend,
            state_dir=lambda: self.path("state"),
            read_user_variable=self.registry.read,
            write_user_variable=self.registry.write,
            delete_user_variable=self.registry.delete,
            broadcast_change=self.registry.broadcast,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.game = self.path("Game")
        self.game.mkdir(parents=True, exist_ok=True)
        self.output = self.path("out")
        self.output.mkdir(parents=True, exist_ok=True)

    # -- helpers -----------------------------------------------------------

    def extension_source(self) -> str:
        return Path(env_backend.__file__).with_name("_extension_body.py").read_text(encoding="utf-8")

    def run_extension(self, *, archives=None, game_files=None, common_files=None,
                 target=None, spent=False, output=None, archives_for_load=None,
                 pin=True, basedir=None):
        """Exec the shipped extension with a stub Ren'Py; return the exit codes it asked for.

        ``archives`` maps member name -> bytes and is what ``load_from_archive`` serves;        """
        searchpath = env_backend.searchpath_dir()
        searchpath.mkdir(parents=True, exist_ok=True)
        out = Path(output) if output else self.path("extension-out")
        out.mkdir(parents=True, exist_ok=True)
        (searchpath / env_backend.EXTENSION_NAME).write_text(self.extension_source(), encoding="utf-8")
        # Tests that exercise the extension's restore need to say what installing had set,
        # and what was there before it; everything else gets the ordinary case.
        installed = getattr(self, "config_installed", {"RENPY_SEARCHPATH": str(searchpath)})
        previous = getattr(self, "config_previous", {})
        (searchpath / "config.json").write_text(json.dumps({
            "output": str(out),
            # ``pin=False`` models an install that named no game: the extension then
            # takes whatever launches, which is the point of --env.
            "game": None if not pin else str(target if target is not None else self.game),
            "manifest": str(env_backend.manifest_path()),
            "installed": installed,
            "previous": previous,
        }), encoding="utf-8")
        if spent:
            (searchpath / "spent").write_text("1\n", encoding="utf-8")

        archived = dict(archives or {})
        archive_file = self.game / "game" / "father.blend"

        loader = types.ModuleType("renpy.loader")
        loader.archives = [(str(archive_file), {n: (0, len(d), 0) for n, d in archived.items()})]
        loader.arc_files = [("father", ".blend", str(archive_file))]
        loader.archive_handlers = _Handlers({".blend": []})
        # The engine stores ``(directory, name)`` pairs; the tests pass them as the keys
        # of a dict so a call site reads as one line per file.
        loader.common_files = [tuple(key) for key in (common_files or {})]
        loader.game_files = [tuple(key) for key in (game_files or {})]
        loader.load = lambda name: None
        loader.load_from_archive = lambda name: io.BytesIO(archived[name])

        def index_archives():
            return None

        loader.index_archives = index_archives
        loader.scandirfiles_from_archives = lambda add, seen: None
        loader.scandirfiles_callbacks = []

        renpy = types.ModuleType("renpy")
        renpy.version = "8.5.2"
        renpy.loader = loader
        home = Path(basedir) if basedir is not None else self.game
        renpy.config = types.SimpleNamespace(
            gamedir=str(home / "game"),
            basedir=str(home),
            archives=["father"],
            commondir=str(self.path("engine", "renpy", "common")),
        )

        exits: list = []
        previous_flag = sys.dont_write_bytecode
        with mock.patch.dict(sys.modules, {"renpy": renpy, "renpy.loader": loader,
                                           "winreg": self.registry}):
            namespace = {"__file__": str(searchpath / env_backend.EXTENSION_NAME),
                         "RENPY_UNPACK_LEAVE": exits.append}
            exec(compile(self.extension_source(), "extension-body", "exec"), namespace)
            loader.index_archives()
        sys.dont_write_bytecode = previous_flag
        # The configured path is a base: the extension adds a folder named after the
        # game, so the interesting directory is one level down.
        self.base_output = out
        return exits, out / home.name

    def manifest(self):
        return env_backend.read_manifest()

    def log(self) -> str:
        path = env_backend.log_path()
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


class TestInstalling(EnvCase):
    def test_installing_without_a_named_game_accepts_any(self):
        result = env_backend.install(None, self.output)
        config = json.loads(env_backend.config_path().read_text(encoding="utf-8"))
        self.assertIsNone(config["game"])
        self.assertIsNone(result.game_root)
        self.assertIn("the game you want", result.describe())

    def test_installing_without_a_named_game_keeps_the_output_as_a_base(self):
        env_backend.install(None, self.output)
        config = json.loads(env_backend.config_path().read_text(encoding="utf-8"))
        self.assertEqual(Path(config["output"]), self.output)

    def test_the_extension_is_written_outside_the_game(self):
        result = env_backend.install(self.game, self.output)
        searchpath = env_backend.extension_path()
        self.assertTrue(searchpath.is_file())
        self.assertNotIn(str(self.game), str(searchpath))
        self.assertEqual(searchpath.read_text(encoding="utf-8"), self.extension_source())
        self.assertEqual(result.output, self.output)

    def test_the_written_extension_has_no_byte_order_mark(self):
        # Measured pitfall from the injection backend: a BOM makes exec() raise
        # "invalid non-printable character U+FEFF" and the extension silently never runs.
        env_backend.install(self.game, self.output)
        raw = env_backend.extension_path().read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.decode("utf-8").encode("utf-8"), raw)

    def test_the_config_carries_absolute_paths_and_the_armed_values(self):
        env_backend.install(self.game, Path("relative-out"))
        config = json.loads(env_backend.config_path().read_text(encoding="utf-8"))
        self.assertTrue(Path(config["output"]).is_absolute())
        self.assertTrue(Path(config["manifest"]).is_absolute())
        self.assertEqual(config["game"], str(self.game))
        self.assertEqual(config["installed"]["RENPY_SEARCHPATH"], str(env_backend.searchpath_dir()))
        self.assertEqual(config["installed"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_arming_sets_the_user_variables(self):
        env_backend.install(self.game, self.output)
        self.assertEqual(self.registry.values["RENPY_SEARCHPATH"], str(env_backend.searchpath_dir()))
        self.assertEqual(self.registry.values["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(self.registry.broadcasts, 1)

    def test_arming_remembers_what_was_there_before(self):
        self.registry.values["RENPY_LOG_BASE"] = "mine"
        result = env_backend.install(self.game, self.output)
        self.assertEqual(result.previous["RENPY_LOG_BASE"], "mine")
        self.assertEqual(self.registry.values["RENPY_LOG_BASE"], str(env_backend.state_dir() / "logs"))

    def test_arming_again_clears_the_previous_run(self):
        env_backend.install(self.game, self.output)
        env_backend.manifest_path().write_text("{}\n", encoding="utf-8")
        env_backend.log_path().write_text("old\n", encoding="utf-8")
        (env_backend.searchpath_dir() / "spent").write_text("1\n", encoding="utf-8")

        env_backend.install(self.game, self.output)

        self.assertFalse(env_backend.manifest_path().exists())
        self.assertFalse(env_backend.log_path().exists())
        self.assertFalse((env_backend.searchpath_dir() / "spent").exists())


class TestUnsetting(EnvCase):
    def test_unset_env_removes_what_env_created(self):
        env_backend.install(self.game, self.output)
        actions = env_backend.uninstall()
        self.assertNotIn("RENPY_SEARCHPATH", self.registry.values)
        self.assertNotIn("PYTHONDONTWRITEBYTECODE", self.registry.values)
        self.assertTrue(any("removed" in action for action in actions))

    def test_unset_env_restores_a_value_that_was_there_before(self):
        self.registry.values["RENPY_LOG_BASE"] = "mine"
        env_backend.install(self.game, self.output)
        env_backend.uninstall()
        self.assertEqual(self.registry.values["RENPY_LOG_BASE"], "mine")

    def test_unset_env_leaves_alone_a_value_that_changed_since_installing(self):
        env_backend.install(self.game, self.output)
        self.registry.values["RENPY_SEARCHPATH"] = "someone else's"
        actions = env_backend.uninstall()
        self.assertEqual(self.registry.values["RENPY_SEARCHPATH"], "someone else's")
        self.assertTrue(any("changed since installing" in action for action in actions))

    def test_unset_env_takes_the_extension_away(self):
        env_backend.install(self.game, self.output)
        self.assertTrue(env_backend.extension_path().is_file())
        env_backend.uninstall()
        self.assertFalse(env_backend.extension_path().exists())


class TestCommandLine(EnvCase):
    def cli(self, **kwargs):
        """Call the console entry point with both streams captured."""
        self.stderr = io.StringIO()
        return env_backend.cli(errors=self.stderr, **kwargs)

    def test_a_refused_output_inside_the_game(self):
        stream = io.StringIO()
        status = self.cli(do_env=True, game=self.game,
                          output=self.game / "extracted", stream=stream)
        self.assertEqual(status, 2)
        self.assertEqual(self.registry.values, {})
        self.assertIn("must extract somewhere outside the game", self.stderr.getvalue())

    def test_the_default_output_is_outside_the_game(self):
        status = self.cli(do_env=True, game=self.game, stream=io.StringIO())
        self.assertEqual(status, 0)
        config = json.loads(env_backend.config_path().read_text(encoding="utf-8"))
        self.assertFalse(Path(config["output"]).is_relative_to(self.game))

    def test_env_and_unset_env_together_are_refused(self):
        self.assertEqual(self.cli(do_env=True, do_unset_env=True, game=self.game), 2)
        self.assertIn("pick one", self.stderr.getvalue())

    def test_an_unset_without_a_previous_install_says_so(self):
        stream = io.StringIO()
        status = self.cli(do_unset_env=True, stream=stream)
        self.assertEqual(status, 0)
        self.assertIn("environment restored", stream.getvalue())

    def test_the_console_accepts_no_game(self):
        status = self.cli(do_env=True, output=self.path("out3"), stream=io.StringIO())
        self.assertEqual(status, 0)
        config = json.loads(env_backend.config_path().read_text(encoding="utf-8"))
        self.assertIsNone(config["game"])

    def test_a_wait_that_never_sees_an_extraction_reports_why(self):
        stream = io.StringIO()
        status = self.cli(do_env=True, game=self.game, output=self.path("out2"), wait=0.1,
                          stream=stream)
        self.assertEqual(status, 1)
        self.assertIn("restart it -- or simply start the", self.stderr.getvalue())


class TestTheExtensionItself(EnvCase):
    def test_it_extracts_every_archive_member_and_leaves(self):
        exits, out = self.run_extension(archives={"images/pic.png": b"PNG", "script.rpyc": b"RENPY"})
        self.assertEqual(exits, [0])
        self.assertEqual((out / "images" / "pic.png").read_bytes(), b"PNG")
        self.assertEqual((out / "script.rpyc").read_bytes(), b"RENPY")
        summary = self.manifest()
        self.assertTrue(summary["done"])
        self.assertEqual(summary["files"], 2)
        self.assertEqual(summary["bytes"], 8)

    def test_it_declines_another_game(self):
        exits, out = self.run_extension(archives={"a.txt": b"a"}, target=self.path("OtherGame"))
        self.assertEqual(exits, [])
        self.assertEqual(list(out.rglob("*")), [])
        self.assertIn("declined", self.log())

    def test_it_declines_when_already_spent(self):
        exits, out = self.run_extension(archives={"a.txt": b"a"}, spent=True)
        self.assertEqual(exits, [])
        self.assertEqual(list(out.rglob("*")), [])
        self.assertIn("already spent", self.log())

    def test_files_land_in_a_folder_named_after_the_game(self):
        exits, out = self.run_extension(archives={"a.txt": b"a"})
        self.assertEqual(exits, [0])
        self.assertEqual(out, self.base_output / self.game.name)
        self.assertEqual((out / "a.txt").read_bytes(), b"a")

    def test_it_accepts_any_game_when_none_was_named(self):
        other = self.path("AnotherGame")
        (other / "game").mkdir(parents=True)

        exits, out = self.run_extension(archives={"a.txt": b"a"}, pin=False,
                                        basedir=other)

        self.assertEqual(exits, [0])
        self.assertEqual(out, self.base_output / "AnotherGame")
        self.assertEqual((out / "a.txt").read_bytes(), b"a")

    def test_it_puts_the_environment_back_when_it_is_done(self):
        self.registry.values["RENPY_SEARCHPATH"] = str(env_backend.searchpath_dir())

        exits, _out = self.run_extension(archives={"a.txt": b"a"})

        self.assertEqual(exits, [0])
        self.assertNotIn("RENPY_SEARCHPATH", self.registry.values)
        self.assertIn("restore: RENPY_SEARCHPATH removed", self.log())

    def test_it_restores_a_value_that_existed_before_arming(self):
        # Installing sets RENPY_LOG_BASE; the user already had one of their own.
        self.config_installed = {"RENPY_LOG_BASE": "ours"}
        self.config_previous = {"RENPY_LOG_BASE": "mine"}
        self.registry.values["RENPY_LOG_BASE"] = "ours"

        self.run_extension(archives={"a.txt": b"a"})

        self.assertEqual(self.registry.values["RENPY_LOG_BASE"], "mine")
        self.assertIn("restored to its earlier value", self.log())

    def test_it_keeps_the_arming_when_something_failed(self):
        # A half-finished extraction must stay installed, so the run can simply be repeated.
        searchpath = env_backend.searchpath_dir()
        searchpath.mkdir(parents=True, exist_ok=True)
        (searchpath / env_backend.EXTENSION_NAME).write_text(self.extension_source(), encoding="utf-8")
        (searchpath / "config.json").write_text(json.dumps({
            "output": str(self.path("extension-out")),
            "game": str(self.game),
            "manifest": str(env_backend.manifest_path()),
            "installed": {"RENPY_SEARCHPATH": "installed-value"},
            "previous": {},
        }), encoding="utf-8")
        self.registry.values["RENPY_SEARCHPATH"] = "installed-value"

        loader = types.ModuleType("renpy.loader")
        loader.archives = [("fake", {"broken": (0, 3, 0)})]
        loader.arc_files = []
        loader.archive_handlers = _Handlers({".blend": []})
        loader.common_files, loader.game_files = [], []
        loader.load = lambda name: None

        def boom(name):
            raise OSError("disk went away")

        loader.load_from_archive = boom
        loader.index_archives = lambda: None
        loader.scandirfiles_from_archives = lambda add, seen: None
        loader.scandirfiles_callbacks = []

        renpy = types.ModuleType("renpy")
        renpy.version = "8.5.2"
        renpy.loader = loader
        renpy.config = types.SimpleNamespace(gamedir=str(self.game / "game"),
                                            basedir=str(self.game), archives=[],
                                            commondir=str(self.path("engine")))

        exits: list = []
        previous_flag = sys.dont_write_bytecode
        with mock.patch.dict(sys.modules, {"renpy": renpy, "renpy.loader": loader,
                                           "winreg": self.registry}):
            namespace = {"__file__": str(searchpath / env_backend.EXTENSION_NAME),
                         "RENPY_UNPACK_LEAVE": exits.append}
            exec(compile(self.extension_source(), "extension-body", "exec"), namespace)
            loader.index_archives()
        sys.dont_write_bytecode = previous_flag

        self.assertEqual(exits, [0])
        self.assertEqual(self.registry.values["RENPY_SEARCHPATH"], "installed-value")
        self.assertFalse((searchpath / "spent").exists())
        self.assertIn("leaving the environment alone", self.log())


if __name__ == "__main__":
    unittest.main()
