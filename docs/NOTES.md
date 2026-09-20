# 开发笔记与实测记录

这里记录 `RenPy-RPA-Unpacker` 每个设计决定背后的证据：真实游戏上的实测结果、
走过的错路、以及踩过的坑。**面向想改代码或想判断结论是否可靠的人**，
不是使用说明。

> 使用说明、安装、参数、已知限制摘要：[../README.md](../README.md)
>
> 这里的结论全部来自真实游戏实测；失败的尝试也保留着——「为什么不用某个方案」和「为什么用这个方案」一样重要。

## 目录

- [三个后端：静态 AST（默认）、活运行时（`--runtime`）与自解包注入（`--inject`）](#三个后端静态-ast默认活运行时--runtime与自解包注入--inject)
  - [为什么活运行时必须存在](#为什么活运行时必须存在)
  - [用法](#用法)
  - [它如何做到零侵入](#它如何做到零侵入)
  - [静态 AST 与活运行时的一致性验证](#静态-ast-与活运行时的一致性验证)
  - [第三个后端：让游戏自己解包（`--inject`）](#第三个后端让游戏自己解包--inject)
  - [实测五：存档伪装成系统 DLL（`Dream_of_BluePlanet`）](#实测五存档伪装成系统-dlldream_of_blueplanet)
- [核心原则：loader 是权威（但它也有边界）](#核心原则loader-是权威但它也有边界)
  - [规则：永远用游戏自己的 loader](#规则永远用游戏自己的-loader)
  - [边界：loader 不认识的东西，谁也读不出来](#边界loader-不认识的东西谁也读不出来)
  - [实测六：已经解包完的游戏（`Mainichikisushite`）](#实测六已经解包完的游戏mainichikisushite)
  - [关于"猴子补丁"：为什么它不是主力方案](#关于猴子补丁为什么它不是主力方案)
- [一个容易踩的坑：游戏目录里的同名文件](#一个容易踩的坑游戏目录里的同名文件)
  - [机制 1：Python 模块（`.py`）——由 RenpyImporter 处理](#机制-1python-模块py由-renpyimporter-处理)
  - [机制 2：Ren'Py 脚本（`.rpy`/`.rpyc`）——另一套系统](#机制-2renpy-脚本rpyrpyc另一套系统)
  - [存档也会进入同一个命名空间](#存档也会进入同一个命名空间)
  - [对本工具的影响（已加防护）](#对本工具的影响已加防护)
  - [对本工具的影响：一个真实的注入漏洞（已修）](#对本工具的影响一个真实的注入漏洞已修)
- [为什么是 AST 而不是自己写解析器](#为什么是-ast-而不是自己写解析器)
  - [实测：一个真实魔改样本](#实测一个真实魔改样本)
  - [实测二：全新的加密格式（handler 不是固定集合）](#实测二全新的加密格式handler-不是固定集合)
  - [实测四：标准格式 + 第三方交叉验证（`LOVE YURI / LoveYuri`）](#实测四标准格式--第三方交叉验证love-yuri--loveyuri)
  - [汇总：六个真实样本](#汇总六个真实样本)
  - [这一样本暴露的两个真实缺陷（已修 + 已加测试）](#这一样本暴露的两个真实缺陷已修--已加测试)
- [为什么不再用字符串偏移切割](#为什么不再用字符串偏移切割)
- [两个需要说明的技术点](#两个需要说明的技术点)
  - [1. 绕过了 Ren'Py 自身的一个 bug（分段存档）](#1-绕过了-renpy-自身的一个-bug分段存档)
  - [2. 安全](#2-安全)
- [上游矩阵：支持哪些 Ren'Py 版本](#上游矩阵支持哪些-renpy-版本)
- [已知限制](#已知限制)
- [测试套件的一个陷阱：用例「消失」而不是「跳过」](#测试套件的一个陷阱用例消失而不是跳过)

---

## 三个后端：静态 AST（默认）、活运行时（`--runtime`）与自解包注入（`--inject`）

| | 静态 AST（默认） | 活运行时（`--runtime`） | 自解包注入（`--inject`） |
| --- | --- | --- | --- |
| 做法 | 读 `renpy/loader.py` 源码，按名字取函数 | 用**游戏自带的 python** 起一次 Ren'Py 运行时，调它自己的 `loader` | 往 `game/` 放一个生成的 `.rpy`，**让游戏自己跑一遍** |
| 需要 `loader.py` 源码 | 是 | **否** | **否** |
| 需要游戏自带的 python 能跑起来 | 否 | **是** | **是** |
| 需要游戏脚本能被加载（能"启动"） | 否 | **否**（在加载脚本**之前**就停） | **是**（必须跑到 `init python early`） |
| 需要 `game/` 可写 | 否 | 否 | **是** |
| **原生编译的解密** | **做不到** | **可以**（`renpy.aescrypt` 等） | **可以** |
| 依赖的解释器 | 任意 Python 3.9+ | 游戏自带的 `lib/py3-*/python` | 游戏自带的 `lib/py3-*/python` |
| 速度 | 快（2003 条目约 30 ms） | 慢（同一游戏 459 MiB 约 30 s） | 中（406.9 MiB 2.3 s；1.5 GiB 15.3 s） |
| 副作用 | 无 | 无（不写游戏目录，见下） | 临时写入 `game/`，跑完即删 |
| 会执行游戏代码 | 否 | **是**（子进程） | **是**（完整启动一次） |

**默认仍是 AST**，这是更安全、更快、已在 5 个真实游戏上验证过的路径。
遇到**解密被编译进原生模块**的存档、或者只发 `loader.pyc` 的发行版时，
先试 `--inject`（能力最强），不方便写游戏目录再退 `--runtime`。

### 为什么活运行时必须存在

AST 提取的固有边界是：**编译进原生模块的代码拿不到源码**。
`小小的身影，重叠的内心` 就是这种情况——`renpy.aescrypt` 是个 Rust 扩展，
编译在 `lib/py3-windows-x86_64/librenpython.dll` 里（导出 `PyInit_renpy_aescrypt`），
磁盘上没有任何 `.py`。我实测过三条路：

| 尝试 | 结果 |
| --- | --- |
| 裸 `import renpy.loader` | **硬崩** — `module 'renpy' has no attribute 'config'`，连 traceback 都打不出（`lost sys.stderr`） |
| 自己 stub `renpy.config` | **硬崩** — 缺属性一路打地鼠（`log_to_stdout` → …），补不完 |
| 走完整 `bootstrap()` | **成功** — `renpy.aescrypt` 的 5 个函数全部可用，存档正常读取 |

结论：`renpy.config` 就是完整运行时，stub 不出来；必须走 `bootstrap()`，而那需要游戏启动器的
**分发方钩子**（`path_to_gamedir` / `path_to_logdir`），这些就在游戏自己的启动器 `.py` 里。

### 用法

```bash
# 看这次能否拿到原生解密（不读数据，只建索引）
python unpacker.py --game "D:\Games\MyGame" --runtime --list

# 用游戏自己的运行时解包
python unpacker.py --game "D:\Games\MyGame" --runtime -o unpacked

# 手动指定解释器（自动探测失败时）
python unpacker.py --game "D:\Games\MyGame" --runtime --runtime-python "D:\Games\MyGame\lib\py3-windows-x86_64\python.exe"
```

`--list` 会明确报告原生解密是否可用：

```
Archive reader: live runtime (the game's own Ren'Py)
  interpreter    : ...\lib\py3-windows-x86_64\pythonw.exe
  launcher hooks : TinyShadowsInterwovenHearts.py
  archives       : 6
  native crypto  : renpy.aescrypt -> decrypt_block, decrypt_file, decrypt_file_inplace, encrypt_block, register_trusted_script
  entries        : 2003 unique file(s)
```

### 它如何做到零侵入

它的承诺是**不往游戏目录写任何东西**：探针的源码通过 `-c` 传入，结果走管道返回。

理由**不是**"商店目录一定只读"——本机实测三个 Steam 安装的 `game/` 都可写，
`Program Files (x86)` 不等于只读——而是"读别人的存档，不该变成写别人的安装"。

> **这里我犯过一个错，值得留档。** 我一度断言"Steam 库目录是只读的"，依据是
> `Copy-Item` 报 `Access denied`。后来发现那次拒绝来自**我自己的沙箱**，不是 Steam：
> 同一个目录在放开限制后写入成功。**把沙箱的拒绝当成环境的性质**，
> 是这个项目里最贵的一类错误——它会让文档写下一个假的事实。

这点我踩了四个坑才做对，都是真实安装上才会暴露的：

1. **只有源码通过 `-c` 传入。** 游戏目录里不会多出探针文件，也就不需要
   `--compile`（那会往脚本目录写 `.rpyc`）。
2. **不需要任何可写位置。** 结果**走管道流式返回**，不用临时文件。
   这样 `--runtime --list` 在只读安装上也能直接跑，不需要 `-o`。
   （早期版本用 payload 文件，既在只读安装上失败，又把 scratch 目录
   留在了输出目录里，被当成解出来的文件。）
3. **不挑错平台。** Ren'Py 会同时附带 `lib/py3-windows-*` 和 `lib/py3-linux-*`，
   早期版本按字母序选中了 Linux 那个，报 `WinError 193`（不是有效的 Win32 程序）。
   现在先按当前平台过滤。
4. **连字节码缓存都不写。** 这条是后来才发现的，而且是直接打脸前三条的：
   探针要 `sys.path.insert(0, game)` 然后 `import renpy.bootstrap`，CPython 编译
   游戏自己的 `renpy/*.py` 之后，会把 `.pyc` **缓存进安装目录**的
   `renpy/__pycache__/`。真实安装上一次运行留下了 **52 个 `.pyc`**——
   与"不写游戏目录"直接矛盾。现在两层都堵住了：探针的第一条语句是
   `sys.dont_write_bytecode = True`，同时给子进程设 `PYTHONDONTWRITEBYTECODE=1`。
   用游戏自带的解释器实测：不加任何一层会生成 `__pycache__`，两种方式任加其一都不会。

   > 顺带一个容易误判的点：**游戏自己正常启动也会写这个目录**（Ren'Py 没有关掉
   > 字节码写入，实测游戏里搜不到 `dont_write_bytecode`）。所以安装里出现
   > `renpy/__pycache__` 本身不代表这个工具来过，不能当成痕迹来判断。

另外两个协议细节也值得记：

* **命令行长度**：文件名是作为 argv 传的。2003 个名字 = 86 KB，超过 Windows 的
  32767 字符上限，导致**整批全部失败**（报 `WinError 206`，症状和原因完全不像）。
  现在每批最多 200 个名字，并可调 `--runtime-batch-mb`。
* **提前退出**：探针不需要 `--compile`。Ren'Py 的 argparse 拒绝我们多传的参数时
  会在**开窗口、编译脚本之前**就退出，Runtime 此时已经活着（`renpy.config` 和
  `renpy.loader` 都在），所以这个"用法错误"恰好是最干净的退出方式。

### 静态 AST 与活运行时的一致性验证

在 `小小的身影，重叠的内心` 上做了交叉验证：

* 条目列表：**完全相同**（各 2003 条）
* 全量逐字节：**静态与活运行时各解一遍**，2003 个文件全部 SHA-256 一致，零差异
* 性能：runtime 后端 459.2 MiB 约 30 秒（约 15 MiB/s，含 11 次 Ren'Py 启动）；
  静态后端同一游戏不到 1 秒（只做索引）

### 第三个后端：让游戏自己解包（`--inject`）

`--runtime` 是**从外面**驱动游戏的运行时；`--inject` 是**从里面**——往 `game/`
放一个生成的脚本，让游戏启动时自己跑一遍解包，跑完把脚本删掉。

```renpy
# <游戏根>/game/zz_renpy_unpack_inject.rpy（由 --inject 自动生成，也会自动删除）
init python early:
    import renpy.loader as loader
    for _archive, index in loader.archives:      # 索引此时已经建好
        for name in index:
            handle = loader.load_from_archive(name)   # 调游戏自己的函数
            ...
    os._exit(0)                                  # 在开始播之前退出，不开窗口
```

#### 为什么可行：时机

| 时机 | 位置 | 此时状态 |
| --- | --- | --- |
| `renpy.loader.index_files()` | `renpy/main.py:367` | **索引已建好** |
| `renpy.game.script.load_script()` | `renpy/main.py:412` | `init python early:` **在这里执行** |
| `renpy.display.core.Interface()` | `renpy/main.py:572` | 到这一步才会建渲染器/开窗口 |

`os._exit(0)` 在第二行和第三行**之间**就退出了，所以**窗口根本不会被创建**
（不是"开完立刻关"）。

所以脚本运行时，需要的一切**游戏都已经准备好了**：`loader.archives` 是现成的
内存索引（`[(存档路径, {名字: [(偏移, 长度), ...]}), ...]`）、`load_from_archive()`
是游戏自己的函数，连 `renpy.aescrypt` 这种原生模块也已经加载。

脚本只做三件事：**遍历索引、调 `load_from_archive()` 读数据、写盘**。
它**完全不解析存档格式**——所以同一个脚本在标准 RPAv3、伪装成 `.dll` 的存档、
以及只发 `loader.pyc` 的发行版上都能跑，一个字节都不用改。

这也回答了一个看起来需要"理解 loader 内部实现"的问题：**不需要**。
游戏已经把索引和解密函数都准备好了，我们只是借用。

#### 实测（结果都做了逐字节比对）

| 游戏 | 存档形态 | 结果 | 耗时 |
| --- | --- | --- | --- |
| `LoveYuri`（副本） | 标准 `RPA-3.0` | 1524/1524 一致，406.9 MiB | 2.3 s |
| `Dream_of_BluePlanet`（副本） | 伪装 `.dll` + 自定义 magic + 索引字段顺序被换 | 2971/2971 一致，1.5 GiB | 15.3 s |
| `LoveYuri` 的 **`loader.pyc`-only** 副本 | 同上（但没有任何 `loader.py` 源码） | 1524/1524 一致，406.9 MiB | 2.3 s |
| **`LoveYuri` 的 Steam 安装本体** | 标准 `RPA-3.0`，`--ext .rpy` 只取脚本 | 23/23 与静态后端逐字节一致，`game/` 零残留 | 2 s 级 |

第三行是关键：**同一棵目录树**，静态后端直接报
`could not find this game's own renpy/loader.py`，`--inject` 正常解包、退出码 0。

第四行验证的是另一件事——**真能写进一个已安装的游戏，并在跑完后把它自己加的东西
清干净**（`game/` 里 `zz_*` 为 0、`.rpyc` 为 0、scratch 目录为 0）。
`--inject` 需要 `game/` 可写这条约束，在这台机器的三个 Steam 安装上都成立
（见下）。

#### 代价（说清楚）

* **需要 `game/` 可写——但这条几乎从不需要。** 说清楚它的真实分量：

  * **只读本身从来不构成死路。** `--runtime` 不写任何东西，静态后端也不写。就算 `game/`
    完全写不进去，只要**有 `loader.py` 源码**（静态）**或游戏能启动**（`--runtime`），
    就还有路走。只读只会**把 `--inject` 从选项里去掉**，仅此而已。
  * **真正无解的组合只有一种**：**没有 loader 源码，且游戏起不来**（那时静态没得读、
    `--runtime` 起不来、`--inject` 也跑不动）。**和只读无关。**
  * 而且"只读安装"比听起来罕见得多：本机三个 Steam 安装
    （`Dream_of_BluePlanet`、`LOVE YURI`、`黄莓C HuangmeiC Demo`，都在
    `Program Files (x86)\Steam\steamapps\common\`）的 `game/` **实测全部可写**，
    真实安装上的那次 `--inject` 也跑通了（上表第四行）。
    ——我一度把这里写成"Steam 库是只读的"，那是**把自己沙箱的拒绝当成了环境的性质**，
    见本节开头那段留档。

  本后端**先探测再动手**，真写不进去就明确告诉你改用 `--runtime`，而不是写一半留一堆半成品。
* **需要游戏能把脚本加载起来。** `init python early:` 本身就是一次脚本加载，
  所以游戏脚本加载不了（坏 mod、缺依赖、标签重复定义）就没戏。
  ——`--runtime` 没有这条约束，它在加载脚本**之前**就停了。
* **会真的启动一次游戏进程。** 脚本在开始播之前 `os._exit(0)`，窗口根本不会被创建
  （见上表：退出发生在 `main.py:412` 和 `:572` 之间，实测整个解包 2.3 s 内完成），
  但杀毒软件/Steam 会看到这个进程。

#### 为什么这不叫"注入风险"

脚本是**我们自己的进程在启动游戏之前、原子地写下去的**：

1. 生成脚本 → 2. 启动游戏（游戏读到它）→ 3. 游戏退出 → 4. 删除脚本

不存在"别人趁机塞一个脚本进来"的窗口期，也不需要在游戏运行时改内存/挂钩子。
这和"安装被第三方篡改"是两件完全不同的事。

#### 清理：两样都要删

| 文件 | 不删的后果 |
| --- | --- |
| `zz_renpy_unpack_inject.rpy` | 留在游戏目录里（无害，但不干净） |
| `zz_renpy_unpack_inject.rpyc` | **每次开游戏都会把解包重跑一遍** |

`--inject-keep-script` 只保留 `.rpy`；**`.rpyc` 永远删**。
实测三次完整运行（两个副本 + 一次真实 Steam 安装）之后，
`game/` 里的 `zz_*` 数量、`.rpyc` 数量、scratch 目录数量都是 **0**。

#### 用法

```bash
# 让游戏自己解包（默认输出到 <game>/extracted_files）
python unpacker.py --game "D:\Games\MyGame" --inject

# 保留下生成的脚本，方便自己看它到底干了什么
python unpacker.py --game "D:\Games\MyGame" --inject --inject-keep-script
```

* `--inject` 与 `--runtime` **互斥**——两者都是"用游戏自己的运行时"，工具会让你选一个，
  而不是猜。
* `--list` / `--dry-run` **不配合 `--inject`**：条目清单**只存在于游戏进程内部**，
  没有"只看不跑"的可能。工具会直接说明原因，而不是打印一个空清单。
* 脚本运行时的过程日志写在输出目录旁的 scratch 目录里，出错时会把它最后几行贴出来；
  正常结束时随 scratch 一起删除。

> **仓库里那份 `selfextract.rpy` 还有用吗？** 有，但只在"完全不想装 Python、也不想跑
> 这个工具"的时候用：把它手工复制到 `game/`，双击游戏，结果落在 `_unpack_out/`。
> 它比 `--inject` **少两样东西**，用之前请知道：
> 1. 它**没有"跑过一次就跳过"的判断**，而 `.rpyc` 一旦留在 `game/` 里，
>    **每次开游戏都会把整个解包重跑一遍**（包括那 1.5 GiB 的写盘）。
> 2. 它不清理自己——`.rpy` 和 `.rpyc` 都要你手工删。
>
> `--inject` 就是把这个流程做成了安全的版本：自动探测可写、自动跑、自动连 `.rpyc`
> 一起删。这两个坑我都真实踩过（三个游戏目录里各留了一份 `selfextract.rpyc`）。

---

### 实测五：存档伪装成系统 DLL（`Dream_of_BluePlanet`）

这一作把**存档改了扩展名**，`game/` 里一个 `.rpa` 都没有：

| 文件名 | 大小 | 真实身份 |
| --- | --- | --- |
| `vcruntime140.dll` | 585 MB | 存档 |
| `mfplat.dll` | 455 MB | 存档 |
| `WindowsCodecsRaw.dll` | 387 MB | 存档 |
| `ucrtbase.dll` | 225 MB | 存档 |
| `msvcp140.dll` | 1.5 MB | 存档 |
| `steam_api.dll` / `steam_api64.dll` | 271 / 312 KB | **真的是 DLL** |

内容是 RPAv3，但魔数连同扩展名一起被换了：

```python
archive_extension = ".dll"                    # 原 ".rpa"
def get_supported_extensions(): return [".dll"]
def get_supported_headers():    return [b"ILOVEYOU"]      # 原 b"RPA-3.0 "
...
index[k] = [(offset ^ key, dlen ^ key) for dlen, offset in index[k]]
#                                           ^^^^^^^^^^^^^ 字段顺序也被换了
```

`ILOVEYOU` 和 `RPA-3.0 ` **都是 8 字节**，所以后面所有字段偏移不变。
数据从 offset 51 开始（40 字节头 + 11 字节填充）。

**结果：2971/2971 成功，1.5 GiB，零失败。** 并用独立解码器逐字节复现：
**2971 个文件 0 处不一致**；659 PNG 魔数全对、2248 MP3 的 ID3 全对、42 个 `.rpyc`
的 `RENPY RPC2` 全对。

#### 这个样本暴露的缺陷（已修 + 已加测试）

**我的工具一开始在它上面完全失败**，报"找不到存档"——**游戏里全是存档**。
原因不是格式（AST 提取能正确读这种 handler），而是**存档发现**：我只找
`.rpa`/`.rpi`，硬编码了扩展名。

修法正是这个项目的原则：**扩展名也该从 loader 里读**。Ren'Py 自己就是问 handler
`get_supported_extensions()` 来决定哪些文件当存档，所以：

* `archive_extensions()` 从 handler 的 `get_supported_extensions()` 读扩展名；
* `archive_headers()` 从 `get_supported_headers()` 读魔数；
* 常规扩展名找不到存档时，用这套声明再扫一遍，并用**头部嗅探**确认。

头部嗅探顺带解决了"怎么区分真 DLL"——`steam_api.dll` 以 `MZ` 开头，直接被排除。
实测扫到的 7 个 `.dll` 里正好滤掉那 2 个真的。

这里还踩了一个我自己造的坑：`get_supported_headers()` 返回的是 **bytes 字面量**
`b"ILOVEYOU"`，我第一版只扫 str 字面量，结果**头部列表为空 → 嗅探等于关闭 → 真 DLL 也被当成存档**。
现在 str/bytes 都收。

> **更正**：我在"实测三"里说过 LoveYuri 那份 loader 的存档部分与官方一致——
> 那个结论是对的。但我曾顺带提到 `Dream_of_BluePlanet` 的 loader"存档部分与官方一致"，
> **那句是错的**：我当时只过滤了 `RPA|offset|seek|key` 等关键词做 diff，
> 恰好漏掉了 `get_supported_headers` 那一行。实际它改了魔数和扩展名两处。

---

## 核心原则：loader 是权威（但它也有边界）

一句话：**不要假设格式，去问游戏的 loader。** 五个样本里三个是魔改，而且三种魔改互不相同：

| 样本 | 魔改 |
| --- | --- |
| 黄莓C | 假 offset + 配套 `.zip` + 偏移 `-33` |
| 小小的身影 | 新增 RPAE-2.0 AES handler |
| Dream_of_BluePlanet | 魔数 `ILOVEYOU` + 扩展名 `.dll` + 索引字段换序 |

`Dream_of_BluePlanet` 的字段换序最能说明问题：它的 `read_index` 把索引元组按
`(dlen, offset)` 解包。**AST 提取原样保留了这行，所以结果是对的**；而任何"我读过
RPA 文档所以我懂格式"的实现，在这里会安静地解出垃圾文件——不报错，只是内容全错，
比崩溃更难发现。

这个原则贯穿三处，**都是问 loader 而不是猜**：

1. **解密函数** — 从 `loader.py` 取 `read_index` / `load_from_archive` 的真源码；
2. **用哪些 handler** — 从 `archive_handlers.append(...)` 读注册表（不是硬编码 v1/v2/v3）；
3. **哪些文件是存档** — 从 `get_supported_extensions()` / `get_supported_headers()` 读扩展名和魔数。

第 3 条是 `Dream_of_BluePlanet` 逼出来的：我原本硬编码 `.rpa`/`.rpi`，在这个游戏上
报"找不到存档"——而它 1.28 GB 的存档就在 `game/` 里，只是叫 `.dll`。

### 规则：永远用游戏自己的 loader

游戏的 `renpy/loader.py` **默认就是用它的**，而且是**强制**的——不是"优先"：

* 有游戏自带的 loader 时，`--loader` **不会**覆盖它；两者不是同一个文件就直接报错：

  ```
  error: this game ships its own loader: ...\Dream_of_BluePlanet\renpy\loader.py
    --loader was given: <SDK>\renpy\loader.py
    The game's own loader is the authority on its archive format. Because it is present, --loader is not used.
    They disagree on the archive format, which is exactly why the game's own loader is required:
      game  : extensions ['.dll', '.rpa', '.rpi'], headers [b'ILOVEYOU', b'RPA-2.0 ', b'x\x9c']
      --loader: extensions ['.rpa', '.rpi'], headers [b'RPA-3.0 ', b'RPA-2.0 ', b'x\x9c']
    Drop --loader, or point it at this game's own loader.py.
  ```

* 也**不再**从游戏目录的上级去找 loader——SDK 摆在游戏旁边不是"这个游戏的 loader"。
* `--loader` 的唯一用途是**补上缺失的 loader 源码**（比如只发 `loader.pyc` 的编译版），
  此时它也必须是**同一发行版**的 loader。

为什么必须这么严：两者的判定是**相反**的，不存在"退而求其次"。

| | 扩展名 | 魔数 |
| --- | --- | --- |
| 官方 SDK loader | `.rpa`, `.rpi` | `RPA-3.0 `, `RPA-2.0 `, `x\x9c` |
| Dream_of_BluePlanet 自己的 | `.dll`, `.rpa`, `.rpi` | **`ILOVEYOU`**, `RPA-2.0 `, `x\x9c` |

拿 SDK 的 loader 去读这个游戏：`game/` 里没有 `.rpa` → 一个存档都找不到；
即便强行扫到 `.dll`，头部嗅探也会因为 `ILOVEYOU != RPA-3.0 ` 而拒绝。
**报出来的错会是"找不到存档"——而 1.28 GB 的存档就躺在那里。**

### 边界：loader 不认识的东西，谁也读不出来

这条原则也限定了能力上限：**loader 不知道的格式，用 loader 也读不出。**
真正读不出来的情况是 loader 本身缺失或残缺——那时任何工具都读不出来，因为没人知道格式了。

已知的边界情况（都实测过，都会明确报错而不是猜）：

| 情况 | 静态 AST | `--runtime` | `--inject` |
| --- | --- | --- | --- |
| 游戏自带 `loader.py` | ✅ | ✅ | ✅ |
| 只有 `loader.pyc` | ❌ 需 `--loader`（且必须是同一发行版） | ✅ 可试 | ✅ **实测通过** |
| 解密在原生模块里（如 `renpy.aescrypt`） | ❌ 拿不到源码 | ✅ | ✅ |
| **游戏脚本加载失败**（如标签重复定义、坏 mod） | ✅ | ✅ **在加载脚本前就停** | ❌ 走不到 `init python early` |
| python 运行时本身起不来（`renpy/` 被裁掉、缺 DLL） | ✅（只要有 `loader.py`） | ❌ | ❌ |
| handler 声明了磁盘上没有的扩展名 | ❌ 明确报错 | ❌ 同理 | ❌ 同理 |
| `game/` 只读 | ✅ | ✅ | ❌ **明确拒绝** |
| `--loader` 指向不匹配的 loader | ❌ **明确拒绝** | — | — |

**结论**：AST 是默认（更轻、更安全、什么都不要求），`--inject` 能力最强
（补齐"原生编译"和"只有 pyc"两个盲区，代价是要写 `game/`，且游戏脚本得能加载），
`--runtime` 是两者的中间态——**不写任何东西，也不要求脚本能加载**（只要求游戏自带
的 python 能把运行时起到 loader 就绪）。三者都以**游戏自己的 loader** 为准。

---

### 实测六：已经解包完的游戏（`Mainichikisushite`）

这个游戏**根本没有存档，也没有加密**。`game/` 里全是明文散文件：

| 类型 | 数量 | 大小 | 头部 |
| --- | --- | --- | --- |
| `.ogg` 音频 | 2156 | 305 MB | `OggS` |
| `.webp` 图片 | 771 | 1048 MB | `RIFF` |
| `.png` 图片 | 187 | 106 MB | `\x89PNG` |
| `.rpy` 源码 | 77 | 3.1 MB | 明文（`label A000:`） |
| `.rpyc` 字节码 | 77 | 2 MB | `RENPY RPC2` |
| `.ttf` 字体 | 8 | 58 MB | — |

没有 `.rpa`、`.rpi`，也没有伪装成 `.dll`/`.bin`/`.dat` 的东西。
它的 `loader.py` 就是官方原版（MD5 `6656A273…`，与 SDK 一致）——**没有魔改，因为没什么可藏的**。

**资源你不需要解包，直接就是可用的。** 这也说明"没加密"和"有存档但没加密"是两件事：
这里是**连存档都没有**。

#### 顺带改掉的一个误导性提示

之前对它输出的是：

```
error: found no .rpa/.rpi archive looking in ...
       Pass --game pointing at the game's root directory.
```

**这是在撒谎**：它暗示"你路径给错了"或"工具失败了"。实际上这里没有失败，也没有可做的事。
现在区分三种情况，各说各的：

| 情况 | 提示 |
| --- | --- |
| 资源是散文件（本样本） | `appears to be already unpacked: its scripts and assets are plain files on disk ... There is nothing for this tool to extract.` |
| `--loader` 与自带 loader 冲突 | 报冲突，说明 `--loader` 未被使用以及两者对格式的判断差异 |
| 真的找不到存档 | 报出 loader 声明了哪些扩展名、哪些候选文件被头部嗅探拒绝 |

判定"已解包"用的是**浅扫描**（只看 `game/` 及其一级子目录，找到 3 个 `.rpy` 或多于 0 个就够）——
它只影响一句错误措辞，不值得为此遍历几 GB 的资源树。实测该路径耗时 **0.09 秒**。

另外顺手补了一处透明性问题：如果把 `--game` 指到 `...\SomeGame\renpy`（很自然的误操作），
工具会向上爬到真正的游戏根，现在会**明说**：

```
  game root      : D:\...\Dream_of_BluePlanet
  (climbed from  : D:\...\Dream_of_BluePlanet\renpy)
```

---

### 关于"猴子补丁"：为什么它不是主力方案

一个自然的想法是：**别提取了，直接在游戏自己的进程里 monkeypatch `load_from_archive` 不就行了？**
我实测了，答案是"能做，但不能无条件"，而且有几个反直觉的坑：

**坑 1：补模块属性截不到胡。** `load_from_archive` 不是被直接调用的，而是**注册进回调列表**：

```python
file_open_callbacks.append(load_from_archive)   # 存的是函数对象，不是名字
```

实测结果：

```
file_open_callbacks: ['load_from_file_open_callback', 'load_from_filesystem', 'load_from_archive']
callback holds the ORIGINAL object: True
callback holds the PATCHED object : False
--- 调 renpy.loader.load()（真正的入口）---
  spy 被调用 0 次      <- 补丁完全没生效
--- 直接调模块属性 ---
  spy 被调用 1 次      <- 只有直接调才看得到
```

之后改 `renpy.loader.load_from_archive` 只改模块属性，列表里那个仍指着原函数。
而且那次 `load()` 走的是**文件系统回调**（散文件优先于存档），压根没经过 archive 回调。

**坑 2：包装 `renpy.main.main` 会让 Ren'Py 崩。** 我试过把 `main` 换成自己的闭包以提前接管，
结果 `Cannot pickle renpy.main.main`——Ren'Py 要 pickle 它做回滚，闭包不可 pickle。

**坑 3：`-c` 的作用域陷阱。** 探针通过 `-c` 传入，它的局部作用域**不是** trace 回调看到的全局作用域，
所以 `settrace` 回调里引用一个全局名会 `NameError`。必须把异常类型当参数传进去。

**坑 4（最重要）：无条件的代价是"游戏必须能启动"**，而这并不成立：

> **LoveYuri 现在自己都启动不了。** 它的 `game/extracted/` 里有 **1524 个文件**——是别人用别的工具
> 解出来的，和 `game/rpy/` **重复定义了同一个 `gui.rpy`**，Ren'Py 直接报
> `ScriptError: Name (..., 519) is defined twice` 然后崩掉。

而真正的脚本加载发生在 `renpy.main.main()` **里面**，`renpy.loader` 的导入则更早——
在 `renpy.import_all()` 里，也就是 `main()` 之前。所以**一个"让 bootstrap 自己跑完"的实现
必然要为启动失败付出代价**，而这两步之间恰好有一段安全窗口。

**我的做法**：不 monkeypatch，而是**直接调用游戏自己的函数**——`loader.index_archives()` 和
`loader.load_from_archive()`，这本质上是"用自己的方式拿到同一个函数对象"，比 monkeypatch 稳。
并且用 `sys.settrace` 在 `renpy.loader` 进入 `sys.modules` 的**那一刻**停下，
**在脚本加载之前**（`renpy.import_all()` 之后、`main()` 之前），于是游戏能不能启动都不影响存档读取。

实测：LoveYuri 的 runtime 后端 **exit=0，1524 条目**——尽管这个游戏自己跑不起来。

**结论**：原则（用游戏自己的 loader）是主力，`--runtime` 是有代价的补充——
代价**不是**"要求游戏能启动"（它**不要求**：在脚本加载之前就停了），
而是慢（每次都要起一个 Ren'Py 进程）和"确实执行了游戏的引导代码"。
**默认仍是 AST**：什么都不要求、不执行任何游戏代码、更快、5 个游戏验证过。

---

## 一个容易踩的坑：游戏目录里的同名文件

这是被一个真实报错引出来的问题：`ScriptError: Name (..., 519) is defined twice`。
答案是**会冲突，但有两种完全不同的机制**，而且**不是"import 了整个 game"**。

### 机制 1：Python 模块（`.py`）——由 RenpyImporter 处理

Ren'Py 自己装了一个导入器，而且**插在 `sys.meta_path` 最前面**，优先级高于所有标准导入器：

```python
sys.meta_path.insert(0, RenpyImporter())      # renpy/importer.py:357
```

我用真实的 `RenpyImporter` 类实测了它认得哪些名字（stub 掉 `renpy.loader` 后直接问它）：

```
=== prefixes the importer searches ===
   ['game/', 'common/', '']
=== parsed cache ===
   extra        -> extra/          package=True      # 目录且无 __init__.py = 命名空间包
   extra.dup    -> extra/dup.py    package=False
   json         -> json.py         package=False     # 故意遮蔽标准库
   os           -> os.py           package=False     # 同上
   shared       -> shared.py       package=False
   rpy.gui      -> rpy/gui.py      package=False
=== which would the importer claim? ===
   shared  CLAIMS   os  CLAIMS   json  CLAIMS
   gui     no       dup no       something_ren no   sys no
```

三个关键结论：

1. **只认 `.py` 文件。** `_cache_entries()` 明确过滤：

   ```python
   files=(fn for _, fn in renpy.loader.game_files
          if fn.endswith(".py") if not fn.endswith("_ren.py"))
   ```

   所以 `.rpy` 脚本**不经过导入器**（`gui` / `dup` 都是 `no`）。带 `_ren.py` 后缀是官方留的**规避冲突的办法**。
2. **目录名也可以被 import。** 目录里没有 `__init__.py` 就变成命名空间包。
3. **名字是带前缀的完整路径，不是 basename。** 所以 `game/a/util.py` 和 `game/b/util.py` 是 `a.util` 和 `b.util`——**不冲突**。
   而 `game/util.py` 和根目录 `util.py` 会解析成同一个 `util`（因为前缀是 `''`）——**冲突**。

反过来说：**`game/` 不是被整体 import 的**，只有你（或游戏）真的 `import` 了某个名字，那个文件才会被加载。
但因为它优先级最高，一旦同名，**stdlib 也会被它抢走**——`os.py`/`json.py` 放在游戏目录里是真的会生效的。

### 机制 2：Ren'Py 脚本（`.rpy`/`.rpyc`）——另一套系统

跟导入器**毫无关系**。Ren'Py 递归扫描 `game/` 把每个脚本都加载，然后检查 label 重名：

```python
"Name %s is defined twice, at %s:%d and %s:%d."    # renpy/script.py:675
```

**当初 LoveYuri 就是这一种**，不是模块冲突：

```
game/rpy/gui.rpy              （散文件）
game/extracted/rpy/gui.rpy    （别人解出来的）
-> 同一个 label 定义两次 -> 游戏拒绝启动
```

注意这里的冲突判定用的是**脚本名 + label 名**，和模块的"带前缀完整路径"规则不同：
`.rpy` 只要 label 重名就炸，哪怕文件在不同目录。

### 存档也会进入同一个命名空间

`scandirfiles_from_archives` 把**存档里的条目也加进同一份 `game_files`**。
所以"存档里有一份 + 磁盘上有一份"同样会冲突——这正是上面那个案例。

### 对本工具的影响（已加防护）

我原本以为默认输出目录在 `game/` 里，实测发现**不在**：

```
默认输出: <game根>/extracted_files      不在 game/ 内
```

但**如果用户用 `-o` 指到 `game/` 里面**，下一次解包就会**自己制造**上面那个冲突。
所以现在会警告（仅警告——把资源解到 `game/tl/` 覆盖翻译是合法用法）：

```
warning: the output directory is inside this game's game/ directory (...), and
  23 extracted file(s) are Ren'Py scripts.
  Ren'Py scans game/ recursively, so a later run could define the same label
  twice and stop the game from starting. Consider an output directory outside game/.
```

只有当**输出在 `game/` 内**且选中项**含 `.rpy`/`.rpyc`** 时才提示，纯资源不提示。

---

### 对本工具的影响：一个真实的注入漏洞（已修）

顺着上面的机制查下去，发现**我自己的工具有个洞**——它能被用来往 `game/` 里注入脚本，
从而复现那个"游戏起不来"的冲突，甚至注入能在游戏里执行的东西。

**漏洞条件**（这是用户指出的方向，我实测确认了）：

```
--game=<root>/game  --loader=<任意 loader>
  game root : ...\MyGame\game
  默认输出   : ...\MyGame\game\extracted_files     <- 落在 game/ 里！
```

成因：`output_dir()` 是 `<game_root>/extracted_files`，而 `--game` 如果直接指向
`game/` 目录本身，`game_root` 就等于 `game/`。一旦写进去，Ren'Py 会递归加载这些脚本。

**为什么原来的警告没拦住**：警告拿输出跟 `<game_root>/game` 比，而这里 `game_root`
本身就是 `game/`，比的是 `.../game/game` —— **一个不存在的路径**，所以判定恒为"安全"。

**修法**：默认输出不再靠猜，而是**结构性地判定出 Ren'Py 真正加载的那个目录**：

```python
def _renpy_game_dir(root):
    # root 或任一祖先里，名为 game 且"看起来是游戏目录"的那个
    # 判定：旁边有 renpy/  → 完整安装
    #       里面有 .rpa/.rpy/.rpyc → 裁剪/重打包的发行
```

然后 `output_dir()` 默认取 `_safe_output_root(game_root) / "extracted_files"`，
保证**永远不落在 Ren'Py 会扫描的目录里**。实测：

| 调用方式 | 修复前 | 修复后 |
| --- | --- | --- |
| `--game=<root>` | `<root>/extracted_files` | 不变 ✅ |
| `--game=<root>/game` | **`<root>/game/extracted_files`** ❌ | `<root>/extracted_files` ✅ |
| `--game=<root>/renpy` | 爬回根（本来就对） | 不变 ✅ |
| `--game=<root>/game`（无 `renpy/`，需 `--loader`） | **`<root>/game/extracted_files`** ❌ | `<root>/extracted_files` ✅ |

**显式 `-o` 仍然被尊重**（把资源解到 `game/tl/` 覆盖翻译是合法用法），但警告现在
用**同一套结构判定**，所以不会再漏。

回归测试 `TestOutputInsideGameDir` 覆盖上表四种情况 + "只有资源时不该警告"，
变异检验：把 `output_dir()` 改回旧写法 → **测试立刻抓住**。

> 顺带说清威胁模型：这个洞**不是**"解包器被远程利用"，而是"**解包器在用户没有明确要求的情况下，
> 把文件写到了游戏会执行的位置**"。如果 `--game` 指错（指到 `game/` 里）再跑一次解包，
> 就可能让游戏起不来——而且用户不会意识到是自己刚跑的那条命令造成的。

---

## 为什么是 AST 而不是自己写解析器

**这是这个工具存在的根本理由。** 很多 Ren'Py 游戏会魔改解密逻辑，而魔改方式千奇百怪、
无法穷举。自己写解析器（不管是 `struct` 手解还是抄一份标准实现）在魔改面前必然失效；
只有把**游戏自己那份解密函数**取出来用，才能正确解开。

AST 方案正好做到这一点：按名字定位 `RPAv3ArchiveHandler.read_index`、`index_archives`、
`load_from_archive`，把它们**原样的源码**搬过来用。游戏怎么改，就跟着怎么走。

### 实测：一个真实魔改样本

在 `黄莓C HuangmeiC Demo`（Steam，Ren'Py 8.5.3）上验证过。它的 `renpy/loader.py`
被改写过（与官方 SDK 的 MD5 不同：`5C84E526…` vs `6656A273…`），改动是三处配合：

```python
# 1) read_index 不再读取 header 里的 offset，只取 key，然后解压"剩下的全部"
l = infile.read(34)
key = int(l[25:33], 16)
index = loads(zlib.decompress(infile.read()))

# 2) index 里的值用 `offset` 做 XOR，而 offset 从未被赋值
index[k] = [(offset ^ key, dlen ^ key) for offset, dlen in index[k]]
#            ^^^^^^ 故意不定义：此时 offset 就是 key，使 XOR 自反

# 3) 取数据时把偏移整体减 33
offset -= 33
rv = RWopsIO(afn, "rb", base=offset, length=dlen)
```

外层还有一层伪装：header 里的 index 偏移字段填的是**假的 0x2d40b549（759 MB）**，
而文件本体只有 21 KB，真正的索引固定跟在 40 字节头之后（这个 759,215,433 恰好
= 配套 `new_archive.zip` 的大小 759,215,400 **+ 33**——这就是那个 33 的来源）。

结果对比：

| 读取方式 | 结果 |
| --- | --- |
| 官方 SDK 标准 `read_index` | **失败**（按假偏移 seek 到 759 MB 处，zlib 报错） |
| 游戏魔改版但去掉 `offset -= 33` | **失败**（读到 `b''`） |
| AST 提取游戏自带 loader | **1447 个条目全部正确，解出 724 MiB** |

完整解包 1447 个文件全部成功，且魔数校验 100% 通过：897 PNG、489 OGG、
53 个 `.rpyc`（`RENPY RPC2`）、4 TTF、3 WebM、1 OTF——`rpyc` 魔数正确说明解密确实到位。

回归测试 `tests/test_unpack_roundtrip.py::TestObfuscatedArchives`
用合成存档复现了同一套魔改（假偏移 + 配套 `.zip` + `-33`），并断言
**标准 loader 读不了、只有游戏自己的 loader 能读**。

### 实测二：全新的加密格式（handler 不是固定集合）

`小小的身影，重叠的内心`（Steam，Ren'Py 8.6.0 nightly）走了另一条路：它在官方
handler 列表**之外新增了一个格式**。

```python
class RPAEv2ArchiveHandler(object):
    """RPAE-2.0 AES-256-CTR encrypted archives。"""
    def get_supported_headers(): return [b"RPAE-2.0"]
    def read_index(infile):
        import renpy.aescrypt as aescrypt     # 非标准模块
        ...
        index_data = aescrypt.decrypt_block(index_nonce, 0, encrypted_index)
```

两个关键点：

1. **handler 集合不是固定的。** `RPAEv2ArchiveHandler` 只在
   `archive_handlers.append(...)` 里出现，`index_archives` 从不按名字引用它。
   所以"硬编码 RPAv1/v2/v3"的做法对这个格式**完全无感**——而这正是 v2 早期版本的
   真实缺陷（见下）。现在 handler 集合是从 loader 自己的注册调用里读出来的，
   注册顺序也照搬（`index_archives` 按顺序做头部嗅探，顺序决定谁先认领）。
2. **解密实现在原生模块里。** `renpy.aescrypt` 没有任何 `.py`，它被编译进了
   `lib/py3-windows-x86_64/librenpython.dll`（Rust 扩展，导出
   `PyInit_renpy_aescrypt`）。AST 提取**拿不到它**，这是 AST 方案的固有边界：
   纯 Python 逻辑能取源码，编译进去的不行。

这一作的 6 个存档实际都是**未加密的 RPA-3.0**，所以本次不需要 aescrypt。
完整解包 **2003/2003 成功，零失败，459.2 MiB**，且
**声明总量 459.2 MiB == 磁盘上的存档总量 459.2 MiB**。

魔数校验 100% 通过：

| 类型 | 数量 | 魔数 |
| --- | --- | --- |
| OGG | 1502 | `OggS` |
| PNG | 355 | `\x89PNG` |
| WEBP/WAV | 65 | `RIFF` |
| `.rpyc` | 53 | `RENPY RPC2` |
| MP3 | 21 | `ID3` |
| TTF / JPEG / LRC | 7 | 各自魔数 |

（该作还带了一个 macOS `.app` 包和 `lib/py3-linux-*`，都无关。）

### 实测四：标准格式 + 第三方交叉验证（`LOVE YURI / LoveYuri`）

Ren'Py 8.5.0，单个 `game_files.rpa`（427 MB），`loader.py` 与官方在存档部分完全一致。
**1524/1524 成功，零失败，406.9 MiB**，声明总量精确吻合。

这一作的价值在于**可交叉验证**：

1. 存档里同时有 **23 个 `.rpy` 源码和 23 个 `.rpyc`**。解出的 `.rpy` 全部是
   合法 UTF-8 的 Ren'Py 脚本（`define luxi = Character("陆汐")` 之类中文与结构完整），
   偏移错一个字节这类就全废。
2. 我另写了一份**完全独立的标准 RPAv3 实现**（`zlib` + `pickle` + XOR，不碰本工具任何代码），
   逐字节比对本工具的输出：**1524 个文件，0 处不一致，0 个缺失**。

顺带一提，这个游戏的 `game/` 里躺着别人的两份手写解包脚本
（`unpacker_for_LoveYuri.py`、`unpacker_universal.py`），都是**重新实现** RPAv3 解密——
正是"自研解析器"那条路。本次它们和 AST 方案结果一致；但换到前面那两个魔改样本上，
手写实现就得跟着改，而 AST 方案不用。

### 汇总：六个真实样本

| 样本 | Ren'Py | 存档 | 花样 | 结果 |
| --- | --- | --- | --- | --- |
| 黄莓C HuangmeiC Demo | 8.5.3 | 1（索引 21 KB）| 假 offset + 配套 `.zip` + `-33` | **1447/1447**，724 MiB |
| 小小的身影，重叠的内心 | 8.6.0 nightly | 6（459 MB）| 新增 RPAE-2.0 AES handler | **2003/2003**，459.2 MiB |
| LOVE YURI / LoveYuri | 8.5.0 | 1（427 MB）| 标准格式 | **1524/1524**，406.9 MiB |
| Dream_of_BluePlanet | 8.5.2 | 5（1.28 GB）| 存档改名 `.dll` + 魔数 `ILOVEYOU` + 字段换序 | **2971/2971**，1.5 GiB |
| Mainichikisushite | 8.5.2 | **无** | 已解包，散文件 | 正确拒绝，exit 2 |
| Dream_of_BluePlanet\renpy | 8.5.2 | **无** | 同上 | 正确拒绝，exit 2 |

后两个的 `game/` 里没有存档（脚本是散文件，说明游戏已经是解包状态），工具给出可操作的提示并以
exit 2 退出，不写任何东西——**解包器在已解包的游戏上没有副作用**。

### 这一样本暴露的两个真实缺陷（已修 + 已加测试）

**1. `build()` 只返回一份硬编码的名字清单。** 结果是一个被正确提取、正确生成的
handler 在返回时被丢掉——看起来像"这个格式读不了"。修法：凡是被生成的都返回。

**2. handler 发现过宽。** 早期版本接受任何 `.append(名字)`，于是
`file_open_callbacks.append(load_from_filesystem)`、`scandirfiles_callbacks`、
以及循环变量 `stem`/`prefix` 全被当成 handler，把 `loader.py` 大半个辅助图
拽进闭包（甚至产生 `import DownloadNeeded` 这种在游戏外不存在的导入）。
修法：接收者名字里必须含 "handler"，且参数必须指向本模块定义的**类**。

回归测试 `TestHandlerDiscovery` 覆盖这两点，并做过变异检验：
把匹配逻辑改回"松散版"，`test_handler_list_is_not_polluted_by_non_classes` 会失败。

---

## 为什么不再用字符串偏移切割

v1 是这样取代码的：

```python
start1 = content.find('class RPAv3ArchiveHandler(object):')
return_idx = content.find('return index', start1)   # 找第一个 "return index"
code1 = content[start1:line_end+1]
```

按字节偏移量切割源码，问题是它**静默出错**：

| 问题 | 后果 |
| --- | --- |
| `content.find('break', start2)` 找到的 `break` 在函数中间，不在结尾 | 切出来的 `index_archives` 是**半截函数**，只是碰巧能跑 |
| `find('return index')` 找的是字面量 | 换行、注释、变量名一变就切错位置 |
| Ren'Py 各版本 handler 写法不同（`@staticmethod` vs `self`） | 纯文本切割无法适配，也没有任何检查能发现 |
| 只挂了 `RPAv3ArchiveHandler` | RPAv1/RPAv2 存档直接失败（尽管 loader.py 里现成就有） |
| 硬编码 `os.getcwd()` | 只能解包当前目录下的游戏 |

v2 用 `ast` 按**名字**定位定义：

1. `ast.parse()` 解析 `loader.py`；
2. 按名字找到 `RPAv1/v2/v3ArchiveHandler`、`index_archives`、`load_from_archive`，
   **保留原始 AST 节点**（所以装饰器、`self` 风格等原样保留，自动适配不同版本）；
3. 遍历这些定义引用的名字，算出**依赖闭包**（比如 handler 用到 `loads` 就把 `loads` 带上）；
4. 把一小撮无法从源码得到的名字（`RWopsIO`、`loads`、`renpy`）用 runtime shim 补上，
   并按需裁剪；
5. 用 `ast.unparse()` 输出一个自包含模块。

结果是「结构上必然正确」，而不是「碰巧正确」。测试
`test_extracted_definitions_match_loader_exactly` 会把提取结果和原文件逐行比对，
防止将来悄悄退化。

---


---

## 两个需要说明的技术点

### 1. 绕过了 Ren'Py 自身的一个 bug（分段存档）

RPAv3 支持把一个文件拆成两段：开头几个字节存在索引 pickle 里，剩余部分在数据区。
Ren'Py 8.5.2 的 `load_from_archive` **构造好读取器之后把它丢了**：

```python
if start == None or len(start) == 0:
    rv = RWopsIO(afn, "rb", base=offset, length=dlen)
    return io.BufferedReader(rv)
else:
    a = RWopsIO.from_buffer(start, name=name)
    b = RWopsIO(afn, "rb", base=offset, length=dlen)
    rv = RWopsIO.from_split(a, b, name=name)
    rv = io.BufferedReader(rv)      # <-- 没有 return
```

执行会掉出循环并 `return None`，于是一个索引里描述完整、明明存在的文件看起来「不存在」。
（本机两份 `loader.py` 的 MD5 完全一致，`6656A273C06CCFD2F151A09754DE6F36`，确认是上游代码。）

`Reader.load()` 对此做了补偿：`load_from_archive` 返回 `None` 时，
按同一语义重建 `head + tail`。回归测试
`test_split_archive_members_are_extracted_end_to_end` 覆盖这条路径。

### 2. 安全

* **pickle**：RPA 索引用 pickle 存储，属于「来自外部、可被投毒」的数据。
  默认使用受限 `Unpickler`，只允许普通容器类型和 pickle 自身为
  `bytes` 产生的 `_codecs.encode/decode`；其它一律拒绝并提示 `--unsafe-pickle`。
  实测：默认下恶意索引里的 `os.system` 被拒绝（报 `nt.system`），
  加 `--unsafe-pickle` 才会真的执行——这就是默认不放开的理由。
* **路径穿越**：存档条目名经过逐段校验，`../`、绝对路径、盘符、Windows 保留名
  一律拒绝，然后在写入前再做一次规范化路径比对，防止解包变成任意写入。

---

## 上游矩阵：支持哪些 Ren'Py 版本

### 为什么需要这张表

静态后端要**替游戏把 loader 驱动起来**——给它填上存档清单、调它的 `index_archives()`、
再从它的 `archives` 里取结果。所以它必须知道 loader **长什么样**。

这里有个关键区分，之前一直被我们含糊过去：

| | 谁造成的 | 可枚举吗 | 怎么应对 |
| --- | --- | --- | --- |
| **格式**（魔数、扩展名、字段顺序、配套文件、自定义 handler） | **游戏作者**（魔改） | **不可枚举** | 读游戏自己的代码——AST 提取 / `--inject`，**一行都不假设** |
| **结构**（有哪些模块级容器、`index_archives()` 怎么工作、导入什么） | **上游 SDK** | **可枚举** | 读上游已发布版本的源码，列成表 |

**"支持哪些版本"问的是第二类**，而第二类是可枚举的：Ren'Py 开源、版本有限，
把各版本的 `renpy/loader.py` 拉下来逐个比对就行——**不该靠"碰到一个游戏才发现"**。

### 踩过的坑

五个样本（黄莓C / 小小的身影 / LOVE YURI / Dream_of_BluePlanet / Mainichikisushite）
**全是 8.5 系**。于是整整一代（≤8.3）的结构从来没被走过一遍：测试全绿、
五个游戏全通过，而覆盖是假的。直到拿一个 8.3.4 的真实游戏（`skyblue`，
存档伪装成 `.blend`）去跑，三个后端**同时**崩，才挖出五个问题——**全都是第二类**。

### 怎么得到这张表

上游 tag 名形如 `8.3.4.24120703`（版本 + build 日期），用 GitHub API 的
`/releases` 或 `/tags` 列出，再从 `raw.githubusercontent.com/renpy/renpy/<tag>/renpy/loader.py`
取源码。下面是实测结果（`yes` = 有该特征）：

| 版本 | `arc_files` | `old_config_archives` | 注册表是 list | 注册表是对象 | `clear()` | 重新绑定 | 读 `config.archives` | compat 导入 `unicode` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8.5.2.26010301 | yes | – | – | yes | yes | – | yes | – |
| 8.4.1.25072401 | yes | – | – | yes | yes | – | yes | – |
| 8.3.7.25031702 | – | yes | yes | – | – | yes | yes | yes |
| 8.3.4.24120703 | – | yes | yes | – | – | yes | yes | yes |
| 8.3.0.24082114 | – | yes | yes | – | – | yes | yes | yes |
| 8.2.3.24061702 | – | yes | yes | – | – | yes | yes | yes |
| 8.1.3.23091805 | – | yes | yes | – | – | yes | yes | yes |
| 8.0.3.22090809 | – | yes | yes | – | – | yes | yes | yes |
| 7.8.7.25031702 | – | yes | yes | – | – | yes | yes | yes |

### 结论：**两种形状，分界线正好在 8.4**

八列一起跳变，中间没有任何连续变化。7.8.7（Python 2 时代）和 8.0–8.3 是**同一个形状**；
8.4 换了形状，8.5 沿用。所以正确的说法不是"支持 8.3.x"，而是：

> **支持 ≤8.3 和 ≥8.4 这两种 loader 形状。**

### 表与代码的对应

每一列上游事实，在代码里对应哪一处适配——**这就是为什么是这五个补丁**：

| 上游事实 | 代码里的适配 |
| --- | --- |
| `arc_files` 有无 | `Reader.index_archives()`：有就填 `arc_files`；没有就把名字写进 `renpy.config.archives`（`Reader._name_archives_in_config`）。`probe._index()` 同样两种都判 |
| 注册表是 list 还是对象 | `prepare_reader()`：只清**存在**的 `.exts` / `.peek` 缓存 |
| `clear()` 还是重新绑定 | `Reader._reload_rebound_globals()`：索引完从函数自己的 `__globals__` 重新取值（`build()` 返回的是快照，抓不住被重新绑定的名字） |
| compat 导入 `unicode` | `ast_extract`：`from X import Y` 不再被当成 `import Y`；来自 `renpy.*` 的名字走 shim，`_fallbacks` 里有那几个 Python 3 别名的等价实现 |
| `old_config_archives` 的 `global` 读 | `ast_extract` 的 `global_names`：被 `global` 声明的名字，其初始化提**升到生成模块顶层**（`global` 解析的是模块命名空间，看不见 `build()` 的局部变量） |

**全部是按能力探测（`getattr` / `hasattr`），没有一处按版本号分支。** 所以上游再换形状时，
代码不会因为"版本号不认识"而拒绝工作——它只会在真的遇到不认识的形状时报错。

### 这一列的来历值得单独记一笔

`old_config_archives` 就是 [issue #1](https://github.com/TesterNaN/RenPy-RPA-Unpacker/issues/1)
报告的那个变量。当时我查了上游 `master` 和 8.5.2，**都没找到**，于是写下"这大概是游戏作者
自己加的"——**错的**。8.0.3 到 8.3.7 全都有，8.4 才删掉。报告者的游戏在
**8.0.x–8.3.7** 之间，那本来是可以从矩阵直接读出来的答案。

教训：**只查一两个版本就断言"上游没有"，和只有一个世代的样本就断言"支持多版本"，
是同一个错误。**

---

## 已知限制

* **静态后端拿不到编译进原生模块的解密。** `小小的身影，重叠的内心` 的
  `renpy.aescrypt` 是编译进 `librenpython.dll` 的 Rust 扩展，没有源码，
  AST 拿不到。此时该 handler 会被列出、但标注为不可用，
  其余格式（同一安装里的普通 RPA）照常解包：

  ```
  handlers       : RPAEv2ArchiveHandler, RPAv3ArchiveHandler, ...
  ```

  加 `--list` 时会看到未加密存档的正常条目。**真遇到 RPAE 加密存档就用
  `--inject`**：脚本跑在游戏自己的进程里，`renpy.aescrypt` 早就加载好了，
  直接调就行——绕开了"从源码提取"这个前提。`--runtime` 同样可以。

  > **诚实说明**：`renpy.aescrypt` 的可用性我实测过（游戏内 `<module 'renpy.aescrypt' (built-in)>`，
  > 系统 Python 报 `ModuleNotFoundError`），但**我手上没有任何一个真正用了 RPAE 加密的存档**：
  > `小小的身影` 那 6 个存档全是普通 `RPA-3.0`。所以"`--inject` 能解 RPAE"是
  > 从机制上成立的推论（索引和解密函数都在，调的就是游戏自己的 `load_from_archive()`），
  > **而不是端到端跑通过的结论**。有样本欢迎验证。
* **RPAv1（`.rpi`）目前只能列清单**。`RPAv1ArchiveHandler.read_index` 从字节 0
  解压，所以 `.rpi` 是「只有索引」的 zlib 流，成员数据在同名配套文件里；
  我们的索引读取是对的，但成员数据落点没有实测样本，没有冒险实现。真的遇到这类
  游戏时用 `--list` 看清单，并欢迎提供样本。
* **静态后端需要 `loader.py` 源码**（`loader.pyc`-only 的发行版解析不了）。
  三条出路，按推荐顺序：
  1. `--inject`——**实测通过**。同一棵 `loader.pyc`-only 的目录树，静态后端报
     `could not find this game's own renpy/loader.py`，`--inject` 正常解包 1524/1524。
     前提是 `game/` 可写。
  2. `--runtime`——不写任何东西，也不要求脚本能加载（在加载脚本之前就停了），
     代价是每次都要起一个 Ren'Py 进程，慢一个量级。
  3. `--loader` 指向一个**版本匹配**的 Ren'Py SDK 的 `loader.py`。
     注意这会把"游戏自己的 loader 才是权威"这条原则打折扣：如果游戏改过解密，
     拿标准 loader 读出来的结果是**错的**（工具会比对双方声明的 handler/扩展名，
     冲突时拒绝，但改得"看起来一样"的情况它认不出来）。
* **比当前 Python 更新的 `loader.py`**：会先尝试删掉签名注解再解析；
  若仍失败会给出明确提示，换更新的解释器即可。
  （已实测 Ren'Py 8.6.0 nightly 的 loader 在 Python 3.12 下正常。）

---

## 测试套件的一个陷阱：用例「消失」而不是「跳过」

部分用例需要一份真实的 `renpy/loader.py`。Ren'Py 的代码不随本仓库分发，
所以 `tests/test_support.py` 会自己去机器上找（环境变量 → 仓库内 → 常见安装位置）。

**踩过的坑**：`test_ast_extract.py` 原本在**模块级**调用了取 loader 的函数：

```python
SDK_SOURCE_FOR_DISCOVERY = _sdk_source()      # 找不到就 raise unittest.SkipTest
```

在一台没有 Ren'Py 的机器上，这个 `SkipTest` 是在 **import 期间**抛出的，于是
`unittest` 把**整个模块**标记为跳过——这个文件里大约 46 个**根本不需要 loader**
的用例（AST 提取、路径安全、pickle 限制……）全部静默消失。

危险的地方在于**测试依然是"绿"的**：

| | 收集到 | 实际执行 |
| --- | --- | --- |
| 有 Ren'Py | 151 | 151 |
| 没有 Ren'Py（修复前） | **83** | 少的 68 个既没跳过也没报错 |

修复方式是把它改成函数调用，让 `SkipTest` 在**用例内部**抛出，只跳过真正需要的用例。
现在无论有没有 Ren'Py，都是 151 个用例被收集：

| | 收集 | 执行 | 跳过 | 结果 |
| --- | --- | --- | --- | --- |
| 有 Ren'Py | 151 | 151 | 0 | 全部通过 |
| 没有 Ren'Py | 151 | 84 | 67 | 0 错误 0 失败，退出码 0 |

另外两个统计上的坑，量数据时别被绕进去：

* `testsRun` 统计的是**已开始**的用例（含跳过的），所以「执行 + 跳过 ≠ 收集」。
  没有 Ren'Py 的那一列里，`testsRun` 是 116 而不是 151。
* 由 `setUpClass` 抛出的跳过会把**整类折叠成一条记录**（35 个用例只算 1 条），
  所以「跳过 32」和「35 个没开始」是两件事。
* 想对比两种环境，必须在**各自的子进程**里跑：测试模块只 import 一次，
  而 `@unittest.skipUnless(real_loaders(), ...)` 是**定义时**求值的，
  同一个进程里换条件会继承上一次的结论，反而报出假的 `setUpClass` 错误。
