"""Classification tests: the diagnosis must be right without any game installed.

These build tiny fake trees.  The point is that a build which deleted ``renpy/`` and
renamed its archives is still recognised as Ren'Py, and that each backend gets a reason
instead of a bare "no".
"""

from __future__ import annotations

import pickle
import tempfile
import unittest
import zlib
from pathlib import Path

from renpy_unpack.classify import classify, format_profile


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _source_build(root: Path) -> Path:
    _write(root / "renpy" / "loader.py", b"# loader\n")
    _write(root / "game" / "script.rpyc", b"RENPY RPC2")
    _write(root / "lib" / "py3-windows-x86_64" / "libpython3.12.dll", b"MZ")
    _write(root / "lib" / "py3-windows-x86_64" / "librenpython.dll", b"MZ")
    _write(root / "Game.py", b"import renpy\n")
    _write(root / "Game.exe", b"MZ")
    return root


def _compiled_build(root: Path) -> Path:
    _write(root / "renpy.pyd", b"MZ" + b"\0" * 1024)
    _write(root / "game" / "script.rpyc", b"RENPY RPC2")
    _write(root / "lib" / "py3-windows-x86_64" / "libpython3.12.dll", b"MZ")
    _write(root / "lib" / "py3-windows-x86_64" / "librenpython.dll", b"MZ")
    _write(root / "Game.exe", b"MZ")
    return root


def _rpa3(body: bytes = b"payload") -> bytes:
    """A minimal RPA-3.0 archive: 34-byte header, zlib+pickle index, raw members.

    ``RPA-3.0 `` (8) + offset (16 hex) + space + key (8 hex) + newline = 34 bytes, which
    is what Ren'Py's own reader slices: ``l[8:24]`` for the offset, ``l[25:33]`` for the
    key.
    """
    offset = 34
    index = {"a.png": [(offset, len(body))]}
    blob = zlib.compress(pickle.dumps(index, 2))
    return b"RPA-3.0 %016x %08x\n" % (offset, 0) + blob + body


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_source_build_is_recognised(self) -> None:
        profile = classify(_source_build(self.root / "game1"))
        self.assertEqual(profile.engine, "source")
        self.assertEqual(profile.backend("static").applicable, "yes")
        self.assertEqual(profile.backend("runtime").applicable, "yes")
        self.assertEqual(profile.backend("hook").applicable, "optional")

    def test_compiled_build_reports_hook(self) -> None:
        profile = classify(_compiled_build(self.root / "game2"))
        self.assertEqual(profile.engine, "compiled-pyd")
        self.assertEqual(profile.backend("static").applicable, "no")
        self.assertEqual(profile.backend("runtime").applicable, "no")
        self.assertEqual(profile.backend("hook").applicable, "yes")
        # the reason has to name the build, not just refuse
        self.assertIn("compiled", profile.backend("static").reason)

    def test_missing_renpy_py_disqualifies_env(self) -> None:
        source = classify(_source_build(self.root / "game3"))
        self.assertEqual(source.backend("env").applicable, "no")
        self.assertIn("renpy.py", source.backend("env").reason)

        with_renpy = _source_build(self.root / "game4")
        _write(with_renpy / "renpy.py", b"# launcher\n")
        self.assertEqual(classify(with_renpy).backend("env").applicable, "yes")

    def test_nested_single_folder_is_descended(self) -> None:
        outer = self.root / "Library Entry"
        inner = _source_build(outer / "RealGame")
        profile = classify(outer)
        self.assertEqual(profile.root, inner.resolve())

    def test_renamed_archive_is_counted_by_extension(self) -> None:
        root = _source_build(self.root / "game5")
        _write(root / "game" / "assets.blend", b"WJZ-4.9 " + b"\0" * 8192)
        profile = classify(root)
        self.assertTrue(any(p.suffix == ".blend" for p in profile.archive_candidates))
        self.assertTrue(any("renamed" in note or "custom format" in note
                            for note in profile.notes))

    def test_stock_archive_is_recognised_by_content(self) -> None:
        root = _source_build(self.root / "game6")
        # content sniffing ignores files below its 512-byte floor, so give it a real one
        _write(root / "game" / "archive.rpa", _rpa3(b"payload" * 1024))
        profile = classify(root)
        self.assertTrue(profile.archives, "content sniffing should find a real RPA-3.0 file")

    def test_format_profile_mentions_every_backend(self) -> None:
        text = format_profile(classify(_compiled_build(self.root / "game7")))
        for name in ("static", "runtime", "inject", "env", "hook"):
            self.assertIn(name, text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
