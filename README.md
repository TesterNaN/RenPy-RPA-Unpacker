# RenPy-RPA-Auto-Unpacker 🚀

> 不猜加密、不试密钥、不自己解析格式 —— **直接借用游戏自己的解包代码**

## 📋 项目简介

**RenPy-RPA-Auto-Unpacker** 是一个 Ren'Py 游戏 `.rpa` 存档解包工具。

它的最大特色是：**不重新实现 RPA 格式，而是把游戏自己的 `renpy/loader.py` 借来用。**

为什么这件事值得炫耀一下——因为你要是自己写解析器，就得自己面对这些东西：

| 真实游戏里遇到的 | 自己写解析器的下场 |
| --- | --- |
| 索引字段被 XOR 混淆（`index[k] = [(offset ^ key, dlen ^ key)]`） | 读出垃圾偏移，报"文件损坏" |
| 数据偏移整体减 33，取值前从不赋值 `offset`（故意的，让 XOR 自反） | 完全看不懂，只能猜 |
| 存档改名成 `vcruntime140.dll`，魔数换成 `ILOVEYOU` | 一个存档都找不到 |
| 一个文件拆成两段（头在索引里，尾在数据区） | 解出半个文件 |
| 索引是 Python 2 的 pickle | `UnicodeDecodeError` |
| 解密编译进 `librenpython.dll`，磁盘上没有 `.py` | 拿不到源码，直接卡死 |

**前五种我都在真实游戏上遇到过、并且完整解包成功了**（样本和逐字节校验结果在
[docs/NOTES.md](docs/NOTES.md)）。最后一种（原生解密）**机制已经打通，但只验证到
"模块可用"** —— 我手上没有真正加密的存档，这一条是推论不是结论，下面「注意事项」里也标了。

靠的不是我聪明，而是这些细节**写这个游戏的人早就处理过了**——我只要把那段代码拿来跑就行。

---

## ✨ 核心特色

### 🎯 最牛逼的点：用游戏自己的解密，而不是"猜"解密

很多解包器的思路是"识别加密算法 → 推导密钥 → 自己解密"。这条路注定要不停追新：
作者换个 XOR 常量、换个字段顺序、加个自定义 handler，工具就得更新一版。

本工具**换了个思路**：

- **不推导密钥** —— 解密函数是游戏自己带来的，密钥怎么来的也归它管
- **不识别算法** —— 加载器声明了什么格式，就按什么格式读
- **不需要配置** —— 没有"请选择加密方式"这一步，因为它压根不关心
- **格式自动跟进** —— 游戏换版本、作者换魔改方式，**工具代码一行都不用改**

这不是"更聪明的猜测"，是**从根上不猜**。

### 🚀 三个后端，一个原则

| 后端 | 一句话 | 适合什么时候 |
| --- | --- | --- |
| **静态 AST**（默认） | 把游戏 `loader.py` 里的解析代码**提取出来**，在自己进程里跑 | 一般情况。最快、不启动游戏、不写游戏目录 |
| **`--runtime`** | 用**游戏自带的 python** 起一次运行时，从外面调它自己的 `loader` | 静态后端读不出来，又不想写游戏目录 |
| **`--inject`** | 往 `game/` 放一个脚本，**让游戏自己解包**然后自动删掉 | 静态后端读不出来，且 `game/` 可写。能力最强 |

**三个后端的共同点，也是整个工具唯一的铁律：以游戏自己的 loader 为准。**
所以"魔改游戏"不需要任何特殊处理——工具从来没打算比游戏更懂它自己的存档。

### 💪 其他实在的功能

- **🖥️ 只读安装也能用** —— `--runtime` 不往游戏目录写任何东西，全程结果走管道
- **🔒 默认安全** —— 索引 pickle 默认用受限反序列化，恶意索引里的 `os.system` 会被拒绝
- **🛡️ 路径穿越防护** —— `../`、绝对路径、盘符、Windows 保留名一律拒绝
- **📦 无第三方依赖** —— 纯标准库，Python 3.9+，下载下来就能跑
- **⚡ 快** —— 1524 条目的存档 `--list` 约 **0.11 秒**（含解释器启动，实测三次
  108/110/118 ms）；同一个游戏 406.9 MB 全量解包约 **1 秒**
- **🧹 干净** —— `--inject` 跑完连 `.rpyc` 一起删，不留任何痕迹
- **🖥️ 纯标准库、不依赖平台特有接口**（Windows 上实测；Linux/macOS 的解释器探测路径
  代码里也处理了，但没实测过）

---

## 📦 快速使用指南

### 环境要求

**Python 3.9 或更高版本**（无第三方依赖，只用标准库）

### 安装

```bash
# 方式一：装成命令（推荐）
pipx install git+https://github.com/TesterNaN/RenPy-RPA-Unpacker

# 方式二：直接用，不安装
git clone https://github.com/TesterNaN/RenPy-RPA-Unpacker
cd RenPy-RPA-Unpacker
python unpacker.py --help
```

### 使用方法

```bash
# 1️⃣ 先看看存档里有什么（不写盘，最快）
renpy-unpack --game "D:\Games\SomeGame" --list

# 2️⃣ 一键解包（默认输出到 <游戏根>/extracted_files）
renpy-unpack --game "D:\Games\SomeGame" -j 16
```

**`--game` 指向游戏根目录**（含 `renpy/` 的那一层）。不带 `--game` 时，
工具会从当前目录**自动向上查找**——所以直接把 `unpacker.py` 丢进游戏目录双击也能跑。

### 三种等价写法

```bash
renpy-unpack --game "D:\Games\SomeGame" --list          # 装好后
python -m renpy_unpack --game "D:\Games\SomeGame" --list
python unpacker.py --game "D:\Games\SomeGame" --list
```

### 输出结果

解包后的文件保存在 `extracted_files/`，**完整保留原始目录结构**
（默认落在游戏根目录下，而不是 `game/` 里面——因为 Ren'Py 会递归扫描 `game/`，
把脚本解包进去会让它重复定义标签、直接启动失败）。

### 静态后端读不出来的时候

先用 `--runtime --list` 看一眼，再决定走哪条：

```bash
# 探一下这个游戏的原生解密（renpy.aescrypt / renpy.encryption）能不能用
renpy-unpack --game "D:\Games\SomeGame" --runtime --list

# 让游戏自己解包（能力最强，需要 game/ 可写）
renpy-unpack --game "D:\Games\SomeGame" --inject

# 用游戏自己的运行时读（不写游戏目录，但慢一些）
renpy-unpack --game "D:\Games\SomeGame" --runtime -o unpacked
```

**怎么选后端**（默认静态 AST，够用就别换）：

| 情况 | 用哪个 |
| --- | --- |
| 一般游戏、游戏自带 `loader.py` | 默认（不加参数） |
| 解密在原生模块里 / 只有 `loader.pyc`，且 `game/` 可写 | `--inject` |
| 同上，但 `game/` 写不进去（只读安装） | `--runtime` |
| 游戏脚本加载不了（坏 mod、标签重复定义） | `--runtime` |
| 只想先看看有没有原生解密 | `--runtime --list` |

> `--inject` 和 `--runtime` **互斥**，同时给会直接报错让你选一个。
> `--inject` 也不能和 `--list` / `--dry-run` 同用——条目清单只存在于游戏进程内部。

---

## 🔧 技术原理

### 三个后端分别在干什么

它们**机制完全不同、没有任何共享的读取器代码**，只是终点是同一个 `load_from_archive()`：

| | 谁执行 loader 代码 | 谁提供运行上下文 | 函数从哪来 |
| --- | --- | --- | --- |
| **静态 AST** | **我们的**解释器 | 我们的替身（`renpy`/`RWopsIO`/`loads`） | 源码 → AST → 重新生成 → 执行 |
| **`--runtime`** | 游戏解释器（子进程） | 游戏自己 | `import` 到的**活对象** |
| **`--inject`** | 游戏解释器（游戏本体） | 游戏自己 | `import` 到的**活对象** |

一句话概括：**静态 AST 是"抄一份带回家"，`--runtime` 是"打个电话问"，`--inject` 是"让它在自己家里干"。**

### 静态后端：AST 提取而不是字符串切割

早期版本用 `str.find()` 按偏移切函数，结果切出过半截函数
（`content.find('break', start2)` 匹配到了函数体中间的 `break`）。现在：

1. `ast.parse()` 解析 `loader.py`
2. 按**名字**找到 `RPAv1/v2/v3ArchiveHandler`、`index_archives`、`load_from_archive` 等，
   **保留原始 AST 节点**（装饰器、`self` 风格原样保留，自动适配不同 Ren'Py 版本）
3. 遍历这些定义引用的名字，算出**依赖闭包**
4. 把无法从源码得到的少数名字（`RWopsIO`、`loads`、`renpy`）用替身补上，并按需裁剪
5. `ast.unparse()` 输出一个自包含模块

结果是「结构上必然正确」，而不是「碰巧正确」。
测试 `test_extracted_definitions_match_loader_exactly` 会把提取结果和原文件**逐个节点比对**，
防止将来悄悄退化。

### `--runtime`：在游戏脚本加载之前抢回控制权

```
renpy.bootstrap.bootstrap()      ← 探针从这里进去
  └ renpy.import_all()           ← renpy.loader 在这里进 sys.modules  ← 探针在这一刻停下
  └ renpy.main.main()            ← 真正的脚本加载在这里
```

探针用 `sys.settrace` 盯着 `renpy.loader` 进入 `sys.modules` 的那一瞬间，立刻把控制权抢回来。
**实测证据**：LoveYuri 自己都启动不了（`game/extracted/` 和 `game/rpy/` 重复定义 `gui.rpy`），
但 `--runtime` 对它 **exit=0、1524 条目**。游戏能不能玩，完全不影响读它的存档。

### `--inject`：让游戏自己跑

```
renpy.loader.index_files()          main.py:367   ← 索引已建好
renpy.game.script.load_script()     main.py:412   ← init python early 在这里执行
renpy.display.core.Interface()      main.py:572   ← 到这一步才会开窗口
```

脚本在第二行和第三行**之间**就 `os._exit(0)` 退出了，所以**窗口根本不会被创建**。
它只做三件事：遍历现成的索引、调 `load_from_archive()`、写盘——**完全不解析存档格式**。

### 顺带修了 Ren'Py 自己的一个 bug 🐛

RPAv3 支持把一个文件拆成两段，而 Ren'Py 8.5.2 的 `load_from_archive`
**构造好读取器之后把它丢了**：

```python
rv = RWopsIO.from_split(a, b, name=name)
rv = io.BufferedReader(rv)      # <-- 没有 return
```

于是掉出循环 `return None`，一个索引里写得清清楚楚、明明存在的文件看起来"不存在"。
本工具的 `Reader.load()` 补上了这一步。

---

## ⚠️ 重要说明

### 合法使用

本工具仅用于**合法的技术研究、学习交流和个人存档备份**。
请遵守你所在地区的法律法规以及游戏本身的用户协议，不要用它分发你没有权利分发的资源。

### 注意事项

1. 解包前建议备份游戏目录
2. 已经解包完的游戏（资源是散装文件）没有东西可解——加了 `--include-loose` 才会导出散文件
3. `--inject` 会**真的启动一次游戏进程**（在开窗口之前就退出），杀毒软件/Steam 会看到它
4. **原生加密存档（RPAE/AES）我没有实测样本**：机制上 `--inject` / `--runtime` 成立，
   但我手上的游戏全是普通 `RPA-3.0`，所以这一条是推论而非结论。有样本欢迎验证
5. **完整的边界实测和已知限制**（含 RPAv1、`loader.pyc`-only、只读安装等每一种情况的
   实测结论）在 [docs/NOTES.md 的「已知限制」](docs/NOTES.md#已知限制)

---

## ❓ 常见问题

### Q: 为什么不用自己写的解密？为什么非要读游戏的文件？

A: 因为**格式的权威是游戏自己**。同一个 `.rpa` 扩展名，官方 SDK 认为是
`RPA-3.0 ` 魔数，而某个真实游戏改成了 `.dll` + `ILOVEYOU`。
拿标准实现去读，报出来的会是"找不到存档"——**而 1.5 GB 的存档就躺在那里**
（实测：那个游戏 `game/` 里的五个"系统 DLL"分别是 558.7 / 434.0 / 369.1 / 215.3 / 1.5 MB）。
自己写一套解密，就是在和游戏的作者玩一场永远追不上的追逐战。

### Q: 工具需要放在游戏根目录吗？

A: 不需要。用 `--game` 指定即可；不带 `--game` 时会从当前目录**自动向上查找**。
放在游戏根目录直接跑当然也可以。

### Q: 提示"没有找到任何存档"

A: 依次检查：
- `--game` 指向的是不是游戏**根目录**（含 `renpy/` 的那一层）
- 游戏是不是**已经完全解包**了（资源是散装文件，那就没东西可解）
- 存档的扩展名是不是被改过（比如伪装成 `.dll`）——本工具会自动按 loader 声明的扩展名去找
- 加 `--runtime --list` 看看游戏自己的运行时能认出几个存档

### Q: 解包失败怎么办？

A: 按这个顺序试：

| 报的错 | 含义 | 换哪个 |
| --- | --- | --- |
| `could not find this game's own renpy/loader.py` | 游戏只发了 `loader.pyc` | `--inject` 或 `--runtime` |
| `cannot write into ...` | `game/` 写不进去 | `--runtime` |
| `could not take control of the game's runtime` | 运行时没能抢到控制权 | `--inject` |
| `the game started but never ran the injected script` | 游戏脚本加载失败（坏 mod？） | `--runtime` |

**三条路机制完全独立，一条失败不代表另一条也失败。**

### Q: 支持哪些 Ren'Py 版本？

A: 支持**按 loader 自己的说法**来读——这比"支持某个版本列表"更靠谱。

- **真实游戏上实测解包成功**：标准 `RPA-3.0`、XOR 混淆索引、偏移整体平移、
  伪装成 `.dll` 的自定义格式与魔数、自定义 handler、只有 `loader.pyc` 的发行版、
  完全没有存档的散装游戏。已实测 Ren'Py **8.5.2** 与 **8.6.0 nightly**。
- **合成存档端到端回归覆盖**：RPAv1 / RPAv2 / 分段存档 / Python 2 pickle。
- **例外**：RPAv1（`.rpi`）目前只能**列清单**，成员数据的落点没有实测样本，
  没有冒险实现——欢迎提供样本。

### Q: 会往我的游戏目录里写东西吗？

A: **默认不会。** 静态后端和 `--runtime` 全程不写游戏目录。
`--inject` 需要往 `game/` 放一个脚本，跑完会**连 `.rpyc` 一起删掉**——
`.rpyc` 留在那里会让游戏每次启动都重跑一遍解包，这是个真实的坑。

### Q: 解包出来的文件放在哪？

A: 默认 `<游戏根>/extracted_files/`，可以用 `-o` 改。
默认**不放在 `game/` 里面**是故意的：Ren'Py 会递归扫描 `game/`，
把 `.rpy` 解包进去会让它重复定义同一个标签，游戏直接启动不了。

---

## 📁 目录结构

```
unpacker.py                  # 直接运行的入口
selfextract.rpy              # 手工版自解包脚本：复制到 game/ 里开一次游戏就行
                             # 不需要本工具、不需要 Python（详见文件头注释）
renpy_unpack/
    core.py                  # 发现游戏、构建读取器、并行提取、CLI、三个后端调度
    ast_extract.py           # 静态后端：AST 提取核心
    runtime.py               # 活运行时后端：探测解释器/启动器、流式协议
    probe.py                 # 活运行时后端：在游戏进程里跑的探针
    inject.py                # 自解包后端：生成脚本、跑一次游戏、读清单、清理
    _inject_body.py          # 注入脚本的正文模板
    runtime_shims.py         # RWopsIO 替身 + 受限 pickle 反序列化
    _fallbacks.py            # loader.py 确实缺少某段时的兜底实现
docs/NOTES.md                # 开发笔记：原理、实测数据、踩过的坑
tests/                       # 151 个用例，无第三方依赖
```

## 🧪 运行测试

```bash
python -m unittest discover -s tests -t tests -v
```

部分用例要在真实的 `renpy/loader.py` 上跑。Ren'Py 的代码不随本仓库分发，
所以测试会自己去找一份（`RENPY_SDK` 环境变量 → 仓库内 → 常见安装位置扫描）。
**找不到时相关用例跳过、不报错**，其余照跑：

| 环境 | 结果 |
| --- | --- |
| 机器上有 Ren'Py | 151 个用例全部执行并通过 |
| 机器上没有 Ren'Py | 退出码 0：84 个执行，67 个跳过 |

---

## 📄 许可证

本项目采用 **GNU General Public License v3.0**（[LICENSE](LICENSE)）。

本仓库**不包含也不需要**任何 Ren'Py 源码：静态后端在运行时读取你机器上游戏自带的
`renpy/loader.py`，提取出的代码不会被打包或分发。Ren'Py 引擎本身由
[Ren'Py 项目](https://www.renpy.org/) 开发，采用其自有许可。

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request！

1. Fork 本项目
2. 创建功能分支 (`git checkout -b feature/新功能`)
3. 提交更改 (`git commit -m '添加新功能'`)
4. 推送到分支 (`git push origin feature/新功能`)
5. 开启一个 Pull Request

> 想改代码的话，**先看 [docs/NOTES.md](docs/NOTES.md)**：
> 每个设计决定背后的实测证据、走过的错路、以及踩过的坑都记在里面。
> 「为什么不用某个方案」和「为什么用这个方案」一样重要。

## 📞 技术支持

1. 先看上面的常见问题，以及 [docs/NOTES.md](docs/NOTES.md) 的「已知限制」
2. 提 Issue 时请附上：完整报错、`--list` 输出、游戏用的是哪个 Ren'Py 版本
3. 如果游戏是魔改的，`renpy/loader.py` 里那几行关键的改动会非常有帮助

---

**免责声明**：本工具仅供技术学习和研究使用。使用本工具产生的任何后果由使用者自行承担。请遵守相关法律法规。

> 如果这个工具对你有帮助，请给它点个星标 ⭐，这将是对开发者的最大鼓励！
