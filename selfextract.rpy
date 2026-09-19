# ============================================================================
#  Ren'Py 自解包脚本 —— 放进 <游戏根>/game/ 目录，启动游戏即自动执行
# ============================================================================
#
#  原理
#  ----
#  Ren'Py 在 renpy/main.py:367 调 renpy.loader.index_files()，在 :412 调
#  load_script()。`init python early:` 的代码块在 load_script() 里执行，所以
#  轮到它的时候，loader 的索引**已经建好**了：
#
#      renpy.loader.archives     [(存档路径, {名字: [(偏移, 长度), ...]}), ...]
#      renpy.loader.game_files   [(目录, 文件名), ...]
#
#  脚本直接读这些现成结构，调 loader 自己的 load_from_archive() 取数据，
#  然后 os._exit(0) 在游戏开始播之前退出（不开窗口）。
#
#  用法
#  ----
#  1. 把本文件复制到  <游戏根>/game/  下（文件名随意，建议 zz_unpack.rpy）
#  2. 用游戏自带的解释器启动，例如：
#         <游戏根>\lib\py3-windows-x86_64\python.exe <游戏根>\<启动器>.py
#     或者直接双击游戏 exe（那样会开窗口，但脚本会在窗口出现前退出）
#  3. 结果看  <游戏根>/_unpack_out/_unpack.log   和  _unpack_out/ 下的文件
#
#  改完记得删掉本 .rpy 和它生成的 .rpyc
# ============================================================================

init python early:

    import os
    import sys
    import traceback

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------
    # 输出到「游戏根」下，不放 game/ 里面 —— 避免污染 Ren'Py 扫描的目录
    _UNPACK_OUT = os.path.join(os.path.dirname(renpy.config.gamedir), "_unpack_out")
    _UNPACK_ONLY = None      # None = 全部；也可以写 ['.rpy', '.png'] 之类只看这些
    _UNPACK_LOG = os.path.join(_UNPACK_OUT, "_unpack.log")

    def _ulog(msg):
        try:
            with open(_UNPACK_LOG, "a", encoding="utf-8") as f:
                f.write(str(msg) + "\n")
        except Exception:
            pass

    def _unpack_main():
        import renpy.loader as loader

        os.makedirs(_UNPACK_OUT, exist_ok=True)
        _ulog("=" * 60)
        _ulog("Ren'Py 自解包开始")
        _ulog("python      : %s" % sys.version.split()[0])
        _ulog("basedir     : %s" % renpy.config.basedir)
        _ulog("gamedir     : %s" % renpy.config.gamedir)
        _ulog("输出目录    : %s" % _UNPACK_OUT)

        # 索引此刻已经建好（main.py 在调 load_script 之前就 index_files 了）
        _ulog("arc_files   : %d" % len(loader.arc_files))
        _ulog("archives    : %d" % len(loader.archives))
        _ulog("game_files  : %d" % len(loader.game_files))

        # 万一某版本到这一步还没索引，自己补一次（幂等）
        if not loader.archives and loader.arc_files:
            _ulog("索引为空，自行调用 index_archives()")
            loader.index_archives()
            _ulog("archives    : %d" % len(loader.archives))

        # 原生解密模块是否可用（例如 RPAE/AES 存档需要它）
        try:
            import renpy.aescrypt as _aes
            _ulog("aescrypt    : %s" % [n for n in dir(_aes) if not n.startswith("_")])
        except Exception as e:
            _ulog("aescrypt    : 不可用 (%s)" % type(e).__name__)

        # ---------------- 收集条目 ----------------
        names = []
        seen = set()
        for _afn, index in loader.archives:
            for name in index:
                if isinstance(name, bytes):
                    name = name.decode("utf-8", "surrogateescape")
                if not name or name in seen:
                    continue
                seen.add(name)
                names.append(name)
        _ulog("存档条目    : %d" % len(names))

        if _UNPACK_ONLY:
            want = tuple(_UNPACK_ONLY)
            names = [n for n in names if n.lower().endswith(want)]
            _ulog("过滤后      : %d" % len(names))

        # ---------------- 导出 ----------------
        ok = 0
        failed = 0
        total = 0
        for i, name in enumerate(names, 1):
            try:
                h = loader.load_from_archive(name)
                if h is None:
                    _ulog("取不到      : %s" % name)
                    failed += 1
                    continue
                try:
                    data = h.read()
                finally:
                    try:
                        h.close()
                    except Exception:
                        pass

                target = os.path.join(_UNPACK_OUT, name.replace("/", os.sep))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as out:
                    out.write(data)
                ok += 1
                total += len(data)

                if i % 200 == 0:
                    _ulog("进度        : %d/%d" % (i, len(names)))
            except Exception as e:
                failed += 1
                _ulog("出错        : %s -> %s: %s" % (name, type(e).__name__, e))

        _ulog("-" * 60)
        _ulog("完成        : 成功 %d, 失败 %d, 共 %.1f MiB" % (ok, failed, total / 1048576.0))
        _ulog("输出        : %s" % _UNPACK_OUT)

    # ------------------------------------------------------------------
    # 执行；出任何问题都写日志，不让游戏崩
    # ------------------------------------------------------------------
    try:
        _unpack_main()
    except Exception:
        _ulog("未捕获异常:\n" + traceback.format_exc())

    _ulog("调用 os._exit(0)：停在游戏开始之前，不开窗口")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
