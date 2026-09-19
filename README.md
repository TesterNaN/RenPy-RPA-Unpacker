# RenPy-RPA-Unpacker

解包 Ren'Py 的 `.rpa` 存档——**不自己实现 RPA 格式，而是借用游戏自己的 `renpy/loader.py`**。

这样做不是洁癖，是必需：Ren'Py 存档在真实游戏里被改得五花八门（索引字段加密 XOR、
数据偏移整体平移、扩展名和魔数换成 `.dll` / `ILOVEYOU`、分段存档、Python 2 pickle、
handler 自定义、解密编译进原生模块）。自己写解析器的结局就是"能解 80%，剩下 20%
报一个看不懂的错"。借游戏自己的代码，这些细节**全部**由写这个游戏的人处理过了。

三条路走到同一个目标：

| 后端 | 一句话 | 什么时候用 |
| --- | --- | --- |
| **静态 AST**（默认） | 把游戏 `loader.py` 里的解析代码**提取出来**，在自己进程里跑 | 一般情况。最快、不启动游戏、不写游戏目录 |
| **`--runtime`** | 用**游戏自带的 python** 起一次运行时，从外面调它自己的 `loader` | 静态后端读不出来，且不想写游戏目录 |
| **`--inject`** | 往 `game/` 放一个脚本，**让游戏自己解包**然后删掉 | 静态后端读不出来，且 `game/` 可写。能力最强 |

不管走哪条，**都以游戏自己的 loader 为准**。魔改解密的游戏不需要任何特殊处理。

> 三个后端的原理、实测数据、踩过的坑和失败尝试，都记在
> **[docs/NOTES.md](docs/NOTES.md)**。那份文档面向想改代码、或想判断结论是否可靠的人。

---

## 安装

需要 Python 3.9+（只用标准库，无第三方依赖）。

```bash
# 推荐：隔离安装，装完直接有 renpy-unpack 命令
pipx install git+https://github.com/TesterNaN/RenPy-RPA-Unpacker

# 或者
pip install git+https://github.com/TesterNaN/RenPy-RPA-Unpacker
```

不想装也行——仓库根目录的 `unpacker.py` 可以直接跑：

```bash
git clone https://github.com/TesterNaN/RenPy-RPA-Unpacker
cd RenPy-RPA-Unpacker
python unpacker.py --game "D:\Games\SomeGame" --list
```

`python -m renpy_unpack` 与 `renpy-unpack` 与 `python unpacker.py` 三者等价。

---

## 快速上手

```bash
# 1) 先看存档里有什么（不写盘，最快）
renpy-unpack --game "D:\Games\SomeGame" --list

# 2) 解包（默认输出到 <game>/extracted_files）
renpy-unpack --game "D:\Games\SomeGame" -j 16

# 3) 只要图片，跳过已存在的
renpy-unpack --game "D:\Games\SomeGame" -o unpacked \
    --match "*.png" --match "*.webp" --ext jpg --skip-existing
```

`--game` 指向**游戏根目录**（含 `renpy/` 的那一层）。不带 `--game` 时从当前目录向上找。

### 静态后端读不出来时

先用 `--runtime --list` 看一眼情况，再决定走哪条：

```bash
# 探一下这个游戏的原生解密（如 renpy.aescrypt / renpy.encryption）能不能用
renpy-unpack --game "D:\Games\SomeGame" --runtime --list

# 让游戏自己解包 —— 能力最强，需要 game/ 可写
renpy-unpack --game "D:\Games\SomeGame" --inject

# 用游戏自己的运行时读 —— 不写游戏目录，但慢一些
renpy-unpack --game "D:\Games\SomeGame" --runtime -o unpacked
```

**怎么选**（默认静态 AST，够用就别换）：

| 情况 | 用哪个 |
| --- | --- |
| 一般游戏、游戏自带 `loader.py` | 默认（不加参数） |
| 解密在原生模块里 / 只有 `loader.pyc`，且 `game/` 可写 | `--inject` |
| 同上，但 `game/` 写不进去（只读安装） | `--runtime` |
| 游戏脚本加载不了（坏 mod、标签重复定义） | `--runtime` |
| 只想先看看有没有原生解密 | `--runtime --list` |

`--inject` 和 `--runtime` **互斥**，同时给会直接报错让你选一个。
`--inject` 与 `--list` / `--dry-run` 也不能同用——条目清单只存在于游戏进程内部。

> **魔改游戏不用做任何特殊处理。** 工具默认优先使用**游戏自带的** `renpy/loader.py`，
> 而不是 SDK 里那份标准实现。只有游戏缺少 `loader.py` 源码时，才需要 `--loader`
> 手动指定一个替代品——而且它会校验两者声明的 handler/扩展名是否冲突，冲突就拒绝。

---

## 常用参数

| 参数 | 说明 |
| --- | --- |
| `--game PATH` | 游戏根目录（含 `renpy/` 的目录），默认从当前目录向上查找 |
| `--loader PATH` | 指定要提取的 `loader.py`（默认自动查找游戏自己的那份） |
| `-o, --output` | 输出目录，默认 `<game>/extracted_files` |
| `-j, --jobs` | 并行工作线程数，默认 `min(16, CPU*2)` |
| `--list` / `--dry-run` | 只列清单 / 只报告不写盘 |
| `--match GLOB` / `--ext EXT` | 过滤条目，可重复，`**` 可用 |
| `--skip-existing` | 已有文件跳过（增量解包） |
| `--include-loose` | 连散装文件（不在存档里的）一起导出 |
| `--runtime` | 用游戏自己的 python + Ren'Py 运行时读存档 |
| `--runtime-python PATH` | 手动指定游戏自带的解释器（自动探测失败时用） |
| `--inject` | 让游戏自己解包：往 `game/` 放一个生成的 `.rpy`，跑一次，跑完删掉 |
| `--inject-keep-script` | `--inject` 时保留生成的 `.rpy`（`.rpyc` 始终删除） |
| `--unsafe-pickle` | 用原生 `pickle.loads` 读索引（**仅限你信任的存档**） |
| `--write-core PATH` | 额外写出生成的读取器模块（排查问题用） |
| `-q, --quiet` | 不显示进度行 |

完整列表见 `--help`。

---

## 已知限制

完整版（含失败尝试和边界实测）在 [docs/NOTES.md](docs/NOTES.md#已知限制)，摘要：

- **静态后端拿不到编译进原生模块的解密。** `renpy.aescrypt` / `renpy.encryption`
  这类模块是编译进游戏自带解释器的，没有源码可提取。此时用 `--inject`
  （脚本跑在游戏进程里，模块已经加载好了）或 `--runtime`。
- **静态后端需要 `loader.py` 源码。** 只发 `loader.pyc` 的发行版走 `--inject`
  或 `--runtime`，或者用 `--loader` 指向同版本的 SDK——但那样读魔改游戏会出错，
  因为格式的权威已经不是它了。
- **`--inject` 需要 `game/` 可写，且游戏脚本能加载。** 写不进去时会明确提示改用
  `--runtime`，不会留半成品。注意 `Program Files` / Steam 库**不一定**只读，
  工具会先探测再动手。
- **RPAv1（`.rpi`）目前只能列清单。** 成员数据的落点没有实测样本，没有冒险实现。
  欢迎提供样本。

---

## 目录结构

```
unpacker.py                  # 直接运行的入口（保留原文件名）
selfextract.rpy              # 手工版自解包脚本：复制到 game/ 里，开一次游戏就行
                             # 不需要本工具、不需要 Python（详见文件头注释）
renpy_unpack/
    __init__.py
    __main__.py              # python -m renpy_unpack
    core.py                  # 发现游戏、构建读取器、并行提取、CLI、三个后端的调度
    ast_extract.py           # 静态后端：AST 提取核心（索引、依赖闭包、裁剪、unparse）
    runtime.py               # 活运行时后端：探测解释器/启动器、流式协议
    probe.py                 # 活运行时后端：在游戏进程里跑的探针源码（用 -c 传入）
    inject.py                # 自解包后端：生成脚本、跑一次游戏、读清单、清理
    _inject_body.py          # 注入脚本的正文模板
    runtime_shims.py         # RWopsIO 替身 + 受限 pickle 反序列化
    _fallbacks.py            # loader.py 里确实缺少某段时的兜底实现
docs/
    NOTES.md                 # 开发笔记：原理、实测、踩过的坑
tests/                       # 151 个用例，无第三方依赖
tests/rpa_factory.py         # 合成 RPAv1/2/3、分段、Python2 pickle、伪装 DLL 存档
```

---

## 运行测试

```bash
python -m unittest discover -s tests -t tests -v
```

部分用例要在**真实的 `renpy/loader.py`** 上跑（提取保真度、端到端回归）。
Ren'Py 的代码不随本仓库分发，所以测试会自己去机器上找一份：

1. 环境变量 `RENPY_SDK`（SDK 目录）或 `RENPY_LOADER`（直接指向 `loader.py`）
2. 仓库内 `renpy/loader.py` 或 `vendor/renpy/loader.py`（已被 `.gitignore` 忽略）
3. 常见安装位置扫描（`~/renpy-*`、`/opt`、各盘符下的 `renpy-*/renpy/loader.py` 等）

找不到时**相关用例跳过，不报错**，其余用例照常跑：

| 环境 | 结果 |
| --- | --- |
| 机器上有 Ren'Py | 151 个用例全部执行并通过 |
| 机器上没有 Ren'Py | 退出码 0：84 个用例实际执行，67 个跳过，0 错误 0 失败 |

（跳过有两种统计方式：32 个逐个跳过，另外 35 个因为所在类的 `setUpClass` 跳过而被
`unittest` 折叠成整类一条记录，所以 `testsRun` 显示的是 116 而不是 151。）

---

## 许可

[GPL-3.0-or-later](LICENSE)。

本仓库**不包含也不需要**任何 Ren'Py 源码：静态后端在运行时读取你自己机器上游戏自带的
`renpy/loader.py`，提取出的代码不会被打包或分发。Ren'Py 引擎本身由
[Ren'Py 项目](https://www.renpy.org/) 开发，采用其自有许可。

---

## 声明

仅供学习、备份和存档格式研究使用。请遵守你所在地区的法律以及游戏本身的许可协议，
不要用它分发你没有权利分发的资源。
