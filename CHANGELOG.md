# 更新日志

版本号以 `pyproject.toml` 里的 `version` 为准，对应的 git tag 是 `v<版本号>`。

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
