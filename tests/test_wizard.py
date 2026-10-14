"""The interactive front end: what it asks, and what it turns the answers into.

The wizard only builds an argument list, so feeding it answers is enough to test it --
no terminal, no game, no extraction.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_support import ScratchCase

from renpy_unpack import core, wizard


class Scripted:
    """Feeds the wizard answers and remembers what it was asked."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class WizardCase(ScratchCase):
    def make_game(self, name="Game", *, loader_source=True, loader_bytecode=False):
        root = self.path(name)
        (root / "renpy").mkdir(parents=True, exist_ok=True)
        (root / "game").mkdir(parents=True, exist_ok=True)
        if loader_source:
            (root / "renpy" / "loader.py").write_text("# loader\n", encoding="utf-8")
        if loader_bytecode:
            (root / "renpy" / "loader.pyc").write_bytes(b"\x00\x00\x00\x00")
        return root

    def run_wizard(self, answers, **kwargs):
        scripted = Scripted(answers)
        output = io.StringIO()
        result = wizard.run(input_fn=scripted, output=output, **kwargs)
        self.scripted = scripted
        self.text = output.getvalue()
        return result


class TestQuestions(WizardCase):
    def test_it_offers_the_static_reader_when_the_loader_source_is_there(self):
        root = self.make_game()
        self.run_wizard([str(root), "", "", ""])
        self.assertIn("1) 静态 AST（默认） *", self.text)

    def test_it_offers_injection_when_only_bytecode_ships(self):
        root = self.make_game(loader_source=False, loader_bytecode=True)
        # Answers: game root, method, who launches, output, go ahead.
        self.run_wizard([str(root), "", "", "", ""])
        self.assertIn("3) --inject *", self.text)
        self.assertIn("--inject", self.run_wizard([str(root), "", "", "", ""]))

    def test_the_defaults_run_the_static_backend(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "", "", ""])

        self.assertEqual(arguments[0], "--game")
        self.assertEqual(arguments[1], str(root))
        self.assertEqual(arguments[2], "-o")
        self.assertEqual(arguments[3], str(Path.cwd() / "extracted" / root.name))
        self.assertNotIn("--runtime", arguments)
        self.assertNotIn("--inject", arguments)
        self.assertNotIn("--env", arguments)
        self.assertNotIn("--env-launch", arguments)

    def test_every_number_maps_to_its_flag(self):
        root = self.make_game()
        for number, flag in (("1", None), ("2", "--runtime"), ("3", "--inject"), ("4", "--env-launch")):
            arguments = self.run_wizard([str(root), number, "", "", ""])
            if flag is None:
                self.assertNotIn("--runtime", arguments)
                self.assertNotIn("--inject", arguments)
                self.assertNotIn("--env", arguments)
            else:
                self.assertIn(flag, arguments)

    def test_a_method_can_be_named_as_well_as_numbered(self):
        root = self.make_game()
        self.assertIn("--runtime", self.run_wizard([str(root), "runtime", "", ""]))

    def test_env_can_be_launched_by_the_tool_or_by_a_launcher(self):
        root = self.make_game()
        quick = self.run_wizard([str(root), "4", "1", "", ""])
        self.assertIn("--env-launch", quick)
        self.assertNotIn("--wait", quick)
        self.assertIn("不改任何系统设置", self.text)

        steam = self.run_wizard([str(root), "4", "2", "", "", ""])
        self.assertIn("--env", steam)
        self.assertNotIn("--env-launch", steam)
        self.assertIn("--wait", steam)
        self.assertIn("已经开着的启动器", self.text)

    def test_inject_asks_who_launches_and_manual_skips_the_launch(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "3", "2", "", ""])
        self.assertIn("--inject", arguments)
        self.assertIn("--inject-no-launch", arguments)
        self.assertIn("--inject-cleanup", self.text)

    def test_inject_defaults_to_starting_the_game_itself(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "3", "", "", ""])
        self.assertIn("--inject", arguments)
        self.assertNotIn("--inject-no-launch", arguments)
        self.assertIn("出窗口之前就退出", self.text)

    def test_the_other_backends_say_who_launches_instead_of_asking(self):
        root = self.make_game()
        self.run_wizard([str(root), "1", "", ""])
        self.assertIn("不启动游戏", self.text)
        self.run_wizard([str(root), "2", "", ""])
        self.assertIn("由工具启动游戏自带的 python", self.text)
        self.run_wizard([str(root), "4", "1", "", ""])
        self.assertIn("工具会直接启动一次游戏", self.text)

    # Answers are consumed in order: game root, method, (who launches, for --inject and
    # --env), output, (wait, for the Steam path only), go ahead.
    def test_env_waits_when_steam_launches_it(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "4", "2", "", "", ""])
        self.assertIn("--env", arguments)
        self.assertNotIn("--env-launch", arguments)
        self.assertIn("--wait", arguments)
        out = Path(arguments[arguments.index("-o") + 1])
        self.assertFalse(out == root or root in out.parents)

    def test_arm_moves_an_output_that_was_put_inside_the_game(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "4", "1", str(root / "out"), ""])
        out = Path(arguments[arguments.index("-o") + 1])
        self.assertNotIn(root, out.parents)
        self.assertIn("不能往那里写", self.text)

    def test_arm_without_waiting(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "4", "2", "", "n", ""])
        self.assertNotIn("--wait", arguments)


class TestValidation(WizardCase):
    def test_a_path_that_is_not_a_game_is_asked_again(self):
        root = self.make_game()
        empty = self.path("not-a-game")
        empty.mkdir()
        arguments = self.run_wizard([str(empty), str(root), "", "", ""])
        self.assertEqual(arguments[1], str(root))
        self.assertIn("里面没有 renpy/、game/、lib/ 或 *.rpa", self.text)

    def test_a_number_that_is_not_a_method_is_asked_again(self):
        root = self.make_game()
        arguments = self.run_wizard([str(root), "9", "2", "", ""])
        self.assertIn("--runtime", arguments)
        self.assertIn("请输入 1、2、3 或 4", self.text)

    def test_an_unusable_method_is_refused_without_restarting(self):
        root = self.make_game()
        with mock.patch.object(wizard.sys, "platform", "linux"):
            arguments = self.run_wizard([str(root), "4", "1", "", ""])
        self.assertNotIn("--env", arguments)
        self.assertIn("只支持 Windows", self.text)
        # The game root is asked exactly once: an unusable method re-asks the method
        # only, instead of starting the whole wizard over.
        self.assertEqual(sum("游戏根目录" in prompt for prompt in self.scripted.prompts), 1)

    def test_cancelling_returns_nothing(self):
        root = self.make_game()
        self.assertIsNone(self.run_wizard([]))
        self.assertIsNone(self.run_wizard([str(root), "1", "", "n"]))

    def test_saying_no_to_the_summary_runs_nothing(self):
        root = self.make_game()
        self.assertIsNone(self.run_wizard([str(root), "1", "", "n"]))
        self.assertIn("已取消", self.text)

    def test_the_summary_shows_the_equivalent_command(self):
        root = self.make_game()
        self.run_wizard([str(root), "2", "", ""])
        self.assertIn("接下来会执行：", self.text)
        self.assertIn("--runtime", self.text)


class TestGameDetection(WizardCase):
    def test_it_walks_upwards_to_find_the_game(self):
        root = self.make_game()
        deep = root / "game" / "deep" / "deeper"
        deep.mkdir(parents=True)
        self.assertEqual(wizard.guess_game_root(deep), root)

    def test_it_finds_a_bare_archive_directory(self):
        bare = self.path("Bare")
        bare.mkdir()
        (bare / "files.rpa").write_bytes(b"RPA-3.0")
        self.assertEqual(wizard.guess_game_root(bare), bare)

    def test_nothing_to_find(self):
        empty = self.path("Empty")
        empty.mkdir()
        self.assertIsNone(wizard.guess_game_root(empty))


class TestWhenItAppears(WizardCase):
    def test_no_arguments_on_a_terminal_asks(self):
        with mock.patch.object(sys.stdin, "isatty", lambda: True), \
                mock.patch.object(sys.stdout, "isatty", lambda: True), \
                mock.patch.object(wizard, "run", return_value=["--unset-env"]) as run:
            with mock.patch("renpy_unpack.env.cli", return_value=0) as env_cli:
                status = core.main([])
        self.assertEqual(status, 0)
        run.assert_called_once()
        env_cli.assert_called_once()

    def test_piped_input_keeps_the_old_behaviour(self):
        with mock.patch.object(sys.stdin, "isatty", lambda: False), \
                mock.patch.object(wizard, "run") as run:
            core.main([])
        run.assert_not_called()

    def test_the_flag_skips_the_questions(self):
        with mock.patch.object(sys.stdin, "isatty", lambda: True), \
                mock.patch.object(sys.stdout, "isatty", lambda: True), \
                mock.patch.object(wizard, "run") as run:
            core.main(["--no-prompt", "--game", str(self.path("Nowhere"))])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
