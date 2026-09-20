# 更新日志

版本号以 `pyproject.toml` 里的 `version` 为准，对应的 git tag 是 `v<版本号>`。

## 2.0.2

**支持 8.4 之前与之后两种 loader 形状。**

上游已发布版本的 `renpy/loader.py` 实测下来**只有两种形状，分界线正好在 8.4**
（8.0–8.3 与 7.8 是同一种；8.4 换了形状，8.5 沿用）。此前代码只支持 ≥8.4 那一种，
而五个样本游戏**全是 8.5 系**，所以整整一代从没被走过——测试全绿，覆盖是假的。

在游戏 `skyblue`（Ren'Py 8.3.4，存档伪装成 `.blend`、魔数 `WJZ-4.9 `、
密钥 `0x42424242`）上实测时三个后端**同时崩**，挖出五个问题，**全部属于上游结构差异，
没有一个是游戏作者的改动**（作者只改了格式层）。逐列依据见
[docs/NOTES.md 的「上游矩阵」](docs/NOTES.md#上游矩阵支持哪些-renpy-版本)。

> 全部按能力探测（`getattr` / `hasattr`）实现，**没有一处按版本号分支**——上游再换形状时，
> 代码不会因为"版本号不认识"就拒绝工作。

### 修复

- **`from renpy.compat import unicode` 被输出成 `import unicode`**。被导入的是**名字**
  而不是模块，生成的读取器一执行就 `ModuleNotFoundError`。现在 `from X import Y`
  照原样输出；来自游戏自己包的名字改用 shim（那个包在这里不可导入），Python 3
  兼容别名（`unicode`、`basestring`、`pystr`、`PY2`、`bchr`、`bord`、`tobytes`）
  进了兜底表。
- **`archive_handlers.exts`/`.peek` 被无条件清空**。8.3.x 的注册表是个**普通 list**，
  于是直接 `AttributeError`；现在只在缓存存在时才清。
- **8.3.x 没有 `arc_files`**。读取器改为把存档名写进 `renpy.config.archives`——
  那才是这个版本的 `index_archives()` 会去遍历的东西。
- **`build()` 返回的是字典，字典的项是"返回那一刻的快照"**。8.3 的 `index_archives()`
  写的是 `global archives; archives = []`（**重新绑定**）而不是 `archives.clear()`，
  所以被填充的列表根本不是读取器持有的那个——每个游戏都报"0 条目"。现在索引完会从
  该函数自己的 `__globals__` 重新取值。8.4+ 用的是 `clear()`，所以这个 bug 一直没露头。
- **`--runtime` 探针**有同一假设的两处：一句诊断用的 `len(loader.arc_files)` 直接
  中断整个运行；而 8.3.x 的索引路径需要先设好 `basedir`/`searchpath`，否则 `transfn`
  解析不了，`index_archives()` 里那句 `except Exception: continue` 会**静默地什么都
  不索引**。

### 顺带修掉的两个

- **`--write-core` 只在读取器建好之后才写文件**。于是当"生成的模块本身出错"时，
  错误信息里那句"加 `--write-core` 看看生成的模块"**根本执行不了**——重跑会在同一个
  地方失败，文件永远不会产生。现在它在编译/执行**之前**就写。
- 准备读取器时的意外异常会打出**裸 traceback**，看起来像用户机器崩了。现在报成
  内部错误，并提示附上游戏的 Ren'Py 版本。

### 验证

`skyblue` 解出 **1194 个文件**，`--inject` 与静态后端**逐字节完全一致**；另外四个
样本游戏的生成模块**逐字节不变**、解包结果不变；156 个测试通过，且每个新测试都做了
变异验证（把对应修复改回去，测试立刻失败）。

## 2.0.1

### 修复

- **被 `global` 声明的名字被输出到了错误的作用域**（[#1](https://github.com/TesterNaN/RenPy-RPA-Unpacker/issues/1)）。

  生成模块把所有定义都放进 `build()` 函数体里，而 `global X` 解析的是**模块**命名空间。
  于是当 loader 里某个函数先读后写某个全局名时，它的模块级初始化语句虽然被正确提取了，
  却被输出成 `build()` 的局部变量——函数第一次读它照样 `NameError`。这类名字现在被提升到
  生成模块的顶层，语义与 Ren'Py 一致（第一次调用重新索引，第二次短路返回）。

  症状和 #1 报告的老版本 bug 一模一样，但根因不同：老版本是手工切代码片段时漏掉了初始化
  语句，新版本是把它输出深了一层。报告者的 loader 里 `index_archives()` 本身就用这个形状，
  所以必炸；本机 5 个真实游戏的 loader 里用 `global` 的四个函数
  （`auto_init`、`auto_quit`、`auto_thread_function`、`check_autoreload`）都在提取闭包之外，
  因此一直没被触发。

## 2.0.0

**v2 是一次重写，不是修补：解包用的代码换了来源。**

v1 用字符串偏移到 `renpy/loader.py` 里把函数切出来，切出过半截函数
（`content.find('break', start2)` 匹配到了函数体中间的 `break`）。v2 改成按**名字**
做 AST 提取，多出来的部分不是顺便加的，是同一件事的两面。

### 新增

- **静态 AST 后端（默认）**：`ast.parse()` 找到 `RPAv1/2/3ArchiveHandler`、
  `index_archives`、`load_from_archive` 等定义，保留原始 AST 节点并算出依赖闭包，
  再用 `ast.unparse()` 输出自包含模块。不同 Ren'Py 版本的写法差异（装饰器、
  `self` 风格）自动适配，不再需要硬编码的名称列表。
- **`--runtime` 后端**：用**游戏自带的解释器**起一次 Ren'Py，探针通过 `-c` 传入，
  用 `sys.settrace` 在 `renpy.loader` 进入 `sys.modules` 的瞬间抢回控制权。
  在**脚本加载之前**就停，所以游戏自己启动不了也能读它的存档；全程不往游戏目录写东西。
- **`--inject` 后端**：往 `game/` 放一个生成的 `init python early:` 脚本，让游戏自己
  解包，跑完连 `.rpyc` 一起删掉。两个后端都读不到的发行版（只发 `loader.pyc`、
  解密编译进原生模块）它能读。
- **散装文件**：`--include-loose`，通过 `loader.walkdir()` 枚举，跟随 Ren'Py 自己的
  索引规则（排除 `cache/`、`saves/`）。
- `--skip-existing`、`--match` / `--ext` 过滤、`-j` 并行、`--write-core`。
- 受限 `pickle` 反序列化（默认拒绝索引里的任意可调用对象，`--unsafe-pickle` 才放开）
  和逐段路径校验（拒绝 `../`、绝对路径、盘符、Windows 保留名）。

### 修复

- 绕过了上游 Ren'Py 的一个 bug：`load_from_archive` 为分段条目构造好读取器之后
  没有 `return`（`renpy/loader.py:600`），掉出循环返回 `None`，于是索引里描述完整的
  文件看起来"不存在"。`Reader.load()` 按同一语义补上了这一步。
- `--runtime` 不再往游戏安装目录写 `renpy/__pycache__/`（探针第一条语句设
  `sys.dont_write_bytecode`，并给子进程设 `PYTHONDONTWRITEBYTECODE`）。
- `--inject` 与 `--runtime` 同时给出时，从抛 traceback 改成一条明确的错误信息。
- 默认输出目录从结构上派生（`_safe_output_root`），使得 `--game <root>/game`
  也不可能把文件解到 Ren'Py 会扫描的目录里。

### 说明

- v1 时期的单文件通用解包器和三个游戏专用脚本保存在 **`legacy` 分支**，
  没有删除。那三个游戏专用脚本是当年手工逆出各游戏改动的原始记录。
- **原生加密存档（RPAE/AES）尚未端到端验证**：机制上 `--inject` / `--runtime`
  成立（解密模块就在游戏自带的解释器里），但手上没有真正加密的存档样本，
  只验证到"模块可用"为止。
