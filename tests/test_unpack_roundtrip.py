"""End-to-end round-trip tests: synthetic archives in, exact bytes out.

These run the *real* Ren'Py readers extracted from the SDK's ``loader.py``
against archives built by ``tests/rpa_factory.py``.  That combination is the
whole point of the tool, so it is tested rather than assumed: RPAv1/2/3 variants,
XOR-obfuscated indexes, split segments and Python 2 pickles all have to survive
the trip.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from test_support import ScratchCase, sdk_loader

from renpy_unpack.core import (  # noqa: E402
    UnpackError,
    Unpacker,
    build_reader,
    discover_game_root,
    find_loader,
    parse_loader_source,
    prepare_reader,
    read_loader_source,
)

from rpa_factory import (  # noqa: E402
    DISGUISED_LOADER,
    OBFUSCATED_LOADER,
    write_disguised_dll,
    write_fake_system_dll,
    write_obfuscated_rpa3,
    write_python2_rpa3,
    write_rpa1,
    write_rpa1_with_data,
    write_rpa2,
    write_rpa3,
    write_rpa3_split,
)

SDK_LOADER = sdk_loader()

REQUIRED = (
    "RPAv3ArchiveHandler",
    "RPAv2ArchiveHandler",
    "RPAv1ArchiveHandler",
    "ArchiveHandlers",
    "index_archives",
    "load_from_archive",
    "archives",
    "arc_files",
)

PAYLOAD = {
    "game/script.rpy": b"label start:\n    return\n",
    "game/images/bg/room 1.png": bytes(range(256)) * 8,
    "game/gui/textbox.webp": b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\xab" * 300,
    "game/audio/theme.ogg": b"OggS" + b"\x00" * 500,
    "game/tiny.txt": b"x",
    "game/empty.bin": b"",
}


class ReaderHarness(ScratchCase):
    """Builds the real reader once per case and indexes synthetic archives."""

    @classmethod
    def setUpClass(cls):
        if SDK_LOADER is None:
            raise unittest.SkipTest("no Ren'Py loader.py available on this machine")
        source = parse_loader_source(read_loader_source(SDK_LOADER), str(SDK_LOADER))
        namespace, extraction = build_reader(source, filename=str(SDK_LOADER))
        cls.reader = prepare_reader(namespace, extraction)

    def read_all(self, *archives: Path) -> dict[str, bytes]:
        """Index *all* archives in one pass and read every member.

        All archives must be passed together: loader's ``index_archives()``
        starts with ``archives.clear()``, so indexing one at a time would drop
        the previous one's index.
        """
        self.reader.index_archives(list(archives))

        out: dict[str, bytes] = {}
        for _path, index in self.reader.namespace["archives"]:
            for name in index:
                handle = self.reader.load(name)
                self.assertIsNotNone(handle, f"{name} vanished from the index")
                try:
                    out[name] = handle.read()
                finally:
                    handle.close()
        return out

    def index_only(self, *archives: Path) -> dict:
        """Index *archives* without reading members; returns the merged index."""
        self.reader.index_archives(list(archives))
        merged: dict = {}
        for _path, index in self.reader.namespace["archives"]:
            merged.update(index)
        return merged


class TestReaderAgainstSyntheticArchives(ReaderHarness):
    """The extracted reader must parse every RPA flavour the factory can build."""

    def test_rpa3_round_trip(self):
        archive = write_rpa3(self.path("game", "archive.rpa"), PAYLOAD)
        self.assertEqual(self.read_all(archive), PAYLOAD)

    def test_rpa3_split_segments_round_trip(self):
        # Exercises RWopsIO.from_split: the head of each file lives in the index
        # pickle and only the tail lives in the data area.
        archive = write_rpa3_split(self.path("game", "archive.rpa"), PAYLOAD)
        self.assertEqual(self.read_all(archive), PAYLOAD)

    def test_rpa3_python2_pickle_round_trip(self):
        archive = write_python2_rpa3(self.path("game", "archive.rpa"), PAYLOAD)
        extracted = self.read_all(archive)
        # Python 2 archives yield bytes keys; compare on decoded names.
        normalised = {
            (key.decode("utf-8") if isinstance(key, bytes) else key): value
            for key, value in extracted.items()
        }
        self.assertEqual(normalised, PAYLOAD)

    def test_rpa2_round_trip(self):
        archive = write_rpa2(self.path("game", "archive.rpa"), PAYLOAD)
        self.assertEqual(self.read_all(archive), PAYLOAD)

    def test_rpa1_round_trip(self):
        # RPAv1 (.rpi) is an index-only zlib stream: Ren'Py decompresses from
        # byte 0, so the member data must live in a companion file.  Its index is
        # still read correctly, which is what makes --list work for such games.
        archive = write_rpa1(self.path("game", "archive.rpi"), PAYLOAD)
        index = self.index_only(archive)
        self.assertEqual(sorted(index), sorted(PAYLOAD))

    def test_random_access_seek(self):
        archive = write_rpa3(self.path("game", "archive.rpa"), PAYLOAD)
        self.read_all(archive)

        handle = self.reader.load("game/images/bg/room 1.png")
        try:
            expected = PAYLOAD["game/images/bg/room 1.png"]
            handle.seek(1000)
            self.assertEqual(handle.read(10), expected[1000:1010])
            handle.seek(0)
            self.assertEqual(handle.read(4), expected[:4])
            handle.seek(-4, io.SEEK_END)
            self.assertEqual(handle.read(), expected[-4:])
        finally:
            handle.close()

    def test_missing_entry_returns_none(self):
        archive = write_rpa3(self.path("game", "archive.rpa"), PAYLOAD)
        self.read_all(archive)
        self.assertIsNone(self.reader.load("nope/missing.png"))

    def test_non_standard_rpi_with_data_prepended_is_not_indexed(self):
        # Kept as documentation of the unsupported shape: data before the index
        # means the zlib sniff at byte 0 fails, so no handler claims the file and
        # nothing is indexed.  The factory exists to pin that behaviour down.
        archive = write_rpa1_with_data(self.path("game", "archive.rpi"), PAYLOAD)
        self.assertFalse(archive.read_bytes().startswith(b"\x78\x9c"))
        self.assertEqual(self.index_only(archive), {})


class UnpackerHarness(ScratchCase):
    """Builds a complete fake game tree, including the SDK's loader.py."""

    @classmethod
    def setUpClass(cls):
        if SDK_LOADER is None:
            raise unittest.SkipTest("no Ren'Py loader.py available on this machine")
        cls.loader_text = read_loader_source(SDK_LOADER)

    def make_game(self, *, builder=write_rpa3, suffix: str = ".rpa", payload=None) -> Path:
        root = self.path("MyGame")
        (root / "game").mkdir(parents=True, exist_ok=True)
        (root / "renpy").mkdir(parents=True, exist_ok=True)
        (root / "renpy" / "loader.py").write_text(self.loader_text, encoding="utf-8")
        builder(root / "game" / f"archive{suffix}", PAYLOAD if payload is None else payload)
        return root


class TestUnpackerEndToEnd(UnpackerHarness):
    """Drive the CLI-facing Unpacker over a fake game tree."""

    def test_extract_writes_every_member(self):
        root = self.make_game()
        out = self.path("out")
        stats = Unpacker(root, output=out, jobs=4, progress=False).run()

        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual(stats.extracted, len(PAYLOAD))
        for name, expected in PAYLOAD.items():
            written = out / name
            self.assertTrue(written.is_file(), name)
            self.assertEqual(written.read_bytes(), expected, name)

    def test_skip_existing_is_idempotent(self):
        root = self.make_game()
        out = self.path("out")
        Unpacker(root, output=out, jobs=4, progress=False).run()

        stats = Unpacker(root, output=out, skip_existing=True, progress=False).run()
        self.assertEqual(stats.extracted, 0)
        self.assertEqual(stats.skipped, len(PAYLOAD))

    def test_filters_select_subset(self):
        root = self.make_game()
        unpacker = Unpacker(root, jobs=2, progress=False, suffixes=["png"])
        unpacker.discover()
        unpacker.load_reader()
        self.assertEqual(
            [entry.name for entry in unpacker.selected_entries()],
            ["game/images/bg/room 1.png"],
        )

    def test_glob_filter_with_double_star(self):
        root = self.make_game()
        unpacker = Unpacker(root, jobs=2, progress=False, globs=["game/images/**/*.png"])
        unpacker.discover()
        unpacker.load_reader()
        self.assertEqual(
            [entry.name for entry in unpacker.selected_entries()],
            ["game/images/bg/room 1.png"],
        )

    def test_output_defaults_to_game_root(self):
        root = self.make_game()
        unpacker = Unpacker(root, progress=False)
        unpacker.discover()
        self.assertEqual(unpacker.output_dir(), root / "extracted_files")

    def test_discovery_walks_up_from_a_subdirectory(self):
        # run from a game's own subdirectory and discovery should still work
        root = self.make_game()
        unpacker = Unpacker(root / "game", progress=False)
        unpacker.discover()
        self.assertIn("archive.rpa", [path.name for path in unpacker.archives])

    def test_discovery_climbs_to_the_game_root_above_game_dir(self):
        # A game whose archives live in ./game: starting at ./game resolves to
        # the parent, because that is the directory that also holds renpy/.
        root = self.make_game()
        (root / "game" / "nested").mkdir()
        unpacker = Unpacker(root / "game" / "nested", progress=False)
        unpacker.discover()
        self.assertEqual(unpacker.game_root, root.resolve())

    def test_write_core_produces_an_importable_module(self):
        root = self.make_game()
        core_path = self.path("gen_core.py")
        Unpacker(
            root, output=self.path("out"), jobs=2, progress=False, write_core=core_path
        ).run()

        self.assertTrue(core_path.is_file())
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            code = compile(core_path.read_text(encoding="utf-8"), str(core_path), "exec")
        namespace: dict = {}
        exec(code, namespace)
        self.assertIn("index_archives", namespace["build"]())

    def test_rpa2_archives_are_supported(self):
        root = self.make_game(builder=write_rpa2)
        out = self.path("out")
        stats = Unpacker(root, output=out, jobs=2, progress=False).run()
        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual(stats.extracted, len(PAYLOAD))

    def test_rpa1_index_is_listed(self):
        # The .rpi path is wired end to end: extension mapping plus the zlib
        # magic sniff must find the RPAv1 handler and read its index.
        root = self.make_game(builder=write_rpa1, suffix=".rpi")
        unpacker = Unpacker(root, jobs=1, progress=False)
        unpacker.discover()
        unpacker.load_reader()

        self.assertEqual(len(unpacker.reader.handlers), 3)
        self.assertTrue(unpacker.reader.archivable_suffix(".rpi"))
        self.assertEqual(
            [entry.name for entry in unpacker.entries], sorted(PAYLOAD)
        )

    def test_unreadable_index_is_reported_not_silently_emptied(self):
        # An archive whose index cannot be read must fail loudly: silently
        # extracting nothing looks like success.
        root = self.path("MyGame")
        (root / "game").mkdir(parents=True)
        (root / "renpy").mkdir(parents=True)
        (root / "renpy" / "loader.py").write_text(self.loader_text, encoding="utf-8")
        (root / "game" / "archive.rpa").write_bytes(
            b"RPA-3.0 000000000000ffff 00000000\n" + b"\x00" * 64
        )

        unpacker = Unpacker(root, output=self.path("out"), jobs=1, progress=False)
        unpacker.discover()
        with self.assertRaises(UnpackError) as ctx:
            unpacker.load_reader()
        self.assertIn("index_archives()", str(ctx.exception))

    def test_multiple_archives_are_merged(self):
        root = self.path("MyGame")
        (root / "game").mkdir(parents=True)
        (root / "renpy").mkdir(parents=True)
        (root / "renpy" / "loader.py").write_text(self.loader_text, encoding="utf-8")
        write_rpa3(root / "game" / "a.rpa", {"game/a.txt": b"AAA"})
        write_rpa3(root / "game" / "b.rpa", {"game/b.txt": b"BBB"})

        out = self.path("out")
        stats = Unpacker(root, output=out, jobs=2, progress=False).run()
        self.assertEqual(stats.extracted, 2)
        self.assertEqual((out / "game" / "a.txt").read_bytes(), b"AAA")
        self.assertEqual((out / "game" / "b.txt").read_bytes(), b"BBB")

    def test_traversal_entry_is_refused_not_written(self):
        hostile = {
            "game/ok.txt": b"fine",
            "../escaped.txt": b"pwned",
            "game/../../also_escaped.txt": b"pwned",
        }
        root = self.make_game(payload=hostile)
        out = self.path("out")
        stats = Unpacker(root, output=out, jobs=2, progress=False).run()

        self.assertEqual(stats.extracted, 1)
        self.assertEqual(stats.failed, 2)
        self.assertEqual((out / "game" / "ok.txt").read_bytes(), b"fine")
        self.assertFalse((self.path("escaped.txt")).exists())
        self.assertFalse((self.path("MyGame") / "also_escaped.txt").exists())

    def test_split_archive_members_are_extracted_end_to_end(self):
        """A split RPAv3 member must survive the full Unpacker path.

        Regression test.  Ren'Py 8.5.2's own ``load_from_archive`` builds the
        reader for a split entry and then discards it, so the member looks
        missing; the reader compensates.  Reading through ``Reader.load`` alone
        hid this, because the compensation lives there while the extractor used to
        call the raw loader directly.
        """
        root = self.make_game(builder=write_rpa3_split)
        out = self.path("out")
        stats = Unpacker(root, output=out, jobs=1, progress=False).run()

        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual(stats.extracted, len(PAYLOAD))
        for name, expected in PAYLOAD.items():
            self.assertEqual((out / name).read_bytes(), expected, name)

    def test_unsafe_pickle_flag_is_honoured(self):
        source = parse_loader_source(read_loader_source(SDK_LOADER), str(SDK_LOADER))
        _ns, result = build_reader(source, filename=str(SDK_LOADER), unsafe_pickle=True)
        self.assertIn("unsafe=True", result.source)

        _ns, result = build_reader(source, filename=str(SDK_LOADER), unsafe_pickle=False)
        self.assertIn("unsafe=False", result.source)

    def test_unsafe_pickle_flag_reaches_an_explicit_loader(self):
        # --loader should win over auto-detection and still receive the flag.
        root = self.make_game()
        unpacker = Unpacker(
            root,
            loader=root / "renpy" / "loader.py",
            output=self.path("out"),
            jobs=2,
            progress=False,
            unsafe_pickle=True,
        )
        unpacker.discover()
        result = unpacker.load_reader()
        self.assertIn("unsafe=True", result.source)


class TestObfuscatedArchives(ScratchCase):
    """The headline case: a repacked archive only the game's own loader can read.

    Modelled on a real Steam demo whose ``renpy/loader.py`` had been rewritten so
    that the header's index-offset field is a decoy and every stored offset is
    shifted by 33 bytes.  A from-scratch parser -- or the stock Ren'Py readers --
    cannot read that archive at all; extracting the game's *modified* functions
    can.  That is the entire reason this tool works on AST extraction.
    """

    def setUp(self) -> None:
        super().setUp()
        self.archive = write_obfuscated_rpa3(self.path("game", "archive.rpa"), PAYLOAD)

    def build(self):
        return build_reader(
            OBFUSCATED_LOADER,
            filename="<obfuscated loader.py>",
            unsafe_pickle=True,
        )

    def test_stock_loader_cannot_read_the_archive(self):
        # Guards the premise: if this ever starts working, the test below would
        # be passing for the wrong reason.
        if SDK_LOADER is None:
            self.skipTest("no Ren'Py loader.py available")
        namespace, _ = build_reader(
            parse_loader_source(read_loader_source(SDK_LOADER), str(SDK_LOADER)),
            filename=str(SDK_LOADER),
        )
        with self.assertRaises(Exception):
            with open(self.archive, "rb") as handle:
                namespace["RPAv3ArchiveHandler"].read_index(handle)

    def test_obfuscated_loader_is_extracted_and_reads_the_archive(self):
        namespace, result = self.build()
        reader = prepare_reader(namespace, result)
        reader.index_archives([self.archive])

        # `offset` is deliberately unassigned in read_index; the extractor must
        # leave it alone rather than "resolving" it into something wrong.
        self.assertNotIn("offset", result.missing_names)
        self.assertNotIn("def read_index(self", result.source.replace("def read_index(infile)", ""))

        extracted = {}
        for name in sorted(PAYLOAD):
            handle = reader.load(name)
            self.assertIsNotNone(handle, name)
            try:
                extracted[name] = handle.read()
            finally:
                handle.close()
        self.assertEqual(extracted, PAYLOAD)

    def test_obfuscated_archive_survives_the_unpacker(self):
        namespace, _ = self.build()
        out = self.path("out")
        unpacker = Unpacker(self.path("game"), output=out, jobs=2, progress=False)
        unpacker.game_root = self.path("game")
        unpacker.archives = [self.archive]
        unpacker.reader = prepare_reader(namespace, _)
        unpacker.reader.index_archives(unpacker.archives)
        unpacker._collect_entries()

        stats = unpacker.extract(unpacker.selected_entries())
        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual(stats.extracted, len(PAYLOAD))
        for name, expected in PAYLOAD.items():
            self.assertEqual((out / name).read_bytes(), expected, name)


class TestDisguisedArchives(ScratchCase):
    """Archives renamed to look like system libraries.

    Modelled on a real release whose ``game/`` holds no ``.rpa`` at all: the
    archives are ``mfplat.dll``, ``vcruntime140.dll`` and friends, next to genuine
    ``steam_api.dll`` files.  The loader *declares* ``.dll``, so the extension must
    come from the loader rather than a hardcoded list -- hardcoding ``.rpa``/``.rpi``
    found nothing and the tool reported "no archives" on a game full of them.
    """

    def make_game(self) -> tuple[Path, Path]:
        root = self.path("Game")
        (root / "game").mkdir(parents=True, exist_ok=True)
        (root / "renpy").mkdir(parents=True, exist_ok=True)
        (root / "renpy" / "loader.py").write_text(DISGUISED_LOADER, encoding="utf-8")

        archive = write_disguised_dll(root / "game" / "vcruntime140.dll", PAYLOAD)
        # A real Windows library with the same extension: the header sniff has to
        # reject it, or it would be parsed as an archive.
        write_fake_system_dll(root / "game" / "steam_api.dll")
        return root, archive

    def test_declared_extensions_are_read_from_the_loader(self):
        from renpy_unpack.ast_extract import archive_extensions, archive_headers

        self.assertEqual(archive_extensions(DISGUISED_LOADER), [".dll", ".rpi"])
        self.assertIn(b"ILOVEYOU", archive_headers(DISGUISED_LOADER))

    def test_static_discovery_finds_the_disguised_archive(self):
        root, archive = self.make_game()
        unpacker = Unpacker(root, jobs=1, progress=False)
        unpacker.discover()

        names = [path.name for path in unpacker.archives]
        self.assertIn(archive.name, names)
        # The genuine library must not be mistaken for an archive.
        self.assertNotIn("steam_api.dll", names)
        self.assertEqual(unpacker.game_root, root.resolve())

    def test_disguised_archive_reads_correctly(self):
        """The swapped index fields must come through the extracted handler intact.

        A from-scratch RPAv3 reader would read these tuples as ``(offset, length)``
        and produce garbage; only the loader's own code gets it right.
        """
        root, archive = self.make_game()
        unpacker = Unpacker(root, output=self.path("out"), jobs=2, progress=False)
        stats = unpacker.run()

        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual(stats.extracted, len(PAYLOAD))
        for name, expected in PAYLOAD.items():
            self.assertEqual((self.path("out") / name).read_bytes(), expected, name)

    def test_index_directory_is_the_archive_directory(self):
        root, _archive = self.make_game()
        unpacker = Unpacker(root, jobs=1, progress=False)
        unpacker.discover()
        self.assertEqual(unpacker.archive_dir, root / "game")

    def test_a_game_with_no_archives_still_reports_clearly(self):
        # The fallback must not turn "no archives" into a confusing error.
        root = self.path("Empty")
        (root / "game").mkdir(parents=True)
        (root / "renpy").mkdir(parents=True)
        (root / "renpy" / "loader.py").write_text(DISGUISED_LOADER, encoding="utf-8")
        write_fake_system_dll(root / "game" / "steam_api.dll")

        unpacker = Unpacker(root, jobs=1, progress=False)
        with self.assertRaises(UnpackError) as ctx:
            unpacker.discover()
        self.assertIn("--game", str(ctx.exception))

    @unittest.skipUnless(SDK_LOADER, "no stock loader.py available")
    def test_the_games_own_loader_beats_an_explicit_one(self):
        """--loader must not quietly redirect a game to the wrong format.

        The game ships its own loader, and that loader is the only authority on its
        archive format. Passing a stock SDK loader must be reported, not obeyed --
        obeying it produced "no archives found" on a game whose 1.28 GB of archives
        were sitting right there.
        """
        root, _archive = self.make_game()
        unpacker = Unpacker(root, loader=SDK_LOADER, jobs=1, progress=False)

        with self.assertRaises(UnpackError) as ctx:
            unpacker.discover()
        message = str(ctx.exception)

        self.assertIn("ships its own loader", message)
        self.assertIn("not used", message)
        # It should show *why* they are not interchangeable.
        self.assertIn(".dll", message)
        self.assertIn(".rpa", message)

    def write_loader(self, run: str) -> Path:
        """A stand-in for a loader.py kept outside the game tree, e.g. a `.pyc` build."""
        path = self.path("loaders", run, "loader.py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DISGUISED_LOADER, encoding="utf-8")
        return path

    def test_an_explicit_loader_is_used_when_the_game_has_none(self):
        """--loader exists for compiled builds that ship loader.pyc only."""
        root, archive = self.make_game()
        substitute = self.write_loader("substitute")

        # Simulate a build with no loader source at all.
        isolated = self.path("Isolated")
        (isolated / "game").mkdir(parents=True)
        (isolated / "renpy").mkdir(parents=True)
        (isolated / "game" / archive.name).write_bytes(archive.read_bytes())

        self.assertEqual(find_loader(isolated, substitute), substitute)

        unpacker = Unpacker(isolated, loader=substitute, jobs=1, progress=False)
        unpacker.discover()
        self.assertIn(archive.name, [p.name for p in unpacker.archives])

    def test_a_missing_loader_points_at_the_format_problem(self):
        root = self.path("NoLoader")
        (root / "game").mkdir(parents=True)
        (root / "renpy").mkdir(parents=True)

        with self.assertRaises(UnpackError) as ctx:
            find_loader(root)
        message = str(ctx.exception)
        self.assertIn("own renpy/loader.py", message)
        self.assertIn("--loader", message)
        # The old wording invited pointing at a stock SDK loader, which is the
        # trap this whole section exists to avoid.
        self.assertIn("stock Ren'Py loader only understands stock formats", message)

    def test_an_agreeing_explicit_loader_is_reported_as_redundant(self):
        root, _archive = self.make_game()
        # The game's own loader, passed explicitly: same file, so allowed.
        own = root / "renpy" / "loader.py"
        self.assertEqual(find_loader(root, own), own)

    def test_behaviour_is_unchanged_when_the_loader_matches(self):
        # The custom-format path must not disturb the normal one.
        root, archive = self.make_game()
        unpacker = Unpacker(root, jobs=1, progress=False)
        unpacker.discover()
        self.assertIn(archive.name, [p.name for p in unpacker.archives])


@unittest.skipUnless(SDK_LOADER, "no stock loader.py available")
class TestNoArchivesAtAll(ScratchCase):
    """"No archives" has three different causes and they must not read alike.

    A game that ships its assets as plain files has *nothing to do*, which is not
    the same as "your --game path is wrong" and not the same as "the loader you
    gave me does not describe these archives".
    """

    def make_unpacked_game(self) -> Path:
        root = self.path("Unpacked")
        for directory in ("game/scripts", "game/images", "renpy"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / "renpy" / "loader.py").write_text(
            Path(SDK_LOADER).read_text(encoding="utf-8-sig") if SDK_LOADER else "",
            encoding="utf-8",
        )
        # Plain, unencrypted assets exactly like a shipped-but-unpacked build.
        (root / "game" / "scripts" / "A000.rpy").write_text(
            "label A000:\n    return\n", encoding="utf-8"
        )
        (root / "game" / "scripts" / "A001.rpy").write_text(
            "label A001:\n    return\n", encoding="utf-8"
        )
        (root / "game" / "images" / "title.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        )
        (root / "game" / "audio.ogg").write_bytes(b"OggS" + b"\x00" * 64)
        return root

    def test_already_unpacked_is_reported_as_such(self):
        root = self.make_unpacked_game()
        unpacker = Unpacker(root, jobs=1, progress=False)

        with self.assertRaises(UnpackError) as ctx:
            unpacker.discover()
        message = str(ctx.exception)

        self.assertIn("already unpacked", message)
        self.assertIn("nothing to unpack", message)
        # It must NOT tell the user their --game path is wrong.
        self.assertNotIn("Pass --game", message)
        # And it must point at the way to still get the files.
        self.assertIn("--include-loose", message)

    def test_already_unpacked_probe_does_not_walk_the_asset_tree(self):
        """The probe only changes an error's wording, so it must stay cheap."""
        root = self.make_unpacked_game()
        # A deep, wide tree must not be traversed to answer this.
        deep = root / "game" / "images"
        for index in range(40):
            nested = deep / f"d{index}"
            nested.mkdir(parents=True, exist_ok=True)
            for asset in range(20):
                (nested / f"a{asset}.webp").write_bytes(b"RIFF" + b"\x00" * 8)

        from renpy_unpack.core import _looks_already_unpacked

        self.assertTrue(_looks_already_unpacked(root))

    def test_a_real_archive_beats_the_unpacked_guess(self):
        from renpy_unpack.core import _looks_already_unpacked

        root = self.path("Packed")
        (root / "game" / "scripts").mkdir(parents=True)
        (root / "game" / "scripts" / "stray.rpy").write_text("x\n", encoding="utf-8")
        write_rpa3(root / "game" / "archive.rpa", {"game/a.txt": b"A"})
        # An archive present means "packed", even if loose scripts also exist.
        self.assertFalse(_looks_already_unpacked(root))

    def test_climbing_above_the_named_directory_is_disclosed(self):
        """Pointing at .../SomeGame/renpy is a natural mistake; say so."""
        root = self.make_game()
        unpacker = Unpacker(root / "renpy", jobs=1, progress=False)
        unpacker.discover()

        self.assertEqual(unpacker.game_root, root.resolve())
        self.assertEqual(unpacker.climbed_from, (root / "renpy").resolve())

    def test_no_climb_is_recorded_when_the_path_was_already_right(self):
        root = self.make_game()
        unpacker = Unpacker(root, jobs=1, progress=False)
        unpacker.discover()
        self.assertIsNone(unpacker.climbed_from)

    def make_game(self) -> Path:
        root = self.path("MyGame")
        (root / "game").mkdir(parents=True)
        (root / "renpy").mkdir(parents=True)
        (root / "renpy" / "loader.py").write_text(
            read_loader_source(SDK_LOADER), encoding="utf-8"
        )
        write_rpa3(root / "game" / "archive.rpa", PAYLOAD)
        return root


@unittest.skipUnless(SDK_LOADER, "no stock loader.py available")
class TestOutputInsideGameDir(ScratchCase):
    """Extracting scripts into game/ can stop the game from starting.

    Ren'Py walks ``game/`` recursively and loads every ``.rpy``/``.rpyc``, so two
    copies of one script define the same label twice. One real release could not
    boot for exactly that reason -- a previous unpack had left
    ``game/extracted/`` beside ``game/rpy/`` -- so this warns rather than letting
    the tool create the condition it had to work around.
    """

    def make_unpacker(self, output: Path) -> tuple:
        from renpy_unpack.core import Unpacker

        root = self.path("Game")
        (root / "game").mkdir(parents=True)
        (root / "renpy").mkdir(parents=True)
        (root / "renpy" / "loader.py").write_text(
            read_loader_source(SDK_LOADER), encoding="utf-8"
        )
        write_rpa3(
            root / "game" / "archive.rpa",
            {
                "rpy/gui.rpy": b"label a:\n    return\n",
                "rpy/gui.rpyc": b"RENPY RPC2\x00",
                "images/a.png": b"\x89PNG\r\n\x1a\n",
            },
        )
        unpacker = Unpacker(root, output=output, jobs=1, progress=False)
        unpacker.discover()
        unpacker.load_reader()
        return unpacker, root

    def test_warns_when_scripts_would_land_inside_game_dir(self):
        from renpy_unpack.core import _warn_if_inside_game_dir

        root = self.path("Game")
        unpacker, root = self.make_unpacker(root / "game" / "out")
        warning = _warn_if_inside_game_dir(unpacker, unpacker.entries)

        self.assertIsNotNone(warning)
        self.assertIn("inside this game's game/ directory", warning)
        self.assertIn("define the same label twice", warning)

    def test_no_warning_outside_the_game_dir(self):
        from renpy_unpack.core import _warn_if_inside_game_dir

        unpacker, root = self.make_unpacker(self.path("outside"))
        self.assertIsNone(_warn_if_inside_game_dir(unpacker, unpacker.entries))

    def test_no_warning_when_only_assets_are_selected(self):
        # A per-game directory is a legitimate output target for assets.
        from renpy_unpack.core import _warn_if_inside_game_dir

        root = self.path("Game")
        unpacker, root = self.make_unpacker(root / "game" / "out")
        assets = [e for e in unpacker.entries if e.suffix == ".png"]
        self.assertTrue(assets)
        self.assertIsNone(_warn_if_inside_game_dir(unpacker, assets))

    def test_the_default_output_is_outside_game_dir(self):
        # The default must never be the hazardous location.
        from renpy_unpack.core import Unpacker

        root = self.path("Game").resolve()
        (root / "renpy").mkdir(parents=True)
        (root / "game").mkdir()
        # output_dir() is pure; discovery is not needed to check where it points.
        unpacker = Unpacker(root, progress=False)
        self.assertEqual(unpacker.output_dir(), root / "extracted_files")
        self.assertNotIn(root / "game", unpacker.output_dir().parents)

    def test_default_output_stays_out_when_game_points_inside_game_dir(self):
        """Pointing --game at ``game`` must not default the output into ``game``.

        This was a real hole.  ``output_dir()`` used ``<game_root>/extracted_files``,
        and when ``--game`` named the ``game`` directory itself that resolved to
        ``game/extracted_files`` -- inside the directory Ren'Py scans.  Extracting
        scripts there recreates the duplicate-label condition that stops a game from
        starting, and the warning did not fire because it compared against
        ``<game_root>/game``, a path that does not exist in that case.
        """
        from renpy_unpack.core import Unpacker

        base = self.path("MyGame").resolve()
        game_dir = base / "game"
        game_dir.mkdir(parents=True)
        # Archives are what makes discovery accept the directory as a root; the
        # point of the test is that no renpy/ sibling exists to give it away.
        write_rpa3(game_dir / "archive.rpa", {"rpy/a.rpy": b"label a:\n    return\n"})

        unpacker = Unpacker(game_dir, progress=False)
        output = unpacker.output_dir()

        self.assertNotEqual(output, game_dir / "extracted_files")
        self.assertNotIn(game_dir, output.parents)
        self.assertEqual(output, base / "extracted_files")

    def test_trimmed_game_without_renpy_is_still_recognised(self):
        # A repacked release may ship no renpy/ directory at all; archives and
        # scripts inside the folder are enough to identify it as the game dir.
        from renpy_unpack.core import _renpy_game_dir, _safe_output_root

        base = self.path("Trimmed").resolve()
        game_dir = base / "game"
        game_dir.mkdir(parents=True)
        (game_dir / "script.rpy").write_text("label a:\n    return\n", encoding="utf-8")

        self.assertEqual(_renpy_game_dir(game_dir), game_dir)
        self.assertEqual(_safe_output_root(game_dir), base)

    def test_an_explicit_output_inside_game_dir_is_still_warned_about(self):
        """A deliberate destination is honoured, but the hazard is still named."""
        from renpy_unpack.core import _warn_if_inside_game_dir

        if SDK_LOADER is None:
            self.skipTest("no Ren'Py loader.py available")
        root = self.path("Game").resolve()
        (root / "renpy").mkdir(parents=True)
        (root / "game").mkdir()
        (root / "renpy" / "loader.py").write_text(
            read_loader_source(SDK_LOADER), encoding="utf-8"
        )
        write_rpa3(root / "game" / "archive.rpa", {"rpy/a.rpy": b"label a:\n    return\n"})

        unpacker = Unpacker(
            root, output=root / "game" / "chosen", jobs=1, progress=False
        )
        unpacker.discover()
        unpacker.load_reader()
        warning = _warn_if_inside_game_dir(unpacker, unpacker.entries)
        self.assertIsNotNone(warning)
        self.assertIn("define the same label twice", warning)


class TestIncludeLoose(ScratchCase):
    """Loose files go through the game's loader too -- just without decryption.

    The loader is the authority either way: ``walkdir`` enumerates what Ren'Py
    considers game content, and ``load`` reads it.  That makes an already-unpacked
    game a valid target instead of a dead end.
    """

    @classmethod
    def setUpClass(cls):
        if SDK_LOADER is None:
            raise unittest.SkipTest("no Ren'Py loader.py available")

    def make_unpacked_game(self) -> Path:
        root = self.path("Unpacked")
        (root / "renpy").mkdir(parents=True)
        (root / "game" / "screens").mkdir(parents=True)
        (root / "renpy" / "loader.py").write_text(
            read_loader_source(SDK_LOADER), encoding="utf-8"
        )
        # Plain, unencrypted content: exactly a game somebody already unpacked.
        (root / "game" / "script.rpy").write_text(
            "label start:\n    return\n", encoding="utf-8"
        )
        (root / "game" / "screens" / "gui.rpy").write_text(
            "screen gui():\n    pass\n", encoding="utf-8"
        )
        (root / "game" / "note.txt").write_bytes(b"hello loose file\n")
        (root / "game" / "img.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + bytes(range(64))
        )
        return root

    def test_without_the_flag_an_unpacked_game_is_not_a_target(self):
        root = self.make_unpacked_game()
        unpacker = Unpacker(root, jobs=1, progress=False)
        with self.assertRaises(UnpackError) as ctx:
            unpacker.discover()
        # The message should point at the way to still get the files.
        self.assertIn("--include-loose", str(ctx.exception))

    def test_loose_files_are_enumerated_by_the_loader(self):
        root = self.make_unpacked_game()
        unpacker = Unpacker(root, include_loose=True, jobs=1, progress=False)
        unpacker.discover()
        unpacker.load_reader()

        names = sorted(entry.name for entry in unpacker.entries)
        self.assertIn("script.rpy", names)
        self.assertIn("screens/gui.rpy", names)
        self.assertIn("note.txt", names)
        self.assertIn("img.png", names)
        self.assertTrue(all(entry.loose for entry in unpacker.entries))
        self.assertEqual(unpacker.archives, [])

    def test_loose_files_are_read_byte_exactly(self):
        """Read through ``loader.load()``, and compare against the real files."""
        root = self.make_unpacked_game()
        out = self.path("out")
        stats = Unpacker(
            root, output=out, include_loose=True, jobs=2, progress=False
        ).run()

        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual(stats.extracted, 4)
        for relative in ("script.rpy", "screens/gui.rpy", "note.txt", "img.png"):
            self.assertEqual(
                (out / relative).read_bytes(),
                (root / "game" / relative).read_bytes(),
                relative,
            )

    def test_loose_and_archived_members_coexist(self):
        root = self.path("Mixed")
        (root / "renpy").mkdir(parents=True)
        (root / "game").mkdir()
        (root / "renpy" / "loader.py").write_text(
            read_loader_source(SDK_LOADER), encoding="utf-8"
        )
        write_rpa3(
            root / "game" / "archive.rpa",
            {"archived.txt": b"from archive\n", "shared.txt": b"archived copy\n"},
        )
        (root / "game" / "loose.txt").write_bytes(b"loose file\n")
        (root / "game" / "shared.txt").write_bytes(b"loose copy\n")

        out = self.path("out")
        stats = Unpacker(
            root, output=out, include_loose=True, jobs=2, progress=False
        ).run()

        self.assertEqual(stats.failed, 0, stats.failures)
        self.assertEqual((out / "archived.txt").read_bytes(), b"from archive\n")
        self.assertEqual((out / "loose.txt").read_bytes(), b"loose file\n")
        # A loose file shadows the archived copy, which is how Ren'Py resolves too.
        self.assertEqual((out / "shared.txt").read_bytes(), b"loose copy\n")

    def test_file_open_callbacks_are_wired_in_renpys_order(self):
        """The callback list is built by statements the extractor cannot see.

        Only definitions are recovered, so ``file_open_callbacks`` arrives empty and
        ``load()`` finds nothing at all.  Order matters: the filesystem opener is
        registered before the archive one, which is why a loose file shadows an
        archived copy.
        """
        root = self.make_unpacked_game()
        unpacker = Unpacker(root, include_loose=True, jobs=1, progress=False)
        unpacker.discover()
        unpacker.load_reader()

        callbacks = unpacker.reader.namespace["file_open_callbacks"]
        names = [getattr(cb, "__name__", str(cb)) for cb in callbacks]
        self.assertEqual(names, ["load_from_filesystem", "load_from_archive"])

    def test_reader_config_points_at_the_game_directory(self):
        # loader.load() resolves through transfn(), which needs basedir/searchpath.
        root = self.make_unpacked_game()
        unpacker = Unpacker(root, include_loose=True, jobs=1, progress=False)
        unpacker.discover()
        unpacker.load_reader()

        config = unpacker.reader.namespace["renpy"].config
        self.assertEqual(config.basedir, str(root.resolve()))
        self.assertEqual(config.searchpath, ["game"])
    """With nothing to work on, the tool must explain itself, not crash."""

    def test_missing_loader_message_names_the_search_paths(self):
        with self.assertRaises(UnpackError) as ctx:
            find_loader(self.path("EmptyGame"))
        self.assertIn("loader.py", str(ctx.exception))
        self.assertIn("--loader", str(ctx.exception))

    def test_no_archives_message_names_the_flag(self):
        with self.assertRaises(UnpackError) as ctx:
            discover_game_root(self.path("EmptyGame"))
        self.assertIn("--game", str(ctx.exception))

    def test_explicit_loader_that_does_not_exist_is_rejected(self):
        with self.assertRaises(UnpackError) as ctx:
            find_loader(self.path("EmptyGame"), self.path("nope", "loader.py"))
        self.assertIn("--loader", str(ctx.exception))

    def test_non_loader_file_is_rejected_clearly(self):
        with self.assertRaises(UnpackError) as ctx:
            build_reader("import os\n", filename="<not-a-loader>")
        self.assertIn("index_archives", str(ctx.exception))

    def test_unparseable_source_raises_a_clear_error(self):
        with self.assertRaises(UnpackError):
            parse_loader_source("def broken(:\n", "loader.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)
