"""交互式前端：不带任何参数、在终端里运行时出现。

它只负责**提问**：每个答案都会变成普通的命令行参数，交给同一条代码路径执行。
界面用中文；工具本身的日志与报错仍是英文（与其它输出一致）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

METHODS = (
    ("static", "1", "静态 AST（默认）",
     "读取游戏自己的 loader.py 并在本进程里运行那段解析代码——最快，不启动游戏，不写任何文件"),
    ("runtime", "2", "--runtime",
     "用游戏自带的 python 起一次运行时，从外面调它自己的 loader——同样不需要写权限"),
    ("inject", "3", "--inject",
     "往 game/ 放一个小脚本让游戏自己解包，跑完自动删掉——能力最强，但它**会写 game/**，"
     "只适合没有文件完整性校验的环境"),
    ("env", "4", "--env",
     "在用户级 RENPY_SEARCHPATH 上挂一个 Ren'Py 扩展，让游戏自己解包并退出——可以由工具"
     "直接启动一次游戏，也可以等你从 Steam 启动；游戏目录一个字节都不写，所以"
     "**完全绕过文件完整性/篡改检查**（--inject 会写 game/，只有在没有校验的环境才安全）"),
)

BY_NUMBER = {number: key for key, number, _label, _help in METHODS}
BY_KEY = {key: (number, label, help_text) for key, number, label, help_text in METHODS}


class Cancelled(Exception):
    """用户按了 Ctrl-C，或关掉了输入。"""


def _write(output, text=""):
    output.write(text + "\n")
    output.flush()


def _ask(prompt, *, default=None, show_default=True, input_fn=input, output=sys.stdout):
    """问一行。用户放弃时返回 ``None``。"""
    while True:
        suffix = " [%s]" % default if (default is not None and show_default) else ""
        try:
            answer = input_fn("%s%s: " % (prompt, suffix)).strip()
        except (EOFError, KeyboardInterrupt):
            _write(output)
            return None
        if not answer and default is not None:
            return default
        if answer:
            return answer
        _write(output, "  请直接回车用默认值，或输入一个值")


def _ask_yes_no(prompt, *, default=False, input_fn=input, output=sys.stdout):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = _ask("%s [%s]" % (prompt, hint), default="y" if default else "n",
                      show_default=False, input_fn=input_fn, output=output)
        if answer is None:
            return None
        if answer[:1].lower() in ("y", "n"):
            return answer[:1].lower() == "y"
        _write(output, "  请回答 y 或 n")


def _ask_choice(prompt, options, *, default, input_fn=input, output=sys.stdout):
    """问一个编号选择题；``options`` 是 ``(编号, 标题, 说明)``。"""
    _write(output)
    _write(output, prompt)
    for number, title, detail in options:
        _write(output, "  %s) %s" % (number, title))
        _write(output, "      %s" % detail)
    numbers = [number for number, _title, _detail in options]
    while True:
        answer = _ask("  选择", default=default, input_fn=input_fn, output=output)
        if answer is None:
            return None
        if answer in numbers:
            return answer
        _write(output, "  请输入 %s" % "、".join(numbers))


def guess_game_root(start=None):
    """给一个默认的游戏目录：当前目录，或向上最近的游戏目录。"""
    current = Path(start or os.getcwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "renpy").is_dir() and (candidate / "game").is_dir():
            return candidate
    for candidate in [current, *current.parents]:
        if any(candidate.glob("*.rpa")):
            return candidate
    return None


def _why_not_a_game(path: Path):
    """看起来可用就返回 ``None``，否则给一句简短原因。"""
    if not path.is_dir():
        return "不是一个目录"
    if any((path / marker).exists() for marker in ("renpy", "game", "lib")):
        return None
    if any(path.glob("*.rpa")) or any(path.glob("*.exe")):
        return None
    return "里面没有 renpy/、game/、lib/ 或 *.rpa"


def _recommend(root: Path) -> str:
    """按游戏实际带了什么，决定先推荐哪种方式。"""
    if (root / "renpy" / "loader.py").is_file():
        return "static"
    if (root / "game" / "renpy" / "loader.py").is_file():
        return "static"
    if any(root.glob("**/loader.pyc")):
        # 没有源码可提取，只能让游戏自己去跑 loader。
        return "inject" if os.access(root, os.W_OK) else "runtime"
    return "static"


_NEXT_STEPS = {
    "static": [
        "这种方式不启动游戏：它只是把 loader.py 里的解析代码拿出来在本进程里跑一遍。",
    ],
    "runtime": [
        "这种方式由工具启动游戏自带的 python——它在 loader 导入处就停下，不会走到出窗口那一步，",
        "所以没有「手动启动」这一选项。",
    ],
    "inject-auto": [
        "工具会自己启动一次游戏：它在出窗口之前就退出，所以不会看到游戏画面。",
    ],
    "inject-manual": [
        "脚本已经放好，工具**不会**替你启动游戏：",
        "  1. 你自己启动游戏（点 Steam、快捷方式，怎么方便怎么来）。它会解包并在出窗口前退出。",
        "  2. 解出来的文件在输出目录里。",
        "  3. 脚本解包成功后**会把自己删掉**，不会给下次启动留东西。万一是它没能删掉"
        "（或游戏根本没跑起来），运行 `--inject-cleanup` 它会汇报解出了什么并手动删掉。",
    ],
    "env-launch": [
        "工具会直接启动一次游戏：它在出窗口之前就解包并自行退出。",
        "整个过程不写游戏目录、也不改任何系统设置——三个环境变量只传给这一个子进程。",
    ],
    "env-steam": [
        "这种方式只能由你启动，工具不会碰游戏进程：",
        "  它不写游戏目录，所以启动器的文件校验看不到任何改动。",
        "  1. 重启 Steam（它把启动那一刻的环境传给游戏；趁它没运行时设置最省事）。",
        "  2. 从 Steam 正常启动游戏——它用自己的 loader 解包，并在出窗口前自行退出。",
        "  3. 完事运行 `--unset-env` 收尾（正常情况下扩展解密成功后已经自己撤了）。",
    ],
}


def run(*, input_fn=input, output=sys.stdout):
    """问完所有问题，返回一个参数列表；中途取消返回 ``None``。"""
    _write(output, "")
    _write(output, "=" * 66)
    _write(output, "  Ren'Py 解包器 —— 交互式设置")
    _write(output, "=" * 66)
    _write(output, "  （任何时候 Ctrl-C 都可以取消）")
    _write(output)

    # -- 1. 游戏在哪 --------------------------------------------------------
    detected = guess_game_root()
    _write(output, "游戏在哪里？就是那个包含 renpy/ 和 game/ 的目录。")
    if detected:
        _write(output, "  自动找到：%s" % detected)
    answer = _ask("  游戏根目录", default=str(detected) if detected else None,
                  input_fn=input_fn, output=output)
    if answer is None:
        return None
    root = Path(os.path.expanduser(answer)).resolve()

    reason = _why_not_a_game(root)
    while reason is not None:
        _write(output, "  %s：%s" % (reason, root))
        answer = _ask("  游戏根目录", default=None, input_fn=input_fn, output=output)
        if answer is None:
            return None
        root = Path(os.path.expanduser(answer)).resolve()
        reason = _why_not_a_game(root)

    # -- 2. 用哪种方式 ------------------------------------------------------
    recommended = _recommend(root)
    _write(output)
    _write(output, "用哪种方式读存档？")
    for key, number, label, help_text in METHODS:
        mark = " *" if key == recommended else ""
        _write(output, "  %s) %s%s" % (number, label, mark))
        _write(output, "      %s" % help_text)
    _write(output, "  （* 是按这个游戏的实际情况推荐的）")

    # 某种方式在这台机器/这个游戏上可能行不通：说明原因再问一次，而不是从头再来。
    while True:
        choice = None
        while choice is None:
            answer = _ask("  方式", default=BY_KEY[recommended][0],
                          input_fn=input_fn, output=output)
            if answer is None:
                return None
            if answer in BY_NUMBER:
                choice = BY_NUMBER[answer]
            else:
                for key, _number, label, _help in METHODS:
                    if answer.lower() in (key, label.split()[0].lower()):
                        choice = key
                        break
            if choice is None:
                _write(output, "  请输入 1、2、3 或 4")

        if choice == "env" and sys.platform != "win32":
            _write(output, "  --env 只支持 Windows：它依赖 Steam 传给游戏的用户级环境变量")
            continue
        if choice == "inject" and not os.access(root, os.W_OK):
            _write(output, "  game/ 现在不可写，而 --inject 需要往里面放脚本")
            continue
        break

    # -- 3. 谁启动游戏（只有 --inject 是个真选择）--------------------------
    launch = "auto"
    if choice == "inject":
        picked = _ask_choice(
            "游戏由谁启动？",
            [("1", "由工具启动一次（默认）", "跑完立刻汇报结果，最省事"),
             ("2", "我手动启动", "工具只把脚本放好，你自己启动（比如从 Steam）；"
                                "脚本解包完会**自己删掉**，万一没删掉才需要 --inject-cleanup")],
            default="1", input_fn=input_fn, output=output)
        if picked is None:
            return None
        launch = "auto" if picked == "1" else "manual"

    elif choice == "env":
        # 机制是「环境变量 + 扩展」，谁把游戏启动起来并不重要：工具直接启动 exe 完全等价，
        # 而且连注册表都不用改——三个变量只塞进那一个子进程。
        picked = _ask_choice(
            "游戏由谁启动？",
            [("1", "由工具启动一次（推荐）",
              "工具直接启动游戏，跑完立刻汇报；不改任何系统设置，也不用重启 Steam"),
             ("2", "我手动启动（从 Steam）",
              "设好用户级环境变量，你重启 Steam 后再启动游戏；适合只能由 Steam 启动的情况")],
            default="1", input_fn=input_fn, output=output)
        if picked is None:
            return None
        launch = "auto" if picked == "1" else "steam"

    # -- 4. 解到哪 ---------------------------------------------------------
    _write(output)
    if choice == "env":
        # 装的时候还不知道会是哪个游戏（它可能捕获任意游戏），所以这里是**基目录**：
        # 扩展会在它下面按捕获到的游戏名建子目录，结果与其它方式的默认布局一致。
        default_out = Path.cwd() / "extracted"
        _write(output, "解出来的文件放哪？--env 这里是**基目录**：每个游戏在它下面单独一层，"
                       "并且保证不落在游戏目录里。")
    else:
        default_out = Path.cwd() / "extracted" / root.name
    answer = _ask("  输出目录", default=str(default_out), input_fn=input_fn, output=output)
    if answer is None:
        return None
    out = Path(os.path.expanduser(answer))
    if choice == "env":
        resolved = Path(os.path.abspath(out))
        if resolved == root or root in resolved.parents:
            _write(output, "  那在游戏目录里面，--env 不能往那里写；改用 %s" % default_out)
            out = default_out

    # -- 5. 值得单独问的两个选项 -------------------------------------------
    wait = None
    if choice == "env" and launch == "steam":
        wait_answer = _ask_yes_no("要不要在这里等它解完并汇报结果？", default=True,
                                  input_fn=input_fn, output=output)
        if wait_answer is None:
            return None
        wait = 900.0 if wait_answer else None

    # -- 6. 汇总确认 -------------------------------------------------------
    arguments = ["--game", str(root), "-o", str(out)]
    if choice == "env":
        if launch == "auto":
            arguments.append("--env-launch")
        else:
            arguments.append("--env")
            if wait is not None:
                arguments += ["--wait", "%.0f" % wait]
    elif choice == "inject":
        arguments.append("--inject")
        if launch == "manual":
            arguments.append("--inject-no-launch")
    elif choice == "runtime":
        arguments.append("--runtime")

    _write(output)
    _write(output, "接下来会执行：")
    _write(output, "  renpy-unpack " + " ".join(
        '"%s"' % part if " " in part else part for part in arguments))
    _write(output)
    steps_key = choice
    if choice == "inject":
        steps_key = "inject-manual" if launch == "manual" else "inject-auto"
    elif choice == "env":
        steps_key = "env-launch" if launch == "auto" else "env-steam"
    _write(output, "说明：")
    for line in _NEXT_STEPS[steps_key]:
        _write(output, "  " + line)

    confirmed = _ask_yes_no("就这样执行？", default=True, input_fn=input_fn, output=output)
    if confirmed is None:
        return None
    if not confirmed:
        _write(output, "已取消")
        return None
    return arguments
